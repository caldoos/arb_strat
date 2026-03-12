"""Execution controller that applies risk rules and records outcomes."""

from __future__ import annotations

import time

from arb_strat.config import AppConfig
from arb_strat.execution.live import LiveExecutionError, LiveExecutor
from arb_strat.execution.paper import PaperExecutor
from arb_strat.execution.risk import PreparedOpportunity, RiskManager, RiskViolation
from arb_strat.models import ExecutionRecord, FillRecord, Opportunity, OrderStatusRecord
from arb_strat.state import StateStore


class ExecutionController:
    """Coordinate risk checks, execution backends, and runtime state updates."""

    def __init__(
        self,
        config: AppConfig,
        clients: dict[str, object],
        state_store: StateStore,
    ) -> None:
        """Initialize live/paper executors and the shared risk manager."""
        self.config = config
        self.clients = clients
        self.state_store = state_store
        self.paper_executor = PaperExecutor()
        self.live_executor = LiveExecutor(clients)
        self.risk = RiskManager(config, state_store)
        self.live_orders_executed_this_cycle = 0

    def begin_cycle(self) -> None:
        """Reset per-cycle live execution counters."""
        self.live_orders_executed_this_cycle = 0

    def execute(self, opportunity: Opportunity, *, live: bool) -> ExecutionRecord:
        """Execute or reject one opportunity and persist the resulting record."""
        mode = "live" if live else "paper"
        try:
            prepared = self.risk.prepare(opportunity, self.clients, live=live)
        except RiskViolation as exc:
            record = self._record_rejection(opportunity, mode=mode, reason=str(exc))
            return record

        if live:
            next_order_count = self.live_orders_executed_this_cycle + len(prepared.opportunity.orders)
            if next_order_count > self.config.risk.max_live_orders_per_cycle:
                return self._record_rejection(
                    prepared.opportunity,
                    mode=mode,
                    reason="per-cycle live order limit reached",
                )

        try:
            if live:
                responses = self.live_executor.execute(prepared.opportunity)
                self._record_submitted_orders(prepared, responses)
                self._reconcile_live_orders(prepared, responses)
                self.live_orders_executed_this_cycle += len(prepared.opportunity.orders)
                record = ExecutionRecord.now(
                    mode=mode,
                    strategy=prepared.opportunity.strategy,
                    venue=prepared.opportunity.venue,
                    summary=prepared.opportunity.summary,
                    status="live_submitted",
                    edge_bps=prepared.opportunity.edge_bps,
                    expected_pnl=prepared.opportunity.expected_pnl,
                    pnl_currency=prepared.opportunity.pnl_currency,
                    order_count=len(prepared.opportunity.orders),
                    metadata={
                        "total_notional": prepared.total_notional,
                        "responses": responses,
                    },
                )
            else:
                self.paper_executor.execute(prepared.opportunity)
                record = ExecutionRecord.now(
                    mode=mode,
                    strategy=prepared.opportunity.strategy,
                    venue=prepared.opportunity.venue,
                    summary=prepared.opportunity.summary,
                    status="paper_executed",
                    edge_bps=prepared.opportunity.edge_bps,
                    expected_pnl=prepared.opportunity.expected_pnl,
                    pnl_currency=prepared.opportunity.pnl_currency,
                    order_count=len(prepared.opportunity.orders),
                    metadata={"total_notional": prepared.total_notional},
                )
            self.risk.register_success()
            self.state_store.record_execution(record)
            return record
        except LiveExecutionError as exc:
            if exc.responses and self.config.risk.cancel_on_partial_failure:
                self._attempt_cancel_open_legs(prepared, exc.responses)
            if exc.responses and self.config.risk.pause_on_partial_fill:
                self.risk.pause(
                    "paused immediately due to partial live fill / leg mismatch"
                )
            self.risk.register_failure(str(exc))
            self.state_store.record_error("live_execution", str(exc))
            record = ExecutionRecord.now(
                mode=mode,
                strategy=prepared.opportunity.strategy,
                venue=prepared.opportunity.venue,
                summary=prepared.opportunity.summary,
                status="live_partial_failure",
                edge_bps=prepared.opportunity.edge_bps,
                expected_pnl=prepared.opportunity.expected_pnl,
                pnl_currency=prepared.opportunity.pnl_currency,
                order_count=len(prepared.opportunity.orders),
                reason=str(exc),
                metadata={
                    "total_notional": prepared.total_notional,
                    "responses": exc.responses,
                },
            )
            self.state_store.record_execution(record)
            return record
        except Exception as exc:
            self.risk.register_failure(str(exc))
            self.state_store.record_error(f"{mode}_execution", str(exc))
            record = ExecutionRecord.now(
                mode=mode,
                strategy=prepared.opportunity.strategy,
                venue=prepared.opportunity.venue,
                summary=prepared.opportunity.summary,
                status=f"{mode}_error",
                edge_bps=prepared.opportunity.edge_bps,
                expected_pnl=prepared.opportunity.expected_pnl,
                pnl_currency=prepared.opportunity.pnl_currency,
                order_count=len(prepared.opportunity.orders),
                reason=str(exc),
                metadata={"total_notional": prepared.total_notional},
            )
            self.state_store.record_execution(record)
            return record

    def pause(self, reason: str) -> str:
        """Pause live execution and persist the operator reason."""
        self.risk.pause(reason)
        return "Execution paused."

    def resume(self) -> str:
        """Resume live execution and reset the failure counter."""
        self.risk.resume()
        return "Execution resumed."

    def risk_summary(self) -> str:
        """Return the current configured execution rules."""
        return self.risk.summary()

    def fetch_open_orders(self) -> list[OrderStatusRecord]:
        """Fetch fresh open orders from exchanges and record them in state."""
        records: list[OrderStatusRecord] = []
        for exchange_name, client in self.clients.items():
            try:
                orders = client.fetch_open_orders()
            except Exception as exc:
                self.state_store.record_error("open_orders", f"{exchange_name}: {exc}")
                continue

            for order in orders:
                record = self._to_order_status_record(exchange_name, order)
                self.state_store.record_order_status(record)
                records.append(record)
        return records

    def _record_rejection(self, opportunity: Opportunity, *, mode: str, reason: str) -> ExecutionRecord:
        """Persist a rejected opportunity record."""
        record = ExecutionRecord.now(
            mode=mode,
            strategy=opportunity.strategy,
            venue=opportunity.venue,
            summary=opportunity.summary,
            status="rejected",
            edge_bps=opportunity.edge_bps,
            expected_pnl=opportunity.expected_pnl,
            pnl_currency=opportunity.pnl_currency,
            order_count=len(opportunity.orders),
            reason=reason,
        )
        self.state_store.record_execution(record)
        return record

    def _record_submitted_orders(
        self,
        prepared: PreparedOpportunity,
        responses: list[dict],
    ) -> None:
        """Record initial submitted order responses as order-status snapshots."""
        for order, response in zip(prepared.opportunity.orders, responses):
            record = self._to_order_status_record(order.exchange, response, fallback_order=order)
            self.state_store.record_order_status(record)
            fill = self._to_fill_record(order.exchange, response, fallback_order=order)
            if fill is not None:
                self.state_store.record_fill(fill)

    def _reconcile_live_orders(
        self,
        prepared: PreparedOpportunity,
        responses: list[dict],
    ) -> None:
        """Poll exchange order state for submitted live orders and store the results."""
        if not self.config.risk.reconcile_live_orders:
            return

        for attempt in range(self.config.risk.reconciliation_max_attempts):
            all_terminal = True
            for order, response in zip(prepared.opportunity.orders, responses):
                order_id = str(response.get("id") or "")
                if not order_id:
                    continue
                try:
                    latest = self.clients[order.exchange].fetch_order(order_id, order.symbol)
                except Exception as exc:
                    self.state_store.record_error(
                        "order_reconciliation",
                        f"{order.exchange} {order.symbol} {order_id}: {exc}",
                    )
                    all_terminal = False
                    continue

                record = self._to_order_status_record(order.exchange, latest, fallback_order=order)
                self.state_store.record_order_status(record)
                fill = self._to_fill_record(order.exchange, latest, fallback_order=order)
                if fill is not None:
                    self.state_store.record_fill(fill)
                if record.status in {"open", "partially_filled"}:
                    all_terminal = False

            if all_terminal:
                return
            if attempt < self.config.risk.reconciliation_max_attempts - 1:
                time.sleep(self.config.risk.reconciliation_poll_seconds)

    def _attempt_cancel_open_legs(
        self,
        prepared: PreparedOpportunity,
        responses: list[dict],
    ) -> None:
        """Attempt to cancel any already-accepted live legs after a later leg fails."""
        for order, response in zip(prepared.opportunity.orders, responses):
            order_id = str(response.get("id") or "")
            if not order_id:
                continue
            try:
                latest = self.clients[order.exchange].fetch_order(order_id, order.symbol)
                record = self._to_order_status_record(order.exchange, latest, fallback_order=order)
                self.state_store.record_order_status(record)
                if record.status in {"open", "partially_filled"}:
                    cancel_response = self.clients[order.exchange].cancel_order(order_id, order.symbol)
                    cancel_record = self._to_order_status_record(
                        order.exchange,
                        cancel_response,
                        fallback_order=order,
                    )
                    self.state_store.record_order_status(cancel_record)
            except Exception as exc:
                self.state_store.record_error(
                    "cancel_recovery",
                    f"{order.exchange} {order.symbol} {order_id}: {exc}",
                )

    def _to_order_status_record(
        self,
        exchange: str,
        payload: dict,
        *,
        fallback_order=None,
    ) -> OrderStatusRecord:
        """Normalize an exchange order payload into the internal status record."""
        status = str(payload.get("status") or "unknown").lower()
        status = {
            "partially_filled": "partially_filled",
            "partial": "partially_filled",
            "closed": "filled",
            "canceled": "canceled",
            "cancelled": "canceled",
            "open": "open",
            "filled": "filled",
            "rejected": "rejected",
        }.get(status, status)

        symbol = str(payload.get("symbol") or getattr(fallback_order, "symbol", ""))
        side = str(payload.get("side") or getattr(fallback_order, "side", ""))
        amount = float(payload.get("amount") or getattr(fallback_order, "amount", 0.0))
        price = float(payload.get("price") or getattr(fallback_order, "price", 0.0))
        filled = float(payload.get("filled") or 0.0)
        remaining_raw = payload.get("remaining")
        remaining = float(remaining_raw) if remaining_raw is not None else max(0.0, amount - filled)
        timestamp_value = payload.get("datetime") or payload.get("timestamp") or ""
        timestamp = str(timestamp_value)
        if timestamp.isdigit():
            timestamp = str(int(timestamp))

        return OrderStatusRecord(
            exchange=exchange,
            symbol=symbol,
            order_id=str(payload.get("id") or ""),
            side=side,
            amount=amount,
            price=price,
            status=status,
            filled=filled,
            remaining=remaining,
            timestamp=timestamp,
            raw=payload,
        )

    def _to_fill_record(
        self,
        exchange: str,
        payload: dict,
        *,
        fallback_order=None,
    ) -> FillRecord | None:
        """Normalize a filled or partially-filled order payload into a fill record."""
        filled = float(payload.get("filled") or 0.0)
        if filled <= 0:
            return None

        fee = payload.get("fee") or {}
        return FillRecord(
            exchange=exchange,
            symbol=str(payload.get("symbol") or getattr(fallback_order, "symbol", "")),
            order_id=str(payload.get("id") or ""),
            side=str(payload.get("side") or getattr(fallback_order, "side", "")),
            filled=filled,
            average_price=float(payload.get("average") or payload.get("price") or getattr(fallback_order, "price", 0.0)),
            fee_cost=float(fee.get("cost") or 0.0),
            fee_currency=str(fee.get("currency") or ""),
            timestamp=str(payload.get("datetime") or payload.get("timestamp") or ""),
            raw=payload,
        )
