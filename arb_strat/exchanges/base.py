"""Protocol definitions for exchange clients used by the strategies."""

from __future__ import annotations

from typing import Protocol

from arb_strat.models import Quote


class ExchangeClient(Protocol):
    """Minimal interface an exchange adapter must satisfy for this project."""

    name: str
    taker_fee_bps: float

    def load_markets(self) -> None:
        ...

    def supported_symbols(self) -> set[str]:
        ...

    def fetch_top_of_book(self, symbol: str) -> Quote:
        ...

    def fetch_balance(self) -> dict[str, float]:
        ...

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        ...

    def close(self) -> None:
        ...
