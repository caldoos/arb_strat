"""SQLite-backed trade ledger for orders, fills, and realized pnl."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from arb_strat.models import FillRecord, Opportunity, OrderStatusRecord


class SQLiteLedger:
    """Persist execution groups, orders, fills, and realized pnl in SQLite."""

    def __init__(self, path: Path) -> None:
        """Initialize the database file and create tables on first use."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def register_execution_group(
        self,
        execution_group_id: str,
        opportunity: Opportunity,
        *,
        mode: str,
        total_notional: float,
    ) -> None:
        """Insert or update the parent execution-group row."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_groups (
                    execution_group_id,
                    created_at,
                    mode,
                    strategy,
                    venue,
                    summary,
                    expected_pnl,
                    pnl_currency,
                    edge_bps,
                    total_notional,
                    status,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_group_id) DO UPDATE SET
                    mode = excluded.mode,
                    strategy = excluded.strategy,
                    venue = excluded.venue,
                    summary = excluded.summary,
                    expected_pnl = excluded.expected_pnl,
                    pnl_currency = excluded.pnl_currency,
                    edge_bps = excluded.edge_bps,
                    total_notional = excluded.total_notional,
                    metadata_json = excluded.metadata_json
                """,
                (
                    execution_group_id,
                    datetime.now(timezone.utc).isoformat(),
                    mode,
                    opportunity.strategy,
                    opportunity.venue,
                    opportunity.summary,
                    opportunity.expected_pnl,
                    opportunity.pnl_currency,
                    opportunity.edge_bps,
                    total_notional,
                    "created",
                    json.dumps(opportunity.metadata, sort_keys=True),
                ),
            )

    def update_execution_group_status(
        self,
        execution_group_id: str,
        *,
        status: str,
    ) -> None:
        """Persist the latest lifecycle status for an execution group."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE execution_groups
                SET status = ?
                WHERE execution_group_id = ?
                """,
                (status, execution_group_id),
            )

    def record_order_status(self, record: OrderStatusRecord) -> None:
        """Persist one normalized order-status record."""
        if not record.execution_group_id:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO order_statuses (
                    execution_group_id,
                    exchange_name,
                    symbol,
                    order_id,
                    side,
                    amount,
                    price,
                    status,
                    filled,
                    remaining,
                    timestamp,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.execution_group_id,
                    record.exchange,
                    record.symbol,
                    record.order_id,
                    record.side,
                    record.amount,
                    record.price,
                    record.status,
                    record.filled,
                    record.remaining,
                    record.timestamp,
                    json.dumps(record.raw, sort_keys=True),
                ),
            )

    def record_fill(self, record: FillRecord) -> None:
        """Persist one normalized fill record and attempt realized-pnl reconciliation."""
        if not record.execution_group_id:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO fills (
                    execution_group_id,
                    exchange_name,
                    symbol,
                    order_id,
                    side,
                    filled,
                    average_price,
                    fee_cost,
                    fee_currency,
                    timestamp,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.execution_group_id,
                    record.exchange,
                    record.symbol,
                    record.order_id,
                    record.side,
                    record.filled,
                    record.average_price,
                    record.fee_cost,
                    record.fee_currency,
                    record.timestamp,
                    json.dumps(record.raw, sort_keys=True),
                ),
            )
            self._reconcile_realized_pnl(connection, record.execution_group_id)

    def realized_pnl_summary(self) -> dict[str, object]:
        """Return aggregate realized-pnl totals and recent events from the ledger."""
        with self._connect() as connection:
            totals_rows = connection.execute(
                """
                SELECT pnl_currency, COALESCE(SUM(realized_pnl), 0.0) AS total_pnl
                FROM pnl_events
                GROUP BY pnl_currency
                ORDER BY pnl_currency
                """
            ).fetchall()
            today_key = datetime.now(timezone.utc).date().isoformat()
            today_rows = connection.execute(
                """
                SELECT pnl_currency, COALESCE(SUM(realized_pnl), 0.0) AS total_pnl
                FROM pnl_events
                WHERE substr(computed_at, 1, 10) = ?
                GROUP BY pnl_currency
                ORDER BY pnl_currency
                """,
                (today_key,),
            ).fetchall()
            recent_rows = connection.execute(
                """
                SELECT execution_group_id, strategy, venue, realized_pnl, pnl_currency, status, computed_at
                FROM pnl_events
                ORDER BY computed_at DESC
                LIMIT 10
                """
            ).fetchall()

        return {
            "totals": {row["pnl_currency"]: float(row["total_pnl"]) for row in totals_rows},
            "today": {row["pnl_currency"]: float(row["total_pnl"]) for row in today_rows},
            "recent": [
                {
                    "execution_group_id": row["execution_group_id"],
                    "strategy": row["strategy"],
                    "venue": row["venue"],
                    "realized_pnl": float(row["realized_pnl"]),
                    "pnl_currency": row["pnl_currency"],
                    "status": row["status"],
                    "computed_at": row["computed_at"],
                }
                for row in recent_rows
            ],
        }

    def _initialize(self) -> None:
        """Create the SQLite schema if it does not already exist."""
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS execution_groups (
                    execution_group_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    expected_pnl REAL NOT NULL,
                    pnl_currency TEXT NOT NULL,
                    edge_bps REAL NOT NULL,
                    total_notional REAL NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_statuses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_group_id TEXT NOT NULL,
                    exchange_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount REAL NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL,
                    filled REAL NOT NULL,
                    remaining REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    UNIQUE(execution_group_id, order_id, status, filled, remaining, timestamp)
                );

                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_group_id TEXT NOT NULL,
                    exchange_name TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    filled REAL NOT NULL,
                    average_price REAL NOT NULL,
                    fee_cost REAL NOT NULL,
                    fee_currency TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    UNIQUE(execution_group_id, order_id, side, filled, average_price, timestamp)
                );

                CREATE TABLE IF NOT EXISTS pnl_events (
                    execution_group_id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    realized_pnl REAL NOT NULL,
                    pnl_currency TEXT NOT NULL,
                    gross_buy_notional REAL NOT NULL,
                    gross_sell_notional REAL NOT NULL,
                    fee_total REAL NOT NULL,
                    matched_base_amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    computed_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a row-factory-enabled SQLite connection under a lock."""
        self._lock.acquire()
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return _LockedConnection(self._lock, connection)

    def _reconcile_realized_pnl(self, connection: sqlite3.Connection, execution_group_id: str) -> None:
        """Compute realized pnl for completed cross-exchange pairs when fills are sufficient."""
        group = connection.execute(
            """
            SELECT strategy, venue, pnl_currency, status
            FROM execution_groups
            WHERE execution_group_id = ?
            """,
            (execution_group_id,),
        ).fetchone()
        if group is None or group["strategy"] != "cross_exchange":
            return

        fills = connection.execute(
            """
            SELECT symbol, side, filled, average_price, fee_cost, fee_currency
            FROM fills
            WHERE execution_group_id = ?
            ORDER BY id ASC
            """,
            (execution_group_id,),
        ).fetchall()
        if len(fills) < 2:
            return

        symbols = {row["symbol"] for row in fills}
        if len(symbols) != 1:
            return

        symbol = next(iter(symbols))
        base_asset, quote_asset = symbol.split("/")
        buy_rows = [row for row in fills if row["side"] == "buy"]
        sell_rows = [row for row in fills if row["side"] == "sell"]
        if not buy_rows or not sell_rows:
            return

        buy_qty = sum(float(row["filled"]) for row in buy_rows)
        sell_qty = sum(float(row["filled"]) for row in sell_rows)
        matched_qty = min(buy_qty, sell_qty)
        if matched_qty <= 0:
            return

        status = "complete"
        if abs(buy_qty - sell_qty) > 1e-9:
            status = "partial_unmatched"

        buy_average = self._weighted_average(buy_rows, total_qty=buy_qty)
        sell_average = self._weighted_average(sell_rows, total_qty=sell_qty)
        gross_buy_notional = matched_qty * buy_average
        gross_sell_notional = matched_qty * sell_average
        fee_total = self._quote_fee_total(
            rows=fills,
            quote_asset=quote_asset,
            base_asset=base_asset,
            buy_average=buy_average,
            sell_average=sell_average,
        )
        realized_pnl = gross_sell_notional - gross_buy_notional - fee_total

        connection.execute(
            """
            INSERT INTO pnl_events (
                execution_group_id,
                strategy,
                venue,
                realized_pnl,
                pnl_currency,
                gross_buy_notional,
                gross_sell_notional,
                fee_total,
                matched_base_amount,
                status,
                computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_group_id) DO UPDATE SET
                realized_pnl = excluded.realized_pnl,
                pnl_currency = excluded.pnl_currency,
                gross_buy_notional = excluded.gross_buy_notional,
                gross_sell_notional = excluded.gross_sell_notional,
                fee_total = excluded.fee_total,
                matched_base_amount = excluded.matched_base_amount,
                status = excluded.status,
                computed_at = excluded.computed_at
            """,
            (
                execution_group_id,
                group["strategy"],
                group["venue"],
                realized_pnl,
                quote_asset,
                gross_buy_notional,
                gross_sell_notional,
                fee_total,
                matched_qty,
                status,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    def _weighted_average(self, rows: list[sqlite3.Row], *, total_qty: float) -> float:
        """Return a fill-weighted average price for one side of an execution group."""
        if total_qty <= 0:
            return 0.0
        return sum(float(row["filled"]) * float(row["average_price"]) for row in rows) / total_qty

    def _quote_fee_total(
        self,
        *,
        rows: list[sqlite3.Row],
        quote_asset: str,
        base_asset: str,
        buy_average: float,
        sell_average: float,
    ) -> float:
        """Convert recorded fees into quote-currency terms for realized-pnl accounting."""
        reference_price = (buy_average + sell_average) / 2.0 if (buy_average + sell_average) > 0 else 0.0
        total = 0.0
        for row in rows:
            fee_cost = float(row["fee_cost"])
            fee_currency = str(row["fee_currency"] or "")
            if fee_cost <= 0 or not fee_currency:
                continue
            if fee_currency == quote_asset:
                total += fee_cost
            elif fee_currency == base_asset and reference_price > 0:
                total += fee_cost * reference_price
        return total


class _LockedConnection:
    """Context manager that releases the shared lock when the connection closes."""

    def __init__(self, lock: Lock, connection: sqlite3.Connection) -> None:
        """Store the acquired lock and the open SQLite connection."""
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        """Return the live SQLite connection."""
        return self._connection

    def __exit__(self, exc_type, exc, tb) -> None:
        """Commit or roll back, then close the connection and release the lock."""
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
            self._lock.release()
