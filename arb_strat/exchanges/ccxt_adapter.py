"""CCXT-backed exchange adapter used by the scanners and live executor."""

from __future__ import annotations

import logging
import os
from typing import Any

import ccxt

from arb_strat.config import ExchangeSettings
from arb_strat.market_data.websocket import QuoteCache
from arb_strat.models import Quote

logger = logging.getLogger(__name__)


class CCXTExchangeAdapter:
    """Wrap a CCXT exchange object behind a smaller project-specific interface."""

    def __init__(self, settings: ExchangeSettings) -> None:
        """Create and configure a CCXT client for one exchange."""
        self.settings = settings
        self.name = settings.name
        self.taker_fee_bps = settings.taker_fee_bps
        self.quote_cache: QuoteCache | None = None
        self.rest_fallback = True
        exchange_cls = getattr(ccxt, settings.name)
        self.exchange = exchange_cls({"enableRateLimit": True})
        if settings.hostname:
            self.exchange.hostname = settings.hostname
        self._markets: dict[str, Any] = {}
        self._configure_auth()

        if settings.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)

    def _configure_auth(self) -> None:
        """Populate CCXT credentials from environment variables when provided."""
        if self.settings.api_key_env:
            self.exchange.apiKey = self._read_env_value(self.settings.api_key_env)
        if self.settings.secret_env:
            secret = self._read_env_value(self.settings.secret_env)
            if self.name == "coinbase":
                secret = self._normalize_coinbase_secret(secret)
            self.exchange.secret = secret
        if self.settings.password_env:
            self.exchange.password = self._read_env_value(self.settings.password_env)

    @staticmethod
    def _read_env_value(env_name: str) -> str:
        """Read an env var and strip only accidental surrounding whitespace."""
        return os.getenv(env_name, "").strip()

    @staticmethod
    def _normalize_coinbase_secret(secret: str) -> str:
        """Convert one-line PEM env values back into the multiline form CCXT expects."""
        normalized = secret.strip()
        if not normalized:
            return normalized
        if normalized[:1] == normalized[-1:] and normalized[:1] in {"'", '"'}:
            normalized = normalized[1:-1].strip()
        if "\\n" in normalized:
            normalized = normalized.replace("\\n", "\n")
        return normalized

    def load_markets(self) -> None:
        """Load exchange markets once and cache them for later symbol checks."""
        if not self._markets:
            self._markets = self.exchange.load_markets()

    def supported_symbols(self) -> set[str]:
        """Return the set of symbols currently exposed by the exchange."""
        self.load_markets()
        return set(self._markets.keys())

    def set_quote_cache(self, quote_cache: QuoteCache, rest_fallback: bool = True) -> None:
        """Attach a shared quote cache populated by the WebSocket market-data layer."""
        self.quote_cache = quote_cache
        self.rest_fallback = rest_fallback

    def fetch_top_of_book(self, symbol: str) -> Quote:
        """Fetch the best bid and ask for a symbol and normalize it into a Quote."""
        if self.quote_cache:
            cached = self.quote_cache.get(self.name, symbol)
            if cached is not None:
                return cached
            if not self.rest_fallback:
                raise ValueError(f"{self.name} has no cached quote yet for {symbol}")

        order_book = self.exchange.fetch_order_book(symbol, limit=5)
        if not order_book.get("bids") or not order_book.get("asks"):
            raise ValueError(f"{self.name} returned an empty book for {symbol}")

        bid_price, bid_size = order_book["bids"][0][:2]
        ask_price, ask_size = order_book["asks"][0][:2]
        return Quote(
            symbol=symbol,
            bid=float(bid_price),
            ask=float(ask_price),
            bid_size=float(bid_size),
            ask_size=float(ask_size),
            timestamp_ms=order_book.get("timestamp"),
        )

    def fetch_balance(self) -> dict[str, float]:
        """Fetch free balances and return only the usable currency amounts."""
        balance = self.exchange.fetch_balance()
        free_balances = balance.get("free", {})
        return {
            currency: float(amount)
            for currency, amount in free_balances.items()
            if amount is not None
        }

    def market_details(self, symbol: str) -> dict[str, Any]:
        """Return raw CCXT market metadata for one symbol."""
        self.load_markets()
        return self.exchange.market(symbol)

    def normalize_order(self, symbol: str, amount: float, price: float) -> tuple[float, float]:
        """Round an order to the exchange-supported amount and price precision."""
        normalized_amount = float(self.exchange.amount_to_precision(symbol, amount))
        normalized_price = float(self.exchange.price_to_precision(symbol, price))
        return normalized_amount, normalized_price

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Submit a limit order through CCXT and return the exchange response."""
        logger.info(
            "Submitting %s %.8f %s @ %.8f on %s",
            side.upper(),
            amount,
            symbol,
            price,
            self.name,
        )
        return self.exchange.create_order(
            symbol=symbol,
            type="limit",
            side=side,
            amount=amount,
            price=price,
        )

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        """Fetch the latest state for one exchange order."""
        return self.exchange.fetch_order(order_id, symbol)

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Fetch currently open orders, optionally scoped to one symbol."""
        return self.exchange.fetch_open_orders(symbol)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel one open order and return the exchange response."""
        return self.exchange.cancel_order(order_id, symbol)

    def close(self) -> None:
        """Close the exchange client if the underlying adapter exposes a close method."""
        close_method = getattr(self.exchange, "close", None)
        if callable(close_method):
            close_method()
