"""Execution risk checks and order normalization for paper/live trading."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from arb_strat.config import AppConfig
from arb_strat.models import BalanceSnapshot, Opportunity, OrderIntent
from arb_strat.state import StateStore


class RiskViolation(RuntimeError):
    """Raised when an opportunity fails execution guardrail checks."""


@dataclass(frozen=True)
class PreparedOpportunity:
    """Opportunity approved for execution after sizing and validation."""

    opportunity: Opportunity
    total_notional: float
    balance_snapshots: tuple[BalanceSnapshot, ...] = ()


class RiskManager:
    """Apply configurable trading guardrails before paper or live execution."""

    def __init__(self, config: AppConfig, state_store: StateStore) -> None:
        """Store config, state tracking, and failure counters."""
        self.config = config
        self.state_store = state_store
        self.consecutive_failures = 0

    def prepare(
        self,
        opportunity: Opportunity,
        clients: dict[str, object],
        *,
        live: bool,
    ) -> PreparedOpportunity:
        """Validate and normalize an opportunity before execution."""
        if not opportunity.orders:
            raise RiskViolation("opportunity has no orders")

        if live and self.state_store.is_execution_paused():
            raise RiskViolation("execution is paused")
        if live and self._daily_loss_limit_reached():
            self.state_store.set_execution_paused(
                True,
                reason="paused because the daily live loss limit was reached",
            )
            raise RiskViolation("daily live loss limit reached")
        if live and not self._strategy_allowed(opportunity.strategy):
            raise RiskViolation(f"live execution disabled for strategy {opportunity.strategy}")

        total_requested_notional = sum(order.amount * order.price for order in opportunity.orders)
        if total_requested_notional <= 0:
            raise RiskViolation("opportunity notional is zero")

        scaling_factors = [1.0]
        if self.config.risk.enabled:
            scaling_factors.append(
                min(1.0, self.config.risk.max_opportunity_notional / total_requested_notional)
            )

        balance_snapshots: list[BalanceSnapshot] = []
        balance_cache: dict[str, dict[str, float]] = {}
        latest_quotes: dict[tuple[str, str, str], object] = {}

        for order in opportunity.orders:
            client = clients[order.exchange]
            order_notional = order.amount * order.price
            if self.config.risk.enabled and order_notional > 0:
                scaling_factors.append(
                    min(1.0, self.config.risk.max_order_notional / order_notional)
                )
            quote = self._validate_market_snapshot(client, order, opportunity.strategy)
            latest_quotes[(order.exchange, order.symbol, order.side)] = quote

            if live:
                if order.exchange not in balance_cache:
                    balances = client.fetch_balance()
                    balance_cache[order.exchange] = balances
                    balance_snapshots.append(
                        self.state_store.record_balances(order.exchange, balances)
                    )
                scaling_factors.append(
                    self._balance_scaling_factor(
                        order=order,
                        balances=balance_cache[order.exchange],
                        fee_rate=client.taker_fee_bps / 10000.0,
                    )
                )
                scaling_factors.append(
                    self._inventory_cap_scaling_factor(
                        order=order,
                        balances=balance_cache[order.exchange],
                        fee_rate=client.taker_fee_bps / 10000.0,
                    )
                )

        scale = max(0.0, min(scaling_factors))
        if scale <= 0:
            raise RiskViolation("risk limits reduced execution size to zero")

        normalized_orders: list[OrderIntent] = []
        for order in opportunity.orders:
            client = clients[order.exchange]
            raw_amount = order.amount * scale
            amount, price = client.normalize_order(order.symbol, raw_amount, order.price)
            self._validate_order_limits(client, order.symbol, amount, price)
            normalized_orders.append(
                replace(
                    order,
                    amount=amount,
                    price=price,
                )
            )

        expected_slippage_cost, expected_slippage_bps = self._estimate_expected_slippage(
            normalized_orders,
            latest_quotes,
        )
        total_notional = sum(order.amount * order.price for order in normalized_orders)
        reference_notional = self._reference_notional(normalized_orders)
        scaled_expected_pnl = opportunity.expected_pnl * scale
        net_expected_pnl = scaled_expected_pnl - expected_slippage_cost
        net_edge_bps = (
            (net_expected_pnl / reference_notional) * 10000.0
            if reference_notional > 0
            else 0.0
        )

        self._validate_net_profit(
            opportunity=opportunity,
            live=live,
            net_expected_pnl=net_expected_pnl,
            net_edge_bps=net_edge_bps,
        )
        if live:
            self._validate_open_notional_cap(total_notional)

        metadata = dict(opportunity.metadata)
        metadata.update(
            {
                "raw_expected_pnl": opportunity.expected_pnl,
                "scaled_expected_pnl": scaled_expected_pnl,
                "expected_slippage_cost": expected_slippage_cost,
                "expected_slippage_bps": expected_slippage_bps,
                "net_expected_pnl": net_expected_pnl,
                "net_edge_bps": net_edge_bps,
                "reference_notional": reference_notional,
            }
        )
        normalized_opportunity = replace(
            opportunity,
            orders=tuple(normalized_orders),
            expected_pnl=net_expected_pnl,
            edge_bps=net_edge_bps,
            metadata=metadata,
        )
        return PreparedOpportunity(
            opportunity=normalized_opportunity,
            total_notional=total_notional,
            balance_snapshots=tuple(balance_snapshots),
        )

    def register_success(self) -> None:
        """Reset the consecutive failure counter after a clean execution."""
        self.consecutive_failures = 0

    def register_failure(self, reason: str) -> None:
        """Increment failure state and optionally pause live execution."""
        self.consecutive_failures += 1
        if (
            self.config.risk.pause_on_execution_error
            and self.consecutive_failures >= self.config.risk.max_consecutive_failures
        ):
            self.state_store.set_execution_paused(
                True,
                reason=(
                    f"paused after {self.consecutive_failures} consecutive execution failures: "
                    f"{reason}"
                ),
            )

    def resume(self) -> None:
        """Resume execution after a manual operator override."""
        self.consecutive_failures = 0
        self.state_store.set_execution_paused(False, reason="")

    def pause(self, reason: str) -> None:
        """Pause execution immediately for an operator or safety reason."""
        self.state_store.set_execution_paused(True, reason=reason)

    def summary(self) -> str:
        """Return the currently configured risk rules in operator-friendly text."""
        settings = self.config.risk
        return (
            "Risk rules\n"
            f"live triangular: {'on' if settings.allow_live_triangular else 'off'}\n"
            f"live cross-exchange: {'on' if settings.allow_live_cross_exchange else 'off'}\n"
            f"min order notional: {settings.min_order_notional:.2f}\n"
            f"max order notional: {settings.max_order_notional:.2f}\n"
            f"max opportunity notional: {settings.max_opportunity_notional:.2f}\n"
            f"min net profit usd: {settings.min_net_profit_usd:.2f}\n"
            f"min net profit bps (live): {settings.min_net_profit_bps_live:.2f}\n"
            f"max total open notional usd: {settings.max_total_open_notional_usd:.2f}\n"
            f"max daily loss usd: {settings.max_daily_loss_usd:.2f}\n"
            f"reserve balance pct: {settings.reserve_balance_pct:.2%}\n"
            f"max slippage: {settings.max_slippage_bps:.2f} bps\n"
            f"max quote age cross-exchange: {settings.max_quote_age_ms_cross_exchange} ms\n"
            f"max quote age triangular: {settings.max_quote_age_ms_triangular} ms\n"
            f"max live orders per cycle: {settings.max_live_orders_per_cycle}\n"
            f"max consecutive failures: {settings.max_consecutive_failures}\n"
            f"pause on partial fill: {'yes' if settings.pause_on_partial_fill else 'no'}\n"
            f"inventory caps configured: {'yes' if settings.max_asset_balance_by_exchange else 'no'}\n"
            f"current live pnl estimate usd: {self.state_store.current_live_pnl_estimate_usd():.2f}\n"
            f"execution paused: {'yes' if self.state_store.is_execution_paused() else 'no'}"
        )

    def _strategy_allowed(self, strategy: str) -> bool:
        """Return whether live trading is enabled for a given strategy."""
        if strategy == "triangular":
            return self.config.risk.allow_live_triangular
        if strategy == "cross_exchange":
            return self.config.risk.allow_live_cross_exchange
        return False

    def _validate_market_snapshot(self, client: object, order: OrderIntent, strategy: str):
        """Check quote freshness and slippage against the intended execution price."""
        quote = client.fetch_top_of_book(order.symbol)
        self._validate_quote_freshness(order, quote.timestamp_ms, strategy)
        slippage_multiplier = self.config.risk.max_slippage_bps / 10000.0
        if order.side == "buy":
            allowed_price = order.price * (1.0 + slippage_multiplier)
            if quote.ask > allowed_price:
                raise RiskViolation(
                    f"{order.exchange} {order.symbol} ask moved to {quote.ask:.8f}, above "
                    f"allowed {allowed_price:.8f}"
                )
            return quote

        allowed_price = order.price * (1.0 - slippage_multiplier)
        if quote.bid < allowed_price:
            raise RiskViolation(
                f"{order.exchange} {order.symbol} bid moved to {quote.bid:.8f}, below "
                f"allowed {allowed_price:.8f}"
            )
        return quote

    def _validate_quote_freshness(
        self,
        order: OrderIntent,
        timestamp_ms: int | None,
        strategy: str,
    ) -> None:
        """Reject orders when the last quote update is missing or stale."""
        max_age_ms = self._quote_age_limit_ms(strategy)
        if max_age_ms <= 0:
            return
        if timestamp_ms is None:
            raise RiskViolation(f"{order.exchange} {order.symbol} quote has no timestamp")

        quote_age_ms = int(time.time() * 1000) - int(timestamp_ms)
        if quote_age_ms > max_age_ms:
            raise RiskViolation(
                f"{order.exchange} {order.symbol} quote age {quote_age_ms} ms exceeds "
                f"max {max_age_ms} ms"
            )

    def _balance_scaling_factor(
        self,
        *,
        order: OrderIntent,
        balances: dict[str, float],
        fee_rate: float,
    ) -> float:
        """Return the largest allowable scale factor based on free balances."""
        base, quote = order.symbol.split("/")
        reserve_multiplier = max(0.0, 1.0 - self.config.risk.reserve_balance_pct)
        if order.side == "buy":
            available_quote = max(0.0, balances.get(quote, 0.0) * reserve_multiplier)
            required_quote = order.amount * order.price * (1.0 + fee_rate)
            if required_quote <= 0:
                return 0.0
            return min(1.0, available_quote / required_quote)

        available_base = max(0.0, balances.get(base, 0.0) * reserve_multiplier)
        if order.amount <= 0:
            return 0.0
        return min(1.0, available_base / order.amount)

    def _inventory_cap_scaling_factor(
        self,
        *,
        order: OrderIntent,
        balances: dict[str, float],
        fee_rate: float,
    ) -> float:
        """Limit execution size so resulting per-exchange inventory stays under configured caps."""
        caps = self.config.risk.max_asset_balance_by_exchange.get(order.exchange, {})
        if not caps:
            return 1.0

        base, quote = order.symbol.split("/")
        if order.side == "buy":
            cap = caps.get(base)
            if cap is None:
                return 1.0
            current_balance = balances.get(base, 0.0)
            max_additional = cap - current_balance
            if max_additional <= 0:
                return 0.0
            if order.amount <= 0:
                return 0.0
            return min(1.0, max_additional / order.amount)

        cap = caps.get(quote)
        if cap is None:
            return 1.0
        current_balance = balances.get(quote, 0.0)
        quote_increase = order.amount * order.price * (1.0 - fee_rate)
        max_additional = cap - current_balance
        if max_additional <= 0:
            return 0.0
        if quote_increase <= 0:
            return 0.0
        return min(1.0, max_additional / quote_increase)

    def _validate_order_limits(
        self,
        client: object,
        symbol: str,
        amount: float,
        price: float,
    ) -> None:
        """Validate rounded orders against config and exchange market limits."""
        if amount <= 0 or price <= 0:
            raise RiskViolation(f"{symbol} normalized to non-positive amount/price")

        notional = amount * price
        if self.config.risk.enabled and notional < self.config.risk.min_order_notional:
            raise RiskViolation(
                f"{symbol} notional {notional:.8f} is below configured minimum "
                f"{self.config.risk.min_order_notional:.8f}"
            )

        market = client.market_details(symbol)
        min_amount = ((market.get("limits") or {}).get("amount") or {}).get("min")
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min")
        if min_amount is not None and amount < float(min_amount):
            raise RiskViolation(
                f"{symbol} amount {amount:.8f} is below exchange minimum {float(min_amount):.8f}"
            )
        if min_cost is not None and notional < float(min_cost):
            raise RiskViolation(
                f"{symbol} notional {notional:.8f} is below exchange minimum {float(min_cost):.8f}"
            )

    def _estimate_expected_slippage(
        self,
        orders: list[OrderIntent],
        latest_quotes: dict[tuple[str, str, str], object],
    ) -> tuple[float, float]:
        """Estimate total execution slippage cost from top-of-book depth and spread."""
        total_cost = 0.0
        max_bps = 0.0
        for order in orders:
            quote = latest_quotes.get((order.exchange, order.symbol, order.side))
            if quote is None:
                continue
            order_notional = order.amount * order.price
            if order.side == "buy":
                available_notional = quote.ask_size * quote.ask
            else:
                available_notional = quote.bid_size * quote.bid
            if available_notional <= 0:
                raise RiskViolation(f"{order.exchange} {order.symbol} has no executable top-of-book depth")

            mid_price = (quote.ask + quote.bid) / 2.0
            if mid_price <= 0:
                continue
            spread_bps = max(0.0, ((quote.ask - quote.bid) / mid_price) * 10000.0)
            impact_ratio = max(0.0, order_notional / available_notional)
            expected_bps = impact_ratio * spread_bps * 0.5
            if expected_bps > self.config.risk.max_slippage_bps:
                raise RiskViolation(
                    f"{order.exchange} {order.symbol} estimated slippage {expected_bps:.2f} bps exceeds "
                    f"max {self.config.risk.max_slippage_bps:.2f} bps"
                )
            total_cost += order_notional * (expected_bps / 10000.0)
            max_bps = max(max_bps, expected_bps)
        return total_cost, max_bps

    def _reference_notional(self, orders: list[OrderIntent]) -> float:
        """Return the notional used to convert net pnl into a bps-style threshold."""
        buy_notional = sum(order.amount * order.price for order in orders if order.side == "buy")
        if buy_notional > 0:
            return buy_notional
        return sum(order.amount * order.price for order in orders)

    def _validate_net_profit(
        self,
        *,
        opportunity: Opportunity,
        live: bool,
        net_expected_pnl: float,
        net_edge_bps: float,
    ) -> None:
        """Reject opportunities that no longer clear post-cost profit floors."""
        if net_expected_pnl < self.config.risk.min_net_profit_usd:
            raise RiskViolation(
                f"{opportunity.summary} net pnl {net_expected_pnl:.6f} {opportunity.pnl_currency} "
                f"is below minimum {self.config.risk.min_net_profit_usd:.6f}"
            )
        if live and net_edge_bps < self.config.risk.min_net_profit_bps_live:
            raise RiskViolation(
                f"{opportunity.summary} net edge {net_edge_bps:.2f} bps is below live minimum "
                f"{self.config.risk.min_net_profit_bps_live:.2f} bps"
            )

    def _validate_open_notional_cap(self, new_notional: float) -> None:
        """Reject live orders that would breach the total in-flight portfolio cap."""
        cap = self.config.risk.max_total_open_notional_usd
        if cap <= 0:
            return
        current_open = self.state_store.open_notional_estimate()
        if current_open + new_notional > cap:
            raise RiskViolation(
                f"open notional {current_open + new_notional:.2f} would exceed portfolio cap {cap:.2f}"
            )

    def _quote_age_limit_ms(self, strategy: str) -> int:
        """Return the per-strategy quote age threshold with a backward-compatible fallback."""
        if strategy == "cross_exchange":
            return self.config.risk.max_quote_age_ms_cross_exchange
        if strategy == "triangular":
            return self.config.risk.max_quote_age_ms_triangular
        return self.config.risk.max_quote_age_ms

    def _daily_loss_limit_reached(self) -> bool:
        """Return whether the current UTC-day live pnl estimate is below the configured floor."""
        limit = self.config.risk.max_daily_loss_usd
        if limit <= 0:
            return False
        return self.state_store.current_live_pnl_estimate_usd() <= -limit
