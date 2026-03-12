"""Telegram notification helpers for opportunities and operational logs."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Final
from urllib import error, parse, request

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"


@dataclass(frozen=True)
class TelegramNotifier:
    """Small Telegram client that can send alerts to one or two chats."""

    token_env: str = "TELEGRAM_TOKEN"
    notification_chat_id_env: str = "TELEGRAM_NOTIFICATION_CHAT_ID"
    logs_chat_id_env: str = "TELEGRAM_LOGS_CHAT_ID"

    @classmethod
    def from_env(
        cls,
        token_env: str = "TELEGRAM_TOKEN",
        notification_chat_id_env: str = "TELEGRAM_NOTIFICATION_CHAT_ID",
        logs_chat_id_env: str = "TELEGRAM_LOGS_CHAT_ID",
    ) -> "TelegramNotifier":
        """Build a notifier using the default environment variable names."""
        return cls(
            token_env=token_env,
            notification_chat_id_env=notification_chat_id_env,
            logs_chat_id_env=logs_chat_id_env,
        )

    def is_enabled(self) -> bool:
        """Return True when a bot token and at least one chat id are available."""
        token = self.token()
        notification_chat_id = self.notification_chat_id()
        logs_chat_id = self.logs_chat_id()
        return bool(token and (notification_chat_id or logs_chat_id))

    def enabled(self) -> bool:
        """Compatibility alias for checking whether Telegram is configured."""
        return self.is_enabled()

    def send_notification(self, message: str) -> bool:
        """Send a normal high-signal alert to the notification chat."""
        return self.send_chat_message(message=message, chat_id=self.notification_chat_id())

    def send_message(self, message: str) -> bool:
        """Compatibility alias for sending a standard notification message."""
        return self.send_notification(message)

    def send_log(self, message: str) -> bool:
        """Send a warning or error log line to the logs chat."""
        chat_id = self.logs_chat_id() or self.notification_chat_id()
        return self.send_chat_message(message=message, chat_id=chat_id)

    def send_chat_message(self, message: str, chat_id: str) -> bool:
        """Send a plain-text Telegram message to an explicit chat id."""
        return self._send(message=message, chat_id=chat_id)

    def token(self) -> str:
        """Return the configured Telegram bot token from the environment."""
        return os.getenv(self.token_env, "")

    def notification_chat_id(self) -> str:
        """Return the configured notification chat id from the environment."""
        return os.getenv(self.notification_chat_id_env, "")

    def logs_chat_id(self) -> str:
        """Return the configured logs chat id from the environment."""
        return os.getenv(self.logs_chat_id_env, "")

    def _send(self, message: str, chat_id: str) -> bool:
        """Send a plain-text Telegram message and swallow transport failures."""
        token = os.getenv(self.token_env, "")
        if not token or not chat_id:
            return False

        payload = parse.urlencode(
            {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
        ).encode("utf-8")
        endpoint = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
        req = request.Request(endpoint, data=payload, method="POST")

        try:
            with request.urlopen(req, timeout=10) as response:
                body = response.read()
                parsed = json.loads(body.decode("utf-8"))
                return bool(parsed.get("ok"))
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            logger.debug("Telegram send failed: %s", exc)
            return False
