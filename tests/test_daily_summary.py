"""Tests for daily summary aggregation and reset behavior."""

from datetime import datetime, timezone

from arb_strat.config import StateSettings
from arb_strat.models import ExecutionRecord, Opportunity
from arb_strat.state import StateStore


def test_daily_summary_accumulates_activity(tmp_path):
    """Daily summary counters should reflect opportunities, executions, and errors."""
    store = StateStore(StateSettings(directory=str(tmp_path)))

    store.record_opportunities(
        [
            Opportunity(
                strategy="cross_exchange",
                venue="binance -> okx",
                summary="test opp",
                edge_bps=12.0,
                expected_pnl=1.0,
                pnl_currency="USDT",
            )
        ]
    )
    store.record_execution(
        ExecutionRecord.now(
            mode="paper",
            strategy="cross_exchange",
            venue="binance -> okx",
            summary="paper execution",
            status="paper_executed",
            edge_bps=12.0,
            expected_pnl=1.0,
            pnl_currency="USDT",
            order_count=2,
        )
    )
    store.record_error("scanner", "temporary failure")

    summary = store.daily_summary_snapshot()

    assert summary["cycles"] == 1
    assert summary["opportunities_total"] == 1
    assert summary["opportunities_by_strategy"]["cross_exchange"] == 1
    assert summary["execution_status_counts"]["paper_executed"] == 1
    assert summary["expected_pnl_by_currency"]["USDT"] == 1.0
    assert summary["error_count"] == 1


def test_daily_summary_reset_marks_last_sent(tmp_path):
    """Sending a daily summary should reset counters and store the send timestamp."""
    store = StateStore(StateSettings(directory=str(tmp_path)))
    store.record_opportunities([])

    sent_at = datetime(2026, 3, 12, 0, 0, tzinfo=timezone.utc)
    store.mark_daily_summary_sent(sent_at)

    summary = store.daily_summary_snapshot()
    assert summary["cycles"] == 0
    assert store.last_daily_summary_sent_at() == sent_at.isoformat()
