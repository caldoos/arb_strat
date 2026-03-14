"""Tests for CCXT adapter auth normalization."""

from __future__ import annotations

from arb_strat.config import ExchangeSettings
from arb_strat.exchanges.ccxt_adapter import CCXTExchangeAdapter


class FakeCoinbaseExchange:
    """Minimal fake CCXT exchange used to capture configured credentials."""

    def __init__(self, config):
        self.config = config
        self.apiKey = ""
        self.secret = ""
        self.password = ""


def test_coinbase_secret_restores_multiline_pem(monkeypatch):
    """Ensure one-line PEM env values are normalized before reaching CCXT."""
    monkeypatch.setattr(
        "arb_strat.exchanges.ccxt_adapter.ccxt.coinbase",
        FakeCoinbaseExchange,
    )
    monkeypatch.setenv("COINBASE_API_KEY", "test-key")
    monkeypatch.setenv(
        "COINBASE_API_SECRET",
        '"-----BEGIN EC PRIVATE KEY-----\\nabc123\\n-----END EC PRIVATE KEY-----\\n"',
    )

    adapter = CCXTExchangeAdapter(
        ExchangeSettings(
            name="coinbase",
            api_key_env="COINBASE_API_KEY",
            secret_env="COINBASE_API_SECRET",
        )
    )

    assert adapter.exchange.apiKey == "test-key"
    assert adapter.exchange.secret == (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "abc123\n"
        "-----END EC PRIVATE KEY-----\n"
    )
