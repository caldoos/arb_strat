"""Tests for triangular arbitrage cycle detection."""

from arb_strat.config import TriangularSettings
from arb_strat.models import Quote
from arb_strat.strategies.triangular import TriangularScanner


class FakeClient:
    """Minimal fake client used to supply deterministic quotes to the scanner."""

    def __init__(self, taker_fee_bps, quotes):
        """Store the fee assumption and static quotes used by the test."""
        self.taker_fee_bps = taker_fee_bps
        self._quotes = quotes

    def load_markets(self):
        """Pretend market metadata has already been loaded."""
        return None

    def supported_symbols(self):
        """Return all symbols defined in the fake quote set."""
        return set(self._quotes)

    def fetch_top_of_book(self, symbol):
        """Return a deterministic top-of-book quote for the requested symbol."""
        return self._quotes[symbol]


def test_triangular_scanner_detects_cycle():
    """Verify the triangular scanner identifies a profitable conversion loop."""
    scanner = TriangularScanner()
    settings = TriangularSettings(
        enabled=True,
        exchanges=("binance",),
        base_assets=("BTC", "ETH", "SOL"),
        settlement_assets=("USDT",),
        min_edge_bps=1.0,
        max_opportunities=5,
    )
    quotes = {
        "BTC/USDT": Quote("BTC/USDT", bid=50000.0, ask=50010.0, bid_size=1.0, ask_size=1.0),
        "ETH/BTC": Quote("ETH/BTC", bid=0.0515, ask=0.0516, bid_size=50.0, ask_size=50.0),
        "ETH/USDT": Quote("ETH/USDT", bid=2595.0, ask=2600.0, bid_size=50.0, ask_size=50.0),
    }
    client = FakeClient(taker_fee_bps=5.0, quotes=quotes)

    opportunities = scanner.scan(
        exchange_name="binance",
        client=client,
        settings=settings,
        quote_capital=1000.0,
    )

    assert opportunities
    assert opportunities[0].strategy == "triangular"
    assert opportunities[0].edge_bps > 1.0
