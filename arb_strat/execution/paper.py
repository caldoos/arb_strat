"""Paper execution backend that logs intended orders instead of sending them."""

from __future__ import annotations

import logging

from arb_strat.models import Opportunity

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Emit order plans to logs so strategies can be tested safely."""

    def execute(self, opportunity: Opportunity) -> None:
        """Log a detected opportunity and each order in its execution plan."""
        logger.info(
            "[paper] %s %.2f bps expected pnl %.6f %s - %s",
            opportunity.strategy,
            opportunity.edge_bps,
            opportunity.expected_pnl,
            opportunity.pnl_currency,
            opportunity.summary,
        )
        for order in opportunity.orders:
            logger.info(
                "[paper] %s %s %.8f %s @ %.8f",
                order.exchange,
                order.side.upper(),
                order.amount,
                order.symbol,
                order.price,
            )
