"""Live execution backend that submits orders through exchange adapters."""

from __future__ import annotations

import logging

from arb_strat.models import Opportunity

logger = logging.getLogger(__name__)


class LiveExecutionError(RuntimeError):
    """Raised when a live multi-leg execution fails part-way through."""

    def __init__(self, message: str, responses: list[dict]) -> None:
        """Store already-accepted exchange responses for later reconciliation."""
        super().__init__(message)
        self.responses = responses


class LiveExecutor:
    """Send strategy-generated limit orders to the configured exchanges."""

    def __init__(self, clients: dict[str, object]) -> None:
        """Store exchange clients used when live execution is enabled."""
        self.clients = clients

    def execute(self, opportunity: Opportunity) -> list[dict]:
        """Submit every order in an opportunity and collect exchange responses."""
        responses: list[dict] = []
        for order in opportunity.orders:
            client = self.clients[order.exchange]
            try:
                response = client.create_limit_order(
                    symbol=order.symbol,
                    side=order.side,
                    amount=order.amount,
                    price=order.price,
                )
                logger.info("Live order accepted by %s for %s", order.exchange, order.symbol)
                responses.append(response)
            except Exception as exc:
                raise LiveExecutionError(
                    f"live order failed on {order.exchange} {order.symbol}: {exc}",
                    responses=responses,
                ) from exc
        return responses
