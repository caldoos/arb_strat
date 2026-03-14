"""Telegram command bot that answers lightweight operational requests."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib import error, parse, request

from arb_strat.config import TelegramSettings
from arb_strat.notifications.telegram import TELEGRAM_API_BASE, TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandHandlers:
    """Bot callbacks used to answer operator commands."""

    status: Callable[[], str]
    balances: Callable[[], str]
    positions: Callable[[], str]
    mode: Callable[[], str]
    last: Callable[[], str]
    orders: Callable[[], str]
    open_orders: Callable[[], str]
    fills: Callable[[], str]
    realized_pnl: Callable[[], str]
    errors: Callable[[], str]
    pnl: Callable[[], str]
    risk: Callable[[], str]
    daily_summary: Callable[[], str]
    pause: Callable[[], str]
    resume: Callable[[], str]
    heartbeat: Callable[[str | None], str]


class TelegramCommandBot:
    """Background long-polling command bot for operator Telegram chats."""

    def __init__(
        self,
        notifier: TelegramNotifier,
        settings: TelegramSettings,
        handlers: CommandHandlers,
    ) -> None:
        """Store bot settings and callbacks for later background polling."""
        self.notifier = notifier
        self.settings = settings
        self.handlers = handlers
        self._allowed_chat_ids = {
            chat_id
            for chat_id in (
                self.notifier.notification_chat_id(),
                self.notifier.logs_chat_id(),
            )
            if chat_id
        }
        self._offset = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start Telegram command polling when credentials and settings allow it."""
        if not self.settings.enabled or not self.settings.commands_enabled:
            return
        if not self.notifier.token():
            return
        if not self._allowed_chat_ids:
            logger.warning("Telegram command bot disabled: no allowed chat ids configured.")
            return
        if self._thread is not None:
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            name="telegram-command-bot",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Telegram command bot started for chats: %s",
            ", ".join(sorted(self._allowed_chat_ids)),
        )

    def stop(self) -> None:
        """Stop polling for Telegram updates and wait briefly for shutdown."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.settings.command_long_poll_seconds + 2)

    def _run_loop(self) -> None:
        """Continuously poll Telegram updates until shutdown is requested."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                logger.warning("Telegram command bot polling error: %s", exc)
                time.sleep(self.settings.command_poll_seconds)

    def _poll_once(self) -> None:
        """Fetch one batch of updates and handle any supported commands."""
        updates = self._get_updates()
        for update in updates:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            self._handle_update(update)

        if not updates:
            time.sleep(self.settings.command_poll_seconds)

    def _get_updates(self) -> list[dict]:
        """Fetch pending bot updates from Telegram using long polling."""
        query = parse.urlencode(
            {
                "timeout": self.settings.command_long_poll_seconds,
                "offset": self._offset,
            }
        )
        endpoint = f"{TELEGRAM_API_BASE}/bot{self.notifier.token()}/getUpdates?{query}"

        try:
            with request.urlopen(
                endpoint,
                timeout=self.settings.command_long_poll_seconds + 5,
            ) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("Telegram getUpdates failed: %s", exc)
            return []

        if not parsed.get("ok"):
            logger.warning("Telegram getUpdates returned not-ok response: %s", parsed)
            return []
        return list(parsed.get("result", []))

    def _handle_update(self, update: dict) -> None:
        """Handle one Telegram message update if it is an authorized command."""
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = str(message.get("text", "")).strip()
        if not chat_id or not text.startswith("/"):
            return
        if chat_id not in self._allowed_chat_ids:
            logger.info("Ignoring Telegram command from unauthorized chat %s", chat_id)
            return

        command, args = self._parse_command(text)
        response = self._dispatch(command, args)
        self.notifier.send_chat_message(response, chat_id=chat_id)

    def _parse_command(self, text: str) -> tuple[str, list[str]]:
        """Normalize a Telegram slash-command and split out any arguments."""
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        return command, [part.lower() for part in parts[1:]]

    def _dispatch(self, command: str, args: list[str]) -> str:
        """Route a normalized command to the appropriate handler."""
        if command in ("/start", "/help"):
            return self._help_text()
        if command == "/status":
            return self.handlers.status()
        if command == "/balances":
            return self.handlers.balances()
        if command == "/positions":
            return self.handlers.positions()
        if command == "/mode":
            return self.handlers.mode()
        if command == "/last":
            return self.handlers.last()
        if command == "/orders":
            return self.handlers.orders()
        if command == "/open_orders":
            return self.handlers.open_orders()
        if command == "/fills":
            return self.handlers.fills()
        if command == "/realized_pnl":
            return self.handlers.realized_pnl()
        if command == "/errors":
            return self.handlers.errors()
        if command == "/pnl":
            return self.handlers.pnl()
        if command == "/risk":
            return self.handlers.risk()
        if command == "/daily_summary":
            return self.handlers.daily_summary()
        if command == "/pause":
            return self.handlers.pause()
        if command == "/resume":
            return self.handlers.resume()
        if command == "/heartbeat":
            action = args[0] if args else None
            return self.handlers.heartbeat(action)
        return "Unknown command. Use /help."

    def _help_text(self) -> str:
        """Return the supported Telegram command list."""
        return (
            "arb_strat commands\n"
            "/help - show this message\n"
            "/status - runtime status and last scan summary\n"
            "/balances - fetch balances for each configured exchange\n"
            "/positions - show latest stored wallet snapshots\n"
            "/mode - current strategy and execution mode\n"
            "/last - last opportunities seen in memory\n"
            "/orders - recent paper/live execution records\n"
            "/open_orders - currently open exchange orders\n"
            "/fills - recent normalized fills\n"
            "/realized_pnl - reconciled realized pnl from SQLite ledger\n"
            "/errors - recent scanner/execution errors\n"
            "/pnl - simulated pnl totals by currency\n"
            "/risk - current execution and risk rules\n"
            "/daily_summary - current daily summary window\n"
            "/pause - pause live execution\n"
            "/resume - resume live execution\n"
            "/heartbeat [on|off|status] - inspect or toggle Telegram heartbeats"
        )
