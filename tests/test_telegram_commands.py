"""Tests for Telegram command parsing and dispatch."""

from arb_strat.config import TelegramSettings
from arb_strat.notifications.telegram_bot import CommandHandlers, TelegramCommandBot


class FakeNotifier:
    """Minimal notifier double for command-bot unit tests."""

    def __init__(self) -> None:
        """Initialize fixed token/chat ids and a sent-message buffer."""
        self.sent_messages: list[tuple[str, str]] = []

    def token(self) -> str:
        """Return a fake bot token."""
        return "token"

    def notification_chat_id(self) -> str:
        """Return the allowed notification chat id."""
        return "111"

    def logs_chat_id(self) -> str:
        """Return the allowed logs chat id."""
        return "222"

    def send_chat_message(self, message: str, chat_id: str) -> bool:
        """Record outgoing command responses instead of sending them."""
        self.sent_messages.append((chat_id, message))
        return True


def _build_bot() -> tuple[TelegramCommandBot, FakeNotifier]:
    """Create a command bot with deterministic fake handlers."""
    notifier = FakeNotifier()
    bot = TelegramCommandBot(
        notifier=notifier,
        settings=TelegramSettings(enabled=True, commands_enabled=True),
        handlers=CommandHandlers(
            status=lambda: "status ok",
            balances=lambda: "balances ok",
            positions=lambda: "positions ok",
            mode=lambda: "mode ok",
            last=lambda: "last ok",
            orders=lambda: "orders ok",
            open_orders=lambda: "open orders ok",
            fills=lambda: "fills ok",
            realized_pnl=lambda: "realized pnl ok",
            errors=lambda: "errors ok",
            pnl=lambda: "pnl ok",
            risk=lambda: "risk ok",
            daily_summary=lambda: "daily summary ok",
            pause=lambda: "paused",
            resume=lambda: "resumed",
            heartbeat=lambda action: f"heartbeat {action or 'status'}",
        ),
    )
    return bot, notifier


def test_command_bot_help_dispatch():
    """Ensure /help commands generate the built-in command list."""
    bot, notifier = _build_bot()

    bot._handle_update({"message": {"chat": {"id": "111"}, "text": "/help"}})

    assert notifier.sent_messages
    assert "/status" in notifier.sent_messages[0][1]
    assert "/realized_pnl" in notifier.sent_messages[0][1]


def test_command_bot_rejects_unauthorized_chat():
    """Ensure commands from chats outside the configured allowlist are ignored."""
    bot, notifier = _build_bot()

    bot._handle_update({"message": {"chat": {"id": "999"}, "text": "/status"}})

    assert notifier.sent_messages == []


def test_command_bot_heartbeat_dispatch_with_argument():
    """Ensure command arguments are normalized and passed through."""
    bot, notifier = _build_bot()

    bot._handle_update({"message": {"chat": {"id": "111"}, "text": "/heartbeat ON"}})

    assert notifier.sent_messages == [("111", "heartbeat on")]


def test_command_bot_realized_pnl_dispatch():
    """Ensure realized-pnl requests route to the ledger-backed handler."""
    bot, notifier = _build_bot()

    bot._handle_update({"message": {"chat": {"id": "111"}, "text": "/realized_pnl"}})

    assert notifier.sent_messages == [("111", "realized pnl ok")]
