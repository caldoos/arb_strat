"""Logging setup for CLI runs and local development."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from arb_strat.config import AppConfig
from arb_strat.notifications.telegram import TelegramNotifier


class TelegramLogHandler(logging.Handler):
    """Forward warning/error log records to the Telegram logs chat."""

    def __init__(self, notifier: TelegramNotifier, level: int) -> None:
        """Create a log handler backed by the shared Telegram notifier."""
        super().__init__(level=level)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        """Send the formatted log record to Telegram and ignore transport failures."""
        try:
            message = self.format(record)
            self.notifier.send_log(message)
        except Exception:
            self.handleError(record)


def configure_logging(config: AppConfig, notifier: TelegramNotifier | None = None) -> None:
    """Configure console, file, and optional Telegram log forwarding."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_path = Path(config.logging.file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers.append(
        RotatingFileHandler(
            log_path,
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
    )

    if notifier and notifier.is_enabled() and config.telegram.enabled:
        telegram_level = getattr(
            logging,
            config.logging.telegram_level.upper(),
            logging.WARNING,
        )
        handlers.append(TelegramLogHandler(notifier=notifier, level=telegram_level))

    for handler in handlers:
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
