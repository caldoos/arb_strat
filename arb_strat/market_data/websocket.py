"""Public WebSocket market-data ingestion for Binance, Coinbase, and OKX."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import threading
from dataclasses import dataclass
from itertools import permutations

import websockets

from arb_strat.config import AppConfig
from arb_strat.models import Quote

logger = logging.getLogger(__name__)


class QuoteCache:
    """Thread-safe in-memory store for the latest best bid/ask per venue and symbol."""

    def __init__(self) -> None:
        """Initialize an empty quote cache."""
        self._lock = threading.Lock()
        self._quotes: dict[tuple[str, str], Quote] = {}

    def set(self, exchange: str, symbol: str, quote: Quote) -> None:
        """Store or replace the latest quote for one exchange/symbol pair."""
        with self._lock:
            self._quotes[(exchange, symbol)] = quote

    def get(self, exchange: str, symbol: str) -> Quote | None:
        """Return the latest cached quote for one exchange/symbol pair if present."""
        with self._lock:
            return self._quotes.get((exchange, symbol))


@dataclass(frozen=True)
class SubscriptionSet:
    """Resolved market symbols and exchange-native ids needed for WebSocket subscriptions."""

    symbols: tuple[str, ...]
    id_to_symbol: dict[str, str]


class MarketDataHub:
    """Maintain WebSocket best-bid/ask streams and expose them through a shared cache."""

    def __init__(self, config: AppConfig, clients: dict[str, object]) -> None:
        """Build a market-data hub for the configured exchanges and strategies."""
        self.config = config
        self.clients = clients
        self.cache = QuoteCache()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background WebSocket event loop if market data is enabled."""
        if not self.config.market_data.enabled or self._thread is not None:
            return

        self._thread = threading.Thread(target=self._run_loop, name="market-data", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background WebSocket event loop and wait briefly for shutdown."""
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        """Create an asyncio loop in a background thread and run all stream tasks."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        tasks = [
            self._loop.create_task(self._run_exchange_stream(exchange_name, client))
            for exchange_name, client in self.clients.items()
        ]
        try:
            self._loop.run_forever()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                self._loop.run_until_complete(
                    asyncio.gather(*tasks, return_exceptions=True)
                )
            self._loop.close()

    async def _run_exchange_stream(self, exchange_name: str, client: object) -> None:
        """Start and maintain the correct public market-data stream for one exchange."""
        subscription = self._build_subscription_set(client, exchange_name)
        if not subscription.symbols:
            logger.info("No WebSocket symbols configured for %s", exchange_name)
            return

        if exchange_name == "binance":
            await self._run_binance(client, subscription)
        elif exchange_name == "coinbase":
            await self._run_coinbase(client, subscription)
        elif exchange_name == "okx":
            await self._run_okx(client, subscription)

    def _build_subscription_set(self, client: object, exchange_name: str) -> SubscriptionSet:
        """Resolve the symbol set needed for both scanners on one exchange."""
        client.load_markets()
        supported_symbols = client.supported_symbols()

        selected_symbols: set[str] = set()
        for symbol in self.config.cross_exchange.symbols:
            if symbol in supported_symbols:
                selected_symbols.add(symbol)

        triangle_assets = set(self.config.triangular.base_assets) | set(
            self.config.triangular.settlement_assets
        )
        for left, right in permutations(sorted(triangle_assets), 2):
            symbol = f"{left}/{right}"
            if symbol in supported_symbols:
                selected_symbols.add(symbol)

        id_to_symbol: dict[str, str] = {}
        for symbol in sorted(selected_symbols):
            market = client.exchange.market(symbol)
            market_id = str(market["id"])
            id_to_symbol[market_id] = symbol

        logger.info(
            "Market-data subscriptions for %s: %s",
            exchange_name,
            ", ".join(sorted(selected_symbols)) or "none",
        )
        return SubscriptionSet(symbols=tuple(sorted(selected_symbols)), id_to_symbol=id_to_symbol)

    async def _run_binance(self, client: object, subscription: SubscriptionSet) -> None:
        """Consume Binance bookTicker streams and update the shared quote cache."""
        stream_names = []
        for symbol in subscription.symbols:
            market_id = client.exchange.market(symbol)["id"].lower()
            stream_names.append(f"{market_id}@bookTicker")

        if not stream_names:
            return

        url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(stream_names)
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    async for raw in ws:
                        payload = json.loads(raw)
                        data = payload.get("data", {})
                        market_id = str(data.get("s", ""))
                        symbol = subscription.id_to_symbol.get(market_id)
                        if not symbol:
                            continue
                        self.cache.set(
                            client.name,
                            symbol,
                            Quote(
                                symbol=symbol,
                                bid=float(data["b"]),
                                ask=float(data["a"]),
                                bid_size=float(data["B"]),
                                ask_size=float(data["A"]),
                                timestamp_ms=int(data.get("E", 0)) or None,
                            ),
                        )
            except Exception as exc:
                logger.warning("Binance WebSocket reconnect after error: %s", exc)
                await asyncio.sleep(2)

    async def _run_coinbase(self, client: object, subscription: SubscriptionSet) -> None:
        """Consume Coinbase ticker updates and update the shared quote cache."""
        product_ids = [client.exchange.market(symbol)["id"] for symbol in subscription.symbols]
        if not product_ids:
            return

        subscribe = {
            "type": "subscribe",
            "product_ids": product_ids,
            "channel": "ticker",
        }
        url = "wss://advanced-trade-ws.coinbase.com"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(subscribe))
                    async for raw in ws:
                        payload = json.loads(raw)
                        for event in payload.get("events", []):
                            for ticker in event.get("tickers", []):
                                market_id = str(ticker.get("product_id", ""))
                                symbol = subscription.id_to_symbol.get(market_id)
                                if not symbol:
                                    continue
                                best_bid = ticker.get("best_bid")
                                best_ask = ticker.get("best_ask")
                                if not best_bid or not best_ask:
                                    continue
                                self.cache.set(
                                    client.name,
                                    symbol,
                                    Quote(
                                        symbol=symbol,
                                        bid=float(best_bid),
                                        ask=float(best_ask),
                                        bid_size=float(ticker.get("best_bid_quantity", 0.0)),
                                        ask_size=float(ticker.get("best_ask_quantity", 0.0)),
                                        timestamp_ms=_maybe_timestamp_ms(ticker.get("time")),
                                    ),
                                )
            except Exception as exc:
                logger.warning("Coinbase WebSocket reconnect after error: %s", exc)
                await asyncio.sleep(2)

    async def _run_okx(self, client: object, subscription: SubscriptionSet) -> None:
        """Consume OKX BBO updates and update the shared quote cache."""
        args = [
            {"channel": "bbo-tbt", "instId": client.exchange.market(symbol)["id"]}
            for symbol in subscription.symbols
        ]
        if not args:
            return

        subscribe = {"op": "subscribe", "args": args}
        url = "wss://ws.okx.com:8443/ws/v5/public"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps(subscribe))
                    async for raw in ws:
                        payload = json.loads(raw)
                        arg = payload.get("arg", {})
                        market_id = str(arg.get("instId", ""))
                        symbol = subscription.id_to_symbol.get(market_id)
                        if not symbol:
                            continue
                        for row in payload.get("data", []):
                            if not row.get("bids") or not row.get("asks"):
                                continue
                            bid_price, bid_size = row["bids"][0][:2]
                            ask_price, ask_size = row["asks"][0][:2]
                            self.cache.set(
                                client.name,
                                symbol,
                                Quote(
                                    symbol=symbol,
                                    bid=float(bid_price),
                                    ask=float(ask_price),
                                    bid_size=float(bid_size),
                                    ask_size=float(ask_size),
                                    timestamp_ms=int(row.get("ts", 0)) or None,
                                ),
                            )
            except Exception as exc:
                logger.warning("OKX WebSocket reconnect after error: %s", exc)
                await asyncio.sleep(2)


def _maybe_timestamp_ms(value: object) -> int | None:
    """Convert a timestamp-like value to milliseconds when possible."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            normalized = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            return None
    return None
