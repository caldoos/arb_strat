"""Cross-exchange arbitrage scanner for simple two-venue spreads."""

from __future__ import annotations

import logging

from arb_strat.config import CrossExchangeSettings
from arb_strat.models import Opportunity, OrderIntent, Quote

logger = logging.getLogger(__name__)


class CrossExchangeScanner:
    """Compare best bid and ask quotes across venues for the same symbol."""

    def scan(
        self,
        clients: dict[str, object],
        settings: CrossExchangeSettings,
        quote_capital: float,
    ) -> list[Opportunity]:
        """Scan configured symbols across exchanges and rank viable spread trades."""
        opportunities: list[Opportunity] = []

        for symbol in settings.symbols:
            available_quotes: list[tuple[str, object, Quote]] = []
            for exchange_name, client in clients.items():
                try:
                    client.load_markets()
                    if symbol not in client.supported_symbols():
                        continue
                    quote = client.fetch_top_of_book(symbol)
                except Exception as exc:
                    logger.warning(
                        "Skipping %s on %s due to market-data error: %s",
                        symbol,
                        exchange_name,
                        exc,
                    )
                    continue
                available_quotes.append((exchange_name, client, quote))

            if len(available_quotes) < 2:
                continue

            asks = sorted(available_quotes, key=lambda item: item[2].ask)
            bids = sorted(available_quotes, key=lambda item: item[2].bid, reverse=True)

            for buy_name, buy_client, buy_quote in asks:
                match = next(
                    (
                        (sell_name, sell_client, sell_quote)
                        for sell_name, sell_client, sell_quote in bids
                        if sell_name != buy_name
                    ),
                    None,
                )
                if match is None:
                    continue

                sell_name, sell_client, sell_quote = match
                opportunity = self._evaluate(
                    symbol=symbol,
                    buy_name=buy_name,
                    buy_client=buy_client,
                    buy_quote=buy_quote,
                    sell_name=sell_name,
                    sell_client=sell_client,
                    sell_quote=sell_quote,
                    quote_capital=quote_capital,
                    min_edge_bps=settings.min_edge_bps,
                )
                if opportunity:
                    opportunities.append(opportunity)
                break

        opportunities.sort(key=lambda item: item.edge_bps, reverse=True)
        return opportunities[: settings.max_opportunities]

    def _evaluate(
        self,
        symbol: str,
        buy_name: str,
        buy_client,
        buy_quote: Quote,
        sell_name: str,
        sell_client,
        sell_quote: Quote,
        quote_capital: float,
        min_edge_bps: float,
    ) -> Opportunity | None:
        """Estimate a two-leg spread trade after fees and return it if profitable enough."""
        base, quote = symbol.split("/")
        trade_amount = min(
            buy_quote.ask_size,
            sell_quote.bid_size,
            quote_capital / buy_quote.ask,
        )
        if trade_amount <= 0:
            return None

        buy_fee = buy_client.taker_fee_bps / 10000.0
        sell_fee = sell_client.taker_fee_bps / 10000.0
        buy_cost = trade_amount * buy_quote.ask * (1.0 + buy_fee)
        sell_proceeds = trade_amount * sell_quote.bid * (1.0 - sell_fee)
        pnl = sell_proceeds - buy_cost
        edge_bps = (pnl / buy_cost) * 10000.0 if buy_cost else 0.0

        if edge_bps < min_edge_bps:
            return None

        return Opportunity(
            strategy="cross_exchange",
            venue=f"{buy_name} -> {sell_name}",
            summary=f"Buy {base} on {buy_name}, sell on {sell_name}",
            edge_bps=edge_bps,
            expected_pnl=pnl,
            pnl_currency=quote,
            orders=(
                OrderIntent(
                    exchange=buy_name,
                    symbol=symbol,
                    side="buy",
                    price=buy_quote.ask,
                    amount=trade_amount,
                    note="cross-exchange buy leg",
                ),
                OrderIntent(
                    exchange=sell_name,
                    symbol=symbol,
                    side="sell",
                    price=sell_quote.bid,
                    amount=trade_amount,
                    note="cross-exchange sell leg",
                ),
            ),
            metadata={"symbol": symbol},
        )
