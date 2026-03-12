"""Tests for market-data cache behavior and subscription resolution."""

from arb_strat.config import (
    AppConfig,
    CrossExchangeSettings,
    ExchangeSettings,
    MarketDataSettings,
    TriangularSettings,
)
from arb_strat.market_data.websocket import MarketDataHub, QuoteCache, _maybe_timestamp_ms
from arb_strat.models import Quote


class FakeExchange:
    """Minimal fake CCXT-like exchange object that exposes market metadata."""

    def __init__(self, markets):
        """Store a deterministic symbol-to-market mapping for tests."""
        self._markets = markets

    def market(self, symbol):
        """Return exchange-native metadata for one symbol."""
        return self._markets[symbol]


class FakeClient:
    """Minimal fake adapter used to test subscription resolution."""

    def __init__(self, markets):
        """Expose a small supported market set and a fake exchange object."""
        self.exchange = FakeExchange(markets)
        self._markets = markets
        self.loaded = False

    def load_markets(self):
        """Record that markets were loaded."""
        self.loaded = True

    def supported_symbols(self):
        """Return the fake market symbols available on this client."""
        return set(self._markets)


def test_quote_cache_round_trip():
    """Ensure quotes written into the cache can be read back by venue and symbol."""
    cache = QuoteCache()
    quote = Quote("BTC/USDT", bid=100.0, ask=100.2, bid_size=1.0, ask_size=1.5)

    cache.set("binance", "BTC/USDT", quote)

    assert cache.get("binance", "BTC/USDT") == quote
    assert cache.get("okx", "BTC/USDT") is None


def test_subscription_set_includes_cross_and_triangular_symbols():
    """Ensure the hub resolves only supported symbols needed by both scanners."""
    markets = {
        "BTC/USDT": {"id": "BTCUSDT"},
        "ETH/BTC": {"id": "ETHBTC"},
        "ETH/USDT": {"id": "ETHUSDT"},
        "BTC/USD": {"id": "BTC-USD"},
    }
    client = FakeClient(markets)
    config = AppConfig(
        exchanges=(ExchangeSettings(name="binance"),),
        triangular=TriangularSettings(
            exchanges=("binance",),
            base_assets=("BTC", "ETH", "SOL"),
            settlement_assets=("USDT",),
            min_edge_bps=1.0,
            max_opportunities=5,
        ),
        cross_exchange=CrossExchangeSettings(
            symbols=("BTC/USDT", "BTC/USD", "SOL/USDT"),
            min_edge_bps=1.0,
            max_opportunities=5,
        ),
        market_data=MarketDataSettings(enabled=True),
    )
    hub = MarketDataHub(config, clients={"binance": client})

    subscription = hub._build_subscription_set(client, "binance")

    assert client.loaded is True
    assert subscription.symbols == ("BTC/USD", "BTC/USDT", "ETH/BTC", "ETH/USDT")
    assert subscription.id_to_symbol == {
        "BTC-USD": "BTC/USD",
        "BTCUSDT": "BTC/USDT",
        "ETHBTC": "ETH/BTC",
        "ETHUSDT": "ETH/USDT",
    }


def test_timestamp_parser_handles_utc_strings():
    """Ensure ISO8601 UTC timestamps are normalized into milliseconds."""
    assert _maybe_timestamp_ms("2026-03-12T10:11:12Z") == 1773310272000

