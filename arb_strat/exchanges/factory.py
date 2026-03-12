"""Factory helpers for building exchange clients from config objects."""

from __future__ import annotations

from arb_strat.config import ExchangeSettings
from arb_strat.exchanges.ccxt_adapter import CCXTExchangeAdapter


def build_exchange_clients(
    exchanges: tuple[ExchangeSettings, ...],
) -> dict[str, CCXTExchangeAdapter]:
    """Instantiate all enabled exchange adapters defined in the config."""
    clients: dict[str, CCXTExchangeAdapter] = {}
    for exchange in exchanges:
        if exchange.enabled:
            clients[exchange.name] = CCXTExchangeAdapter(exchange)
    return clients
