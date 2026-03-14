"""Tests for execution guardrails, sizing, and pause behavior."""

import time

from arb_strat.config import (
    AppConfig,
    ExchangeSettings,
    RiskSettings,
    StateSettings,
)
from arb_strat.execution.controller import ExecutionController
from arb_strat.execution.live import LiveExecutionError
from arb_strat.models import Opportunity, OrderIntent, OrderStatusRecord, Quote
from arb_strat.state import StateStore


class FakeClient:
    """Minimal fake exchange client used to test execution controls."""

    def __init__(self, *, taker_fee_bps=10.0, balances=None, quote=None, market=None):
        """Store deterministic quote, balance, and market metadata."""
        self.taker_fee_bps = taker_fee_bps
        self._balances = balances or {"USDT": 1000.0, "BTC": 1.0}
        self._quote = quote or Quote(
            "BTC/USDT",
            bid=100.0,
            ask=100.0,
            bid_size=10.0,
            ask_size=10.0,
            timestamp_ms=int(time.time() * 1000),
        )
        self._market = market or {
            "limits": {"amount": {"min": 0.001}, "cost": {"min": 10.0}}
        }
        self.cancelled_orders: list[tuple[str, str]] = []
        self.open_orders_payload: list[dict] = []
        self.order_lookup: dict[str, dict] = {}

    def fetch_top_of_book(self, symbol):
        """Return a deterministic top-of-book quote."""
        return self._quote

    def fetch_balance(self):
        """Return a deterministic free balance snapshot."""
        return dict(self._balances)

    def market_details(self, symbol):
        """Return deterministic market limits."""
        return dict(self._market)

    def normalize_order(self, symbol, amount, price):
        """Return rounded order values for predictable tests."""
        return round(amount, 6), round(price, 6)

    def fetch_order(self, order_id, symbol):
        """Return a deterministic order status payload."""
        return self.order_lookup[order_id]

    def fetch_open_orders(self, symbol=None):
        """Return a deterministic open-order list."""
        return list(self.open_orders_payload)

    def cancel_order(self, order_id, symbol):
        """Record cancellation requests and return a canceled status payload."""
        self.cancelled_orders.append((order_id, symbol))
        return {
            "id": order_id,
            "symbol": symbol,
            "side": "buy",
            "amount": 1.0,
            "price": 100.0,
            "filled": 0.5,
            "remaining": 0.5,
            "status": "canceled",
            "datetime": "2026-03-12T00:00:00+00:00",
        }

    def load_markets(self):
        """Provide a no-op load_markets hook for parity with the real adapter."""
        return None

    def supported_symbols(self):
        """Expose the fake quote symbol as a supported market."""
        return {self._quote.symbol}


class FakePaperExecutor:
    """Paper executor double that captures the prepared opportunity."""

    def __init__(self):
        """Initialize an empty captured-opportunity slot."""
        self.last_opportunity = None

    def execute(self, opportunity):
        """Capture the opportunity instead of logging it."""
        self.last_opportunity = opportunity


class AlwaysFailLiveExecutor:
    """Live executor double that always raises an execution failure."""

    def execute(self, opportunity):
        """Simulate a live execution failure."""
        raise RuntimeError("exchange rejected order")


class PartialFailLiveExecutor:
    """Live executor double that simulates one leg accepted before failure."""

    def execute(self, opportunity):
        """Raise a partial-fill style error with an accepted first response."""
        raise LiveExecutionError(
            "second leg failed after first leg accepted",
            responses=[{"id": "accepted-order"}],
        )


class AcceptLiveExecutor:
    """Live executor double that accepts prepared orders and records them."""

    def __init__(self):
        """Initialize an empty captured-opportunity slot."""
        self.last_opportunity = None

    def execute(self, opportunity):
        """Capture the prepared opportunity and simulate a clean exchange response."""
        self.last_opportunity = opportunity
        return [{"id": "accepted-order", "symbol": "BTC/USDT", "side": "buy", "amount": opportunity.orders[0].amount, "price": opportunity.orders[0].price}]


class AcceptMultiLegLiveExecutor:
    """Live executor double that returns one accepted response per planned order."""

    def execute(self, opportunity):
        """Simulate clean exchange responses for each submitted leg."""
        responses = []
        for index, order in enumerate(opportunity.orders, start=1):
            responses.append(
                {
                    "id": f"accepted-order-{index}",
                    "symbol": order.symbol,
                    "side": order.side,
                    "amount": order.amount,
                    "price": order.price,
                }
            )
        return responses


def _config(tmp_path, **risk_overrides):
    """Build a minimal app config with overridable risk settings."""
    defaults = {
        "max_live_orders_per_cycle": 10,
        "max_consecutive_failures": 2,
        "reconcile_live_orders": False,
    }
    defaults.update(risk_overrides)
    return AppConfig(
        exchanges=(ExchangeSettings(name="binance"),),
        risk=RiskSettings(**defaults),
        state=StateSettings(directory=str(tmp_path)),
    )


def _opportunity(strategy="cross_exchange"):
    """Create a one-leg opportunity used in execution control tests."""
    return Opportunity(
        strategy=strategy,
        venue="binance",
        summary="test",
        edge_bps=25.0,
        expected_pnl=1.5,
        pnl_currency="USDT",
        orders=(
            OrderIntent(
                exchange="binance",
                symbol="BTC/USDT",
                side="buy",
                price=100.0,
                amount=2.0,
            ),
        ),
    )


def _paired_opportunity():
    """Create a two-leg cross-exchange opportunity for realized-pnl tests."""
    return Opportunity(
        strategy="cross_exchange",
        venue="binance -> okx",
        summary="Buy BTC on binance, sell on okx",
        edge_bps=25.0,
        expected_pnl=1.5,
        pnl_currency="USDT",
        orders=(
            OrderIntent(
                exchange="binance",
                symbol="BTC/USDT",
                side="buy",
                price=100.0,
                amount=1.0,
            ),
            OrderIntent(
                exchange="okx",
                symbol="BTC/USDT",
                side="sell",
                price=101.5,
                amount=1.0,
            ),
        ),
    )


def test_live_triangular_is_rejected_by_default(tmp_path):
    """Live triangular execution should stay disabled unless explicitly allowed."""
    config = _config(tmp_path, allow_live_triangular=False)
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)

    record = controller.execute(_opportunity(strategy="triangular"), live=True)

    assert record.status == "rejected"
    assert "disabled" in record.reason


def test_paper_execution_scales_to_max_order_notional(tmp_path):
    """Prepared paper orders should be scaled down to the configured max notional."""
    config = _config(tmp_path, max_order_notional=100.0, max_opportunity_notional=500.0)
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)
    paper = FakePaperExecutor()
    controller.paper_executor = paper

    record = controller.execute(_opportunity(), live=False)

    assert record.status == "paper_executed"
    assert record.metadata["total_notional"] == 100.0
    assert paper.last_opportunity.orders[0].amount == 1.0


def test_live_failures_trigger_pause_after_threshold(tmp_path):
    """Repeated live failures should pause execution when configured to do so."""
    config = _config(tmp_path, allow_live_cross_exchange=True, pause_on_execution_error=True)
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)
    controller.live_executor = AlwaysFailLiveExecutor()

    controller.execute(_opportunity(), live=True)
    controller.execute(_opportunity(), live=True)

    assert state_store.is_execution_paused() is True


def test_stale_quote_is_rejected(tmp_path):
    """Quotes older than the configured freshness threshold should be rejected."""
    config = _config(tmp_path, max_quote_age_ms=1)
    stale_quote = Quote(
        "BTC/USDT",
        bid=100.0,
        ask=100.0,
        bid_size=10.0,
        ask_size=10.0,
        timestamp_ms=int(time.time() * 1000) - 10_000,
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(
        config,
        {"binance": FakeClient(quote=stale_quote)},
        state_store,
    )

    record = controller.execute(_opportunity(), live=False)

    assert record.status == "rejected"
    assert "quote age" in record.reason


def test_live_inventory_cap_scales_order_size(tmp_path):
    """Live orders should scale down to stay within configured inventory caps."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        max_asset_balance_by_exchange={"binance": {"BTC": 1.5}},
        max_quote_age_ms=10_000,
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(
        config,
        {"binance": FakeClient(balances={"USDT": 1000.0, "BTC": 1.0})},
        state_store,
    )
    live_executor = AcceptLiveExecutor()
    controller.live_executor = live_executor

    record = controller.execute(_opportunity(), live=True)

    assert record.status == "live_submitted"
    assert record.metadata["total_notional"] == 50.0
    assert live_executor.last_opportunity.orders[0].amount == 0.5


def test_partial_fill_pauses_immediately(tmp_path):
    """Any partial live fill should pause execution immediately when enabled."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        pause_on_partial_fill=True,
        max_quote_age_ms=10_000,
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)
    controller.live_executor = PartialFailLiveExecutor()

    record = controller.execute(_opportunity(), live=True)

    assert record.status == "live_partial_failure"
    assert state_store.is_execution_paused() is True


def test_live_reconciliation_records_fill(tmp_path):
    """Successful live execution should reconcile order status and record fills."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        max_quote_age_ms=10_000,
        reconcile_live_orders=True,
    )
    client = FakeClient()
    client.order_lookup["accepted-order"] = {
        "id": "accepted-order",
        "symbol": "BTC/USDT",
        "side": "buy",
        "amount": 1.0,
        "price": 100.0,
        "average": 100.0,
        "filled": 1.0,
        "remaining": 0.0,
        "status": "closed",
        "datetime": "2026-03-12T00:00:00+00:00",
        "fee": {"cost": 0.1, "currency": "USDT"},
    }
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": client}, state_store)
    controller.live_executor = AcceptLiveExecutor()

    record = controller.execute(_opportunity(), live=True)

    assert record.status == "live_submitted"
    assert state_store.recent_fill_records()
    assert state_store.recent_fill_records()[0].order_id == "accepted-order"
    assert state_store.recent_order_records()[0].status == "filled"


def test_fetch_open_orders_records_open_status(tmp_path):
    """Open-order refresh should normalize and persist current open orders."""
    config = _config(tmp_path)
    client = FakeClient()
    client.open_orders_payload = [
        {
            "id": "open-1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "amount": 1.0,
            "price": 100.0,
            "filled": 0.25,
            "remaining": 0.75,
            "status": "open",
            "datetime": "2026-03-12T00:00:00+00:00",
        }
    ]
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": client}, state_store)

    records = controller.fetch_open_orders()

    assert len(records) == 1
    assert records[0].order_id == "open-1"
    assert state_store.open_order_records()[0].order_id == "open-1"


def test_partial_failure_attempts_cancel(tmp_path):
    """Accepted live legs should be canceled on later failure when configured."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        pause_on_partial_fill=True,
        cancel_on_partial_failure=True,
        max_quote_age_ms=10_000,
    )
    client = FakeClient()
    client.order_lookup["accepted-order"] = {
        "id": "accepted-order",
        "symbol": "BTC/USDT",
        "side": "buy",
        "amount": 1.0,
        "price": 100.0,
        "filled": 0.5,
        "remaining": 0.5,
        "status": "open",
        "datetime": "2026-03-12T00:00:00+00:00",
    }
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": client}, state_store)
    controller.live_executor = PartialFailLiveExecutor()

    record = controller.execute(_opportunity(), live=True)

    assert record.status == "live_partial_failure"
    assert client.cancelled_orders == [("accepted-order", "BTC/USDT")]


def test_low_net_profit_is_rejected_after_slippage(tmp_path):
    """Post-cost net profit checks should reject weak trades after slippage is estimated."""
    config = _config(
        tmp_path,
        min_net_profit_usd=2.0,
        max_quote_age_ms_cross_exchange=10_000,
    )
    quote = Quote(
        "BTC/USDT",
        bid=99.4,
        ask=100.0,
        bid_size=10.0,
        ask_size=10.0,
        timestamp_ms=int(time.time() * 1000),
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(
        config,
        {"binance": FakeClient(quote=quote)},
        state_store,
    )

    record = controller.execute(_opportunity(), live=False)

    assert record.status == "rejected"
    assert "net pnl" in record.reason


def test_open_notional_cap_rejects_new_live_order(tmp_path):
    """Live execution should respect the total in-flight open notional cap."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        max_quote_age_ms_cross_exchange=10_000,
        max_total_open_notional_usd=250.0,
    )
    state_store = StateStore(config.state)
    state_store.record_order_status(
        OrderStatusRecord(
            exchange="binance",
            symbol="BTC/USDT",
            order_id="open-1",
            side="buy",
            amount=2.0,
            price=100.0,
            status="open",
            filled=0.0,
            remaining=2.0,
            timestamp="2026-03-12T00:00:00+00:00",
        )
    )
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)
    controller.live_executor = AcceptLiveExecutor()

    record = controller.execute(_opportunity(), live=True)

    assert record.status == "rejected"
    assert "portfolio cap" in record.reason


def test_daily_loss_limit_blocks_next_live_trade(tmp_path):
    """A breached daily live-loss limit should pause and reject subsequent live trades."""
    config = _config(
        tmp_path,
        allow_live_cross_exchange=True,
        max_quote_age_ms_cross_exchange=10_000,
        max_daily_loss_usd=1.0,
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(config, {"binance": FakeClient()}, state_store)
    controller.live_executor = AlwaysFailLiveExecutor()

    controller.execute(_opportunity(), live=True)
    record = controller.execute(_opportunity(), live=True)

    assert record.status == "rejected"
    assert "daily live loss limit" in record.reason
    assert state_store.is_execution_paused() is True


def test_cross_exchange_uses_tighter_quote_age_limit(tmp_path):
    """Cross-exchange should use its own tighter quote-age threshold instead of the fallback."""
    config = _config(
        tmp_path,
        max_quote_age_ms=10_000,
        max_quote_age_ms_cross_exchange=5,
    )
    stale_quote = Quote(
        "BTC/USDT",
        bid=100.0,
        ask=100.0,
        bid_size=10.0,
        ask_size=10.0,
        timestamp_ms=int(time.time() * 1000) - 50,
    )
    state_store = StateStore(config.state)
    controller = ExecutionController(
        config,
        {"binance": FakeClient(quote=stale_quote)},
        state_store,
    )

    record = controller.execute(_opportunity(), live=False)

    assert record.status == "rejected"
    assert "quote age" in record.reason


def test_completed_cross_exchange_group_records_realized_pnl(tmp_path):
    """A fully filled two-leg cross-exchange trade should produce realized pnl in SQLite."""
    config = AppConfig(
        exchanges=(ExchangeSettings(name="binance"), ExchangeSettings(name="okx")),
        risk=RiskSettings(
            allow_live_cross_exchange=True,
            max_quote_age_ms_cross_exchange=10_000,
            reconcile_live_orders=True,
        ),
        state=StateSettings(directory=str(tmp_path)),
    )
    binance = FakeClient(
        quote=Quote(
            "BTC/USDT",
            bid=99.9,
            ask=100.0,
            bid_size=10.0,
            ask_size=10.0,
            timestamp_ms=int(time.time() * 1000),
        )
    )
    okx = FakeClient(
        quote=Quote(
            "BTC/USDT",
            bid=101.5,
            ask=101.6,
            bid_size=10.0,
            ask_size=10.0,
            timestamp_ms=int(time.time() * 1000),
        )
    )
    binance.order_lookup["accepted-order-1"] = {
        "id": "accepted-order-1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "amount": 1.0,
        "price": 100.0,
        "average": 100.0,
        "filled": 1.0,
        "remaining": 0.0,
        "status": "closed",
        "datetime": "2026-03-12T00:00:00+00:00",
        "fee": {"cost": 0.1, "currency": "USDT"},
    }
    okx.order_lookup["accepted-order-2"] = {
        "id": "accepted-order-2",
        "symbol": "BTC/USDT",
        "side": "sell",
        "amount": 1.0,
        "price": 101.5,
        "average": 101.5,
        "filled": 1.0,
        "remaining": 0.0,
        "status": "closed",
        "datetime": "2026-03-12T00:00:00+00:00",
        "fee": {"cost": 0.1, "currency": "USDT"},
    }
    state_store = StateStore(config.state)
    controller = ExecutionController(
        config,
        {"binance": binance, "okx": okx},
        state_store,
    )
    controller.live_executor = AcceptMultiLegLiveExecutor()

    record = controller.execute(_paired_opportunity(), live=True)

    assert record.status == "live_submitted"
    summary = state_store.realized_pnl_summary()
    assert "USDT" in summary["totals"]
    assert summary["totals"]["USDT"] > 0
    assert summary["recent"][0]["status"] == "complete"
