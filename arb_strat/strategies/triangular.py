"""Triangular arbitrage scanner for one exchange at a time."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

from arb_strat.config import TriangularSettings
from arb_strat.models import Opportunity, OrderIntent, Quote


@dataclass(frozen=True)
class Step:
    """One market conversion step before order sizing is finalized."""
    symbol: str
    side: str
    price: float
    available_base: float


@dataclass(frozen=True)
class ExecutionStep:
    """A fully sized conversion step that can become an order intent."""
    symbol: str
    side: str
    price: float
    order_amount: float
    output_amount: float


class TriangularScanner:
    """Search one exchange for profitable three-leg conversion cycles."""

    def scan(
        self,
        exchange_name: str,
        client,
        settings: TriangularSettings,
        quote_capital: float,
    ) -> list[Opportunity]:
        """Scan the configured asset loops on one exchange and rank opportunities."""
        client.load_markets()
        symbols = client.supported_symbols()
        quote_cache: dict[str, Quote] = {}
        opportunities: list[Opportunity] = []

        for settlement in settings.settlement_assets:
            for asset_a, asset_b in permutations(settings.base_assets, 2):
                cycle = (settlement, asset_a, asset_b, settlement)
                opportunity = self._evaluate_cycle(
                    exchange_name=exchange_name,
                    client=client,
                    symbols=symbols,
                    quote_cache=quote_cache,
                    cycle=cycle,
                    starting_amount=quote_capital,
                    min_edge_bps=settings.min_edge_bps,
                )
                if opportunity:
                    opportunities.append(opportunity)

        opportunities.sort(key=lambda item: item.edge_bps, reverse=True)
        return opportunities[: settings.max_opportunities]

    def _evaluate_cycle(
        self,
        exchange_name: str,
        client,
        symbols: set[str],
        quote_cache: dict[str, Quote],
        cycle: tuple[str, str, str, str],
        starting_amount: float,
        min_edge_bps: float,
    ) -> Opportunity | None:
        """Simulate one full triangular loop and return it if the edge is positive enough."""
        fee_rate = client.taker_fee_bps / 10000.0
        execution_steps: list[ExecutionStep] = []
        amount = starting_amount

        for source, target in zip(cycle, cycle[1:]):
            step = self._resolve_step(client, symbols, quote_cache, source, target)
            if step is None:
                return None

            simulated = self._apply_step(step, amount, fee_rate)
            if simulated is None:
                return None

            execution_steps.append(simulated)
            amount = simulated.output_amount

        expected_pnl = amount - starting_amount
        edge_bps = (expected_pnl / starting_amount) * 10000.0
        if edge_bps < min_edge_bps:
            return None

        orders = tuple(
            OrderIntent(
                exchange=exchange_name,
                symbol=step.symbol,
                side=step.side,
                price=step.price,
                amount=step.order_amount,
                note="triangular leg",
            )
            for step in execution_steps
        )

        return Opportunity(
            strategy="triangular",
            venue=exchange_name,
            summary=f"{cycle[0]} -> {cycle[1]} -> {cycle[2]} -> {cycle[3]}",
            edge_bps=edge_bps,
            expected_pnl=expected_pnl,
            pnl_currency=cycle[0],
            orders=orders,
            metadata={"cycle": cycle},
        )

    def _resolve_step(
        self,
        client,
        symbols: set[str],
        quote_cache: dict[str, Quote],
        source: str,
        target: str,
    ) -> Step | None:
        """Map an asset conversion into the exchange symbol and side needed to trade it."""
        sell_symbol = f"{source}/{target}"
        if sell_symbol in symbols:
            quote = self._get_quote(client, quote_cache, sell_symbol)
            return Step(
                symbol=sell_symbol,
                side="sell",
                price=quote.bid,
                available_base=quote.bid_size,
            )

        buy_symbol = f"{target}/{source}"
        if buy_symbol in symbols:
            quote = self._get_quote(client, quote_cache, buy_symbol)
            return Step(
                symbol=buy_symbol,
                side="buy",
                price=quote.ask,
                available_base=quote.ask_size,
            )

        return None

    def _get_quote(self, client, cache: dict[str, Quote], symbol: str) -> Quote:
        """Fetch and cache top-of-book data so repeated legs do not refetch the same symbol."""
        if symbol not in cache:
            cache[symbol] = client.fetch_top_of_book(symbol)
        return cache[symbol]

    def _apply_step(
        self,
        step: Step,
        amount: float,
        fee_rate: float,
    ) -> ExecutionStep | None:
        """Apply one buy or sell step, including fee drag and top-of-book size limits."""
        if step.side == "buy":
            order_amount = amount / step.price
            if order_amount > step.available_base:
                return None
            output_amount = order_amount * (1.0 - fee_rate)
        else:
            order_amount = amount
            if order_amount > step.available_base:
                return None
            output_amount = order_amount * step.price * (1.0 - fee_rate)

        return ExecutionStep(
            symbol=step.symbol,
            side=step.side,
            price=step.price,
            order_amount=order_amount,
            output_amount=output_amount,
        )
