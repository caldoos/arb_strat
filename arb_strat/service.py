"""Main orchestration layer that runs scanners and optional execution."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arb_strat.config import AppConfig
from arb_strat.execution.controller import ExecutionController
from arb_strat.exchanges.factory import build_exchange_clients
from arb_strat.market_data import MarketDataHub
from arb_strat.models import Opportunity
from arb_strat.notifications.telegram import TelegramNotifier
from arb_strat.state import StateStore
from arb_strat.strategies.cross_exchange import CrossExchangeScanner
from arb_strat.strategies.triangular import TriangularScanner

logger = logging.getLogger(__name__)


class ArbitrageBot:
    """Coordinate exchange clients, strategy scanners, and execution handlers."""

    def __init__(
        self,
        config: AppConfig,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        """Initialize exchange clients and strategy/execution components."""
        self.config = config
        self.notifier = notifier
        self.clients = build_exchange_clients(config.exchanges)
        self.triangular = TriangularScanner()
        self.cross_exchange = CrossExchangeScanner()
        self.state_store = StateStore(config.state)
        self.execution_controller = ExecutionController(config, self.clients, self.state_store)
        self.market_data = MarketDataHub(config, self.clients)
        if self.config.market_data.enabled:
            for client in self.clients.values():
                client.set_quote_cache(
                    self.market_data.cache,
                    rest_fallback=self.config.market_data.rest_fallback,
                )
        self.cycles = 0
        self.started_at = datetime.now(timezone.utc)
        self.last_cycle_completed_at: datetime | None = None
        self.last_opportunities: list[Opportunity] = []
        self.current_strategy = "all"
        self.execute_enabled = False
        self.live_enabled = False
        self._heartbeat_enabled_override: bool | None = None
        self._state_lock = Lock()
        self._daily_summary_zone = self._resolve_daily_summary_zone()
        self.state_store.update_runtime(
            strategy=self.current_strategy,
            mode=self._mode_label(),
            cycles=self.cycles,
            market_data="websocket" if self.config.market_data.enabled else "rest",
        )

    def run(self, strategy: str, once: bool, execute: bool, live: bool) -> int:
        """Run one or more scan cycles and optionally execute discovered plans."""
        with self._state_lock:
            self.current_strategy = strategy
            self.execute_enabled = execute
            self.live_enabled = live
        self.market_data.start()
        if self.config.market_data.enabled and self.config.market_data.warmup_seconds > 0:
            time.sleep(self.config.market_data.warmup_seconds)
        self._notify_startup(strategy=strategy, execute=execute, live=live)
        while True:
            self.execution_controller.begin_cycle()
            self.cycles += 1
            opportunities = self.run_once(strategy)
            with self._state_lock:
                self.last_opportunities = list(opportunities)
                self.last_cycle_completed_at = datetime.now(timezone.utc)
                self.state_store.update_runtime(
                    strategy=self.current_strategy,
                    mode=self._mode_label(),
                    cycles=self.cycles,
                    last_cycle=self._format_datetime(self.last_cycle_completed_at),
                    last_opportunity_count=len(self.last_opportunities),
                    heartbeat="on" if self._heartbeat_enabled() else "off",
                    execution_paused=self.state_store.is_execution_paused(),
                )
                self.state_store.record_opportunities(opportunities)
            self._emit(opportunities)
            self._notify_opportunities(opportunities)
            self._notify_heartbeat(strategy=strategy)

            if execute and opportunities:
                for opportunity in opportunities:
                    record = self.execution_controller.execute(opportunity, live=live)
                    if record.status not in {"paper_executed", "live_submitted"}:
                        logger.warning(
                            "Execution skipped or failed for %s: %s",
                            opportunity.summary,
                            record.reason or record.status,
                        )

            self._maybe_send_daily_summary()

            if once:
                self._notify_shutdown()
                return 0

            time.sleep(self.config.poll_interval_seconds)

    def run_once(self, strategy: str) -> list[Opportunity]:
        """Execute a single scan cycle for the selected strategy scope."""
        opportunities: list[Opportunity] = []

        if strategy in ("triangular", "all") and self.config.triangular.enabled:
            exchange_names = set(self.config.triangular.exchanges)
            for exchange_name, client in self.clients.items():
                if exchange_name not in exchange_names:
                    continue
                try:
                    opportunities.extend(
                        self.triangular.scan(
                            exchange_name=exchange_name,
                            client=client,
                            settings=self.config.triangular,
                            quote_capital=self.config.quote_capital,
                        )
                    )
                except Exception as exc:
                    self.state_store.record_error("triangular_scan", str(exc))
                    logger.warning(
                        "Skipping triangular scan on %s due to error: %s",
                        exchange_name,
                        exc,
                    )

        if strategy in ("cross", "all") and self.config.cross_exchange.enabled:
            try:
                opportunities.extend(
                    self.cross_exchange.scan(
                        clients=self.clients,
                        settings=self.config.cross_exchange,
                        quote_capital=self.config.quote_capital,
                    )
                )
            except Exception as exc:
                self.state_store.record_error("cross_exchange_scan", str(exc))
                logger.warning("Skipping cross-exchange scan due to error: %s", exc)

        opportunities.sort(key=lambda item: item.edge_bps, reverse=True)
        return opportunities

    def close(self) -> None:
        """Close any exchange resources that expose a shutdown hook."""
        self.market_data.stop()
        for client in self.clients.values():
            client.close()

    def format_status(self) -> str:
        """Return a compact runtime status summary for Telegram commands."""
        with self._state_lock:
            strategy = self.current_strategy
            mode = self._mode_label()
            cycles = self.cycles
            last_cycle = self._format_datetime(self.last_cycle_completed_at)
            last_count = len(self.last_opportunities)
            heartbeat = "on" if self._heartbeat_enabled() else "off"
            open_orders = len(self.state_store.open_order_records())

        return (
            "arb_strat status\n"
            f"strategy: {strategy}\n"
            f"mode: {mode}\n"
            f"cycles: {cycles}\n"
            f"last cycle: {last_cycle}\n"
            f"last opportunities: {last_count}\n"
            f"open orders: {open_orders}\n"
            f"heartbeat: {heartbeat}\n"
            f"market data: {'websocket' if self.config.market_data.enabled else 'rest'}\n"
            f"execution paused: {'yes' if self.state_store.is_execution_paused() else 'no'}"
        )

    def format_mode(self) -> str:
        """Return the current strategy and execution mode."""
        with self._state_lock:
            strategy = self.current_strategy
            mode = self._mode_label()
            dry_run = self.config.dry_run

        return (
            "arb_strat mode\n"
            f"strategy: {strategy}\n"
            f"execution: {mode}\n"
            f"dry_run flag: {dry_run}"
        )

    def format_last_opportunities(self, limit: int = 5) -> str:
        """Return the most recent in-memory opportunities seen by the bot."""
        with self._state_lock:
            opportunities = list(self.last_opportunities[:limit])

        if not opportunities:
            return "No opportunities recorded yet."

        lines = ["Last opportunities"]
        for opportunity in opportunities:
            lines.append(
                (
                    f"{opportunity.strategy} | {opportunity.venue} | "
                    f"{opportunity.edge_bps:.2f} bps | "
                    f"pnl {opportunity.expected_pnl:.6f} {opportunity.pnl_currency}"
                )
            )
        return "\n".join(lines)

    def format_balances(self) -> str:
        """Fetch current balances from each configured exchange for operator inspection."""
        sections: list[str] = ["Exchange balances"]
        for exchange_name, client in sorted(self.clients.items()):
            try:
                balances = client.fetch_balance()
                self.state_store.record_balances(exchange_name, balances)
            except Exception as exc:
                self.state_store.record_error("balances", f"{exchange_name}: {exc}")
                sections.append(f"{exchange_name}: error fetching balances ({exc})")
                continue

            non_zero = [
                f"{asset}={amount:.8f}"
                for asset, amount in sorted(balances.items())
                if abs(amount) > 0
            ]
            if not non_zero:
                sections.append(f"{exchange_name}: no non-zero balances")
                continue

            preview = ", ".join(non_zero[:8])
            if len(non_zero) > 8:
                preview += ", ..."
            sections.append(f"{exchange_name}: {preview}")

        return "\n".join(sections)

    def format_positions(self) -> str:
        """Show the latest known wallet snapshot per exchange without refetching."""
        snapshots = self.state_store.balance_snapshots()
        if not snapshots:
            return "No balance snapshots recorded yet. Use /balances first."

        sections = ["Latest wallet snapshots"]
        for exchange_name, snapshot in sorted(snapshots.items()):
            non_zero = [
                f"{asset}={amount:.8f}"
                for asset, amount in sorted(snapshot.balances.items())
                if abs(amount) > 0
            ]
            lines = ", ".join(non_zero[:8]) if non_zero else "no non-zero balances"
            if len(non_zero) > 8:
                lines += ", ..."
            sections.append(f"{exchange_name} @ {snapshot.timestamp}: {lines}")
        return "\n".join(sections)

    def format_orders(self, limit: int = 5) -> str:
        """Show recent execution records from paper/live runs."""
        records = self.state_store.recent_execution_records()[:limit]
        if not records:
            return "No execution records yet."

        lines = ["Recent executions"]
        for record in records:
            lines.append(
                (
                    f"{record.timestamp} | {record.mode} | {record.status} | "
                    f"{record.strategy} | {record.venue} | "
                    f"{record.edge_bps:.2f} bps | {record.reason or record.summary}"
                )
            )
        return "\n".join(lines)

    def format_open_orders(self, refresh: bool = True, limit: int = 10) -> str:
        """Show currently open orders, optionally refreshing from exchanges first."""
        if refresh:
            self.execution_controller.fetch_open_orders()
        records = self.state_store.open_order_records()[:limit]
        if not records:
            return "No open orders."

        lines = ["Open orders"]
        for record in records:
            lines.append(
                (
                    f"{record.exchange} | {record.symbol} | {record.side} | "
                    f"{record.status} | filled {record.filled:.8f} / {record.amount:.8f} | "
                    f"id {record.order_id}"
                )
            )
        return "\n".join(lines)

    def format_fills(self, limit: int = 10) -> str:
        """Show recent normalized fill records."""
        records = self.state_store.recent_fill_records()[:limit]
        if not records:
            return "No fills recorded yet."

        lines = ["Recent fills"]
        for record in records:
            fee_text = (
                f" | fee {record.fee_cost:.8f} {record.fee_currency}"
                if record.fee_currency
                else ""
            )
            lines.append(
                (
                    f"{record.exchange} | {record.symbol} | {record.side} | "
                    f"filled {record.filled:.8f} @ {record.average_price:.8f}{fee_text}"
                )
            )
        return "\n".join(lines)

    def format_errors(self, limit: int = 5) -> str:
        """Show recent recorded scan/execution/runtime errors."""
        records = self.state_store.recent_error_records()[:limit]
        if not records:
            return "No recent errors."

        lines = ["Recent errors"]
        for record in records:
            lines.append(f"{record.timestamp} | {record.source} | {record.message}")
        return "\n".join(lines)

    def format_pnl(self) -> str:
        """Show accumulated simulated pnl totals by currency."""
        pnl = self.state_store.pnl_snapshot()
        if not pnl:
            return "No simulated pnl recorded yet."

        lines = ["Simulated pnl totals"]
        for currency, amount in sorted(pnl.items()):
            lines.append(f"{currency}: {amount:.8f}")
        return "\n".join(lines)

    def format_risk(self) -> str:
        """Return the currently configured execution/risk rules."""
        return self.execution_controller.risk_summary()

    def format_daily_summary(self) -> str:
        """Return the current rolling daily summary window in operator-friendly text."""
        summary = self.state_store.daily_summary_snapshot()
        lines = [
            "Daily summary",
            f"window started: {summary['window_started_at']}",
            f"timezone: {self.config.telegram.daily_summary_timezone}",
            f"cycles: {summary['cycles']}",
            f"opportunity batches: {summary['opportunity_batches']}",
            f"opportunities total: {summary['opportunities_total']}",
        ]

        by_strategy = summary.get("opportunities_by_strategy", {})
        if by_strategy:
            strategy_text = ", ".join(
                f"{name}={count}" for name, count in sorted(by_strategy.items())
            )
            lines.append(f"by strategy: {strategy_text}")

        execution_counts = summary.get("execution_status_counts", {})
        if execution_counts:
            exec_text = ", ".join(
                f"{name}={count}" for name, count in sorted(execution_counts.items())
            )
            lines.append(f"executions: {exec_text}")

        pnl = summary.get("expected_pnl_by_currency", {})
        if pnl:
            pnl_text = ", ".join(
                f"{currency}={amount:.6f}" for currency, amount in sorted(pnl.items())
            )
            lines.append(f"expected pnl: {pnl_text}")

        lines.append(f"errors: {summary['error_count']}")
        return "\n".join(lines)

    def pause_execution(self) -> str:
        """Pause live execution while allowing the scanners to continue running."""
        return self.execution_controller.pause("paused by operator command")

    def resume_execution(self) -> str:
        """Resume live execution after an operator pause."""
        return self.execution_controller.resume()

    def handle_heartbeat_command(self, action: str | None) -> str:
        """Return or change the Telegram heartbeat state at runtime."""
        normalized = (action or "status").lower()
        if normalized not in {"on", "off", "status"}:
            return "Usage: /heartbeat [on|off|status]"

        if normalized == "on":
            self._heartbeat_enabled_override = True
        elif normalized == "off":
            self._heartbeat_enabled_override = False

        state = "on" if self._heartbeat_enabled() else "off"
        return f"Heartbeat is {state}."

    def _emit(self, opportunities: list[Opportunity]) -> None:
        """Log discovered opportunities in a compact operator-friendly format."""
        if not opportunities:
            logger.info("No opportunities found.")
            return

        for opportunity in opportunities:
            logger.info(
                "%s | %s | %.2f bps | pnl %.6f %s | %s",
                opportunity.strategy,
                opportunity.venue,
                opportunity.edge_bps,
                opportunity.expected_pnl,
                opportunity.pnl_currency,
                opportunity.summary,
            )

    def _notify_startup(self, strategy: str, execute: bool, live: bool) -> None:
        """Send a startup notification when Telegram alerts are enabled."""
        if not self._notifications_enabled() or not self.config.telegram.notify_on_startup:
            return

        mode = "live" if live and execute else "paper" if execute else "scan"
        self.notifier.send_notification(
            f"arb_strat started | strategy={strategy} | mode={mode} | dry_run={self.config.dry_run}"
        )

    def _notify_shutdown(self) -> None:
        """Send a shutdown notification for one-shot runs and graceful exits."""
        if not self._notifications_enabled() or not self.config.telegram.notify_on_shutdown:
            return

        self.notifier.send_notification(
            f"arb_strat finished | cycles={self.cycles}"
        )

    def _notify_opportunities(self, opportunities: list[Opportunity]) -> None:
        """Send high-signal Telegram alerts for opportunities above the notify threshold."""
        if (
            not self._notifications_enabled()
            or not self.config.telegram.notify_on_opportunity
            or not opportunities
        ):
            return

        for opportunity in opportunities:
            if opportunity.edge_bps < self.config.telegram.min_edge_bps:
                continue
            self.notifier.send_notification(
                (
                    f"{opportunity.strategy} | {opportunity.venue} | "
                    f"{opportunity.edge_bps:.2f} bps | "
                    f"pnl {opportunity.expected_pnl:.6f} {opportunity.pnl_currency} | "
                    f"{opportunity.summary}"
                )
            )

    def _notify_heartbeat(self, strategy: str) -> None:
        """Send a periodic heartbeat for long-running sessions when configured."""
        if (
            not self._notifications_enabled()
            or not self._heartbeat_enabled()
            or self.config.telegram.heartbeat_interval_cycles <= 0
        ):
            return

        if self.cycles % self.config.telegram.heartbeat_interval_cycles != 0:
            return

        self.notifier.send_notification(
            f"arb_strat heartbeat | strategy={strategy} | cycles={self.cycles}"
        )

    def _maybe_send_daily_summary(self) -> None:
        """Send one daily summary report when the configured local summary hour is reached."""
        if not self._notifications_enabled() or not self.config.telegram.daily_summary_enabled:
            return

        now_utc = datetime.now(timezone.utc)
        now = now_utc.astimezone(self._daily_summary_zone)
        if now.hour < self.config.telegram.daily_summary_hour_utc:
            return

        last_sent = self.state_store.last_daily_summary_sent_at()
        if last_sent:
            try:
                last_sent_dt = datetime.fromisoformat(last_sent)
            except ValueError:
                last_sent_dt = None
            if last_sent_dt and last_sent_dt.astimezone(self._daily_summary_zone).date() == now.date():
                return

        summary = self.state_store.daily_summary_snapshot()
        if int(summary["cycles"]) <= 0:
            return

        self.notifier.send_notification(self.format_daily_summary())
        self.state_store.mark_daily_summary_sent(now_utc)

    def _notifications_enabled(self) -> bool:
        """Return True when Telegram notifications are configured and enabled."""
        return bool(self.notifier and self.config.telegram.enabled and self.notifier.is_enabled())

    def _heartbeat_enabled(self) -> bool:
        """Return the effective heartbeat setting after applying runtime overrides."""
        if self._heartbeat_enabled_override is not None:
            return self._heartbeat_enabled_override
        return self.config.telegram.heartbeat_enabled

    def _mode_label(self) -> str:
        """Return a short human-readable execution mode label."""
        if self.live_enabled and self.execute_enabled:
            return "live"
        if self.execute_enabled:
            return "paper"
        return "scan"

    def _format_datetime(self, value: datetime | None) -> str:
        """Format a UTC datetime for operator-facing status messages."""
        if value is None:
            return "not yet"
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")

    def _resolve_daily_summary_zone(self) -> ZoneInfo:
        """Resolve the configured summary timezone, falling back to UTC if invalid."""
        configured = self.config.telegram.daily_summary_timezone
        if configured == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(configured)
        except ZoneInfoNotFoundError:
            fixed_offset_fallbacks = {
                "Asia/Singapore": timezone(timedelta(hours=8), name="Asia/Singapore"),
            }
            if configured in fixed_offset_fallbacks:
                logger.warning(
                    "Timezone data unavailable for %s, using fixed-offset fallback",
                    configured,
                )
                return fixed_offset_fallbacks[configured]
            logger.warning(
                "Unknown daily summary timezone %s, falling back to UTC",
                configured,
            )
            return timezone.utc
