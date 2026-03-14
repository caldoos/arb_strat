"""Runtime state tracking and lightweight persistence for operators."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from arb_strat.config import StateSettings
from arb_strat.ledger import SQLiteLedger
from arb_strat.models import (
    BalanceSnapshot,
    ErrorRecord,
    ExecutionRecord,
    FillRecord,
    Opportunity,
    OrderStatusRecord,
)


class StateStore:
    """Track recent runtime state and persist snapshots/events to disk."""

    def __init__(self, settings: StateSettings) -> None:
        """Initialize in-memory state buffers and on-disk paths."""
        self.settings = settings
        self.directory = Path(settings.directory)
        self.snapshot_path = self.directory / settings.snapshot_file
        self.event_log_path = self.directory / settings.event_log_file
        self.database_path = self.directory / settings.database_file
        self._lock = Lock()

        self.recent_executions: deque[ExecutionRecord] = deque(maxlen=settings.max_recent_records)
        self.recent_errors: deque[ErrorRecord] = deque(maxlen=settings.max_recent_records)
        self.recent_order_statuses: deque[OrderStatusRecord] = deque(maxlen=settings.max_recent_records)
        self.recent_fills: deque[FillRecord] = deque(maxlen=settings.max_recent_records)
        self.last_balance_snapshots: dict[str, BalanceSnapshot] = {}
        self.open_orders: dict[str, OrderStatusRecord] = {}
        self.paper_pnl_by_currency: dict[str, float] = {}
        self.live_pnl_estimate_usd_by_day: dict[str, float] = {}
        self.execution_paused = False
        self.pause_reason = ""
        self.runtime: dict[str, object] = {}
        self.daily_summary: dict[str, object] = self._new_daily_summary_payload()
        self.ledger = SQLiteLedger(self.database_path) if self.settings.enabled else None

        if self.settings.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._write_snapshot()

    def register_execution_group(
        self,
        execution_group_id: str,
        opportunity: Opportunity,
        *,
        mode: str,
        total_notional: float,
    ) -> None:
        """Persist the parent execution-group record in the SQLite ledger."""
        if self.ledger is None:
            return
        self.ledger.register_execution_group(
            execution_group_id,
            opportunity,
            mode=mode,
            total_notional=total_notional,
        )

    def update_runtime(self, **values: object) -> None:
        """Update the top-level runtime snapshot fields."""
        with self._lock:
            self.runtime.update(values)
            self._write_snapshot()

    def set_execution_paused(self, paused: bool, reason: str = "") -> None:
        """Persist the current execution pause state."""
        with self._lock:
            self.execution_paused = paused
            self.pause_reason = reason
            self._write_event(
                {
                    "type": "pause_state",
                    "paused": paused,
                    "reason": reason,
                }
            )
            self._write_snapshot()

    def is_execution_paused(self) -> bool:
        """Return whether live execution is currently paused."""
        with self._lock:
            return self.execution_paused

    def record_execution(self, record: ExecutionRecord) -> None:
        """Append an execution record to memory and disk."""
        with self._lock:
            self.recent_executions.appendleft(record)
            execution_counts = self.daily_summary["execution_status_counts"]
            execution_counts[record.status] = execution_counts.get(record.status, 0) + 1
            if record.status == "paper_executed":
                self.paper_pnl_by_currency[record.pnl_currency] = (
                    self.paper_pnl_by_currency.get(record.pnl_currency, 0.0)
                    + record.expected_pnl
                )
            live_pnl_delta = self._live_pnl_delta(record)
            if live_pnl_delta != 0.0:
                day_key = record.timestamp[:10]
                self.live_pnl_estimate_usd_by_day[day_key] = (
                    self.live_pnl_estimate_usd_by_day.get(day_key, 0.0) + live_pnl_delta
                )
            expected_pnl = self.daily_summary["expected_pnl_by_currency"]
            expected_pnl[record.pnl_currency] = (
                expected_pnl.get(record.pnl_currency, 0.0) + record.expected_pnl
            )
            if self.ledger and record.execution_group_id:
                self.ledger.update_execution_group_status(
                    record.execution_group_id,
                    status=record.status,
                )
            self._write_event({"type": "execution", **asdict(record)})
            self._write_snapshot()

    def record_order_status(self, record: OrderStatusRecord) -> None:
        """Append an order status record and refresh the open-order view."""
        with self._lock:
            self.recent_order_statuses.appendleft(record)
            key = f"{record.exchange}:{record.order_id}"
            if record.status in {"open", "partially_filled"}:
                self.open_orders[key] = record
            else:
                self.open_orders.pop(key, None)
            if self.ledger:
                self.ledger.record_order_status(record)
            self._write_event({"type": "order_status", **asdict(record)})
            self._write_snapshot()

    def record_fill(self, record: FillRecord) -> None:
        """Append a normalized fill record to memory and disk."""
        with self._lock:
            self.recent_fills.appendleft(record)
            if self.ledger:
                self.ledger.record_fill(record)
            self._write_event({"type": "fill", **asdict(record)})
            self._write_snapshot()

    def record_error(self, source: str, message: str) -> None:
        """Append a runtime error record to memory and disk."""
        with self._lock:
            record = ErrorRecord.now(source=source, message=message)
            self.recent_errors.appendleft(record)
            self.daily_summary["error_count"] = int(self.daily_summary["error_count"]) + 1
            error_sources = self.daily_summary["error_sources"]
            error_sources[source] = error_sources.get(source, 0) + 1
            self._write_event({"type": "error", **asdict(record)})
            self._write_snapshot()

    def record_balances(self, exchange: str, balances: dict[str, float]) -> BalanceSnapshot:
        """Store the latest balance snapshot for one exchange."""
        snapshot = BalanceSnapshot.now(exchange=exchange, balances=balances)
        with self._lock:
            self.last_balance_snapshots[exchange] = snapshot
            self._write_event({"type": "balances", **asdict(snapshot)})
            self._write_snapshot()
        return snapshot

    def record_opportunities(self, opportunities: list[Opportunity]) -> None:
        """Persist a lightweight summary of the most recent opportunity batch."""
        with self._lock:
            summary = [
                {
                    "strategy": item.strategy,
                    "venue": item.venue,
                    "edge_bps": item.edge_bps,
                    "expected_pnl": item.expected_pnl,
                    "pnl_currency": item.pnl_currency,
                    "summary": item.summary,
                }
                for item in opportunities[:10]
            ]
            self.runtime["last_opportunities"] = summary
            self.runtime["last_opportunity_count"] = len(opportunities)
            self.daily_summary["cycles"] = int(self.daily_summary["cycles"]) + 1
            self.daily_summary["opportunity_batches"] = (
                int(self.daily_summary["opportunity_batches"]) + (1 if opportunities else 0)
            )
            self.daily_summary["opportunities_total"] = (
                int(self.daily_summary["opportunities_total"]) + len(opportunities)
            )
            by_strategy = self.daily_summary["opportunities_by_strategy"]
            by_venue = self.daily_summary["opportunities_by_venue"]
            for item in opportunities:
                by_strategy[item.strategy] = by_strategy.get(item.strategy, 0) + 1
                by_venue[item.venue] = by_venue.get(item.venue, 0) + 1
            self._write_snapshot()

    def recent_execution_records(self) -> list[ExecutionRecord]:
        """Return a copy of the in-memory recent execution records."""
        with self._lock:
            return list(self.recent_executions)

    def recent_error_records(self) -> list[ErrorRecord]:
        """Return a copy of the in-memory recent error records."""
        with self._lock:
            return list(self.recent_errors)

    def recent_order_records(self) -> list[OrderStatusRecord]:
        """Return a copy of recently seen order lifecycle records."""
        with self._lock:
            return list(self.recent_order_statuses)

    def recent_fill_records(self) -> list[FillRecord]:
        """Return a copy of recently recorded fills."""
        with self._lock:
            return list(self.recent_fills)

    def open_order_records(self) -> list[OrderStatusRecord]:
        """Return the current in-memory open-order view."""
        with self._lock:
            return list(self.open_orders.values())

    def balance_snapshots(self) -> dict[str, BalanceSnapshot]:
        """Return a copy of the latest balance snapshot per exchange."""
        with self._lock:
            return dict(self.last_balance_snapshots)

    def pnl_snapshot(self) -> dict[str, float]:
        """Return accumulated simulated pnl totals by currency."""
        with self._lock:
            return dict(self.paper_pnl_by_currency)

    def current_live_pnl_estimate_usd(self) -> float:
        """Return the current UTC-day live pnl estimate used for kill-switch checks."""
        with self._lock:
            day_key = datetime.now(timezone.utc).date().isoformat()
            return float(self.live_pnl_estimate_usd_by_day.get(day_key, 0.0))

    def open_notional_estimate(self) -> float:
        """Return the current notional tied up in open orders."""
        with self._lock:
            return float(sum(record.amount * record.price for record in self.open_orders.values()))

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-friendly runtime snapshot."""
        with self._lock:
            return self._snapshot_payload()

    def daily_summary_snapshot(self) -> dict[str, object]:
        """Return a copy of the current daily summary counters."""
        with self._lock:
            return json.loads(json.dumps(self.daily_summary))

    def realized_pnl_summary(self) -> dict[str, object]:
        """Return realized-pnl aggregates from the SQLite execution ledger."""
        if self.ledger is None:
            return {"totals": {}, "today": {}, "recent": []}
        return self.ledger.realized_pnl_summary()

    def mark_daily_summary_sent(self, sent_at: datetime | None = None) -> None:
        """Reset the daily summary window after a report has been sent."""
        with self._lock:
            when = sent_at or datetime.now(timezone.utc)
            self.runtime["last_daily_summary_sent_at"] = when.isoformat()
            self.daily_summary = self._new_daily_summary_payload()
            self._write_snapshot()

    def last_daily_summary_sent_at(self) -> str | None:
        """Return the timestamp of the last sent daily summary if present."""
        with self._lock:
            value = self.runtime.get("last_daily_summary_sent_at")
            return str(value) if value else None

    def _snapshot_payload(self) -> dict[str, object]:
        """Assemble the current JSON-friendly snapshot payload."""
        return {
            "runtime": dict(self.runtime),
            "execution_paused": self.execution_paused,
            "pause_reason": self.pause_reason,
            "recent_executions": [asdict(record) for record in self.recent_executions],
            "recent_errors": [asdict(record) for record in self.recent_errors],
            "recent_order_statuses": [asdict(record) for record in self.recent_order_statuses],
            "recent_fills": [asdict(record) for record in self.recent_fills],
            "open_orders": [asdict(record) for record in self.open_orders.values()],
            "balances": {
                exchange: asdict(snapshot)
                for exchange, snapshot in self.last_balance_snapshots.items()
            },
            "paper_pnl_by_currency": dict(self.paper_pnl_by_currency),
            "live_pnl_estimate_usd_by_day": dict(self.live_pnl_estimate_usd_by_day),
            "database_path": str(self.database_path),
            "daily_summary": dict(self.daily_summary),
        }

    def _write_snapshot(self) -> None:
        """Persist the latest runtime snapshot to disk."""
        if not self.settings.enabled:
            return
        self.snapshot_path.write_text(
            json.dumps(self._snapshot_payload(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _write_event(self, payload: dict[str, object]) -> None:
        """Append one event record to the JSONL event log."""
        if not self.settings.enabled:
            return
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _new_daily_summary_payload(self) -> dict[str, object]:
        """Create a fresh daily-summary accumulator payload."""
        return {
            "window_started_at": datetime.now(timezone.utc).isoformat(),
            "cycles": 0,
            "opportunity_batches": 0,
            "opportunities_total": 0,
            "opportunities_by_strategy": {},
            "opportunities_by_venue": {},
            "execution_status_counts": {},
            "expected_pnl_by_currency": {},
            "error_count": 0,
            "error_sources": {},
            "last_sent_at": None,
        }

    def _live_pnl_delta(self, record: ExecutionRecord) -> float:
        """Estimate daily live pnl impact from execution outcomes for safety limits."""
        if record.mode != "live":
            return 0.0
        if record.pnl_currency not in {"USD", "USDT"}:
            return 0.0
        if record.status == "live_submitted":
            return record.expected_pnl
        if record.status in {"live_partial_failure", "live_error"}:
            return -abs(record.expected_pnl)
        return 0.0
