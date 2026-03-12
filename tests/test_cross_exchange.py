"""Tests for cross-exchange spread detection."""

from arb_strat.config import CrossExchangeSettings
from arb_strat.models import Quote
from arb_strat.strategies.cross_exchange import CrossExchangeScanner


class FakeClient:
    """Minimal fake exchange client used to test scanner logic without network access."""

    def __init__(self, name, taker_fee_bps, quotes):
        """Store a fixed quote set that the scanner can query."""
        self.name = name
        self.taker_fee_bps = taker_fee_bps
        self._quotes = quotes

    def load_markets(self):
        """Pretend markets were loaded successfully."""
        return None

    def supported_symbols(self):
        """Return all symbols exposed by the fake exchange."""
        return set(self._quotes)

    def fetch_top_of_book(self, symbol):
        """Return the predefined quote for the requested symbol."""
        return self._quotes[symbol]


def test_cross_exchange_finds_spread():
    """Verify the scanner detects a profitable spread across two fake exchanges."""
    scanner = CrossExchangeScanner()
    settings = CrossExchangeSettings(
        enabled=True,
        symbols=("BTC/USDT",),
        min_edge_bps=1.0,
        max_opportunities=3,
    )
    clients = {
        "binance": FakeClient(
            "binance",
            5.0,
            {"BTC/USDT": Quote("BTC/USDT", bid=100.0, ask=100.2, bid_size=2.0, ask_size=2.0)},
        ),
        "okx": FakeClient(
            "okx",
            5.0,
            {"BTC/USDT": Quote("BTC/USDT", bid=101.2, ask=101.4, bid_size=2.0, ask_size=2.0)},
        ),
    }

    opportunities = scanner.scan(clients, settings, quote_capital=1000.0)

    assert len(opportunities) == 1
    assert opportunities[0].venue == "binance -> okx"
    assert opportunities[0].edge_bps > 1.0
