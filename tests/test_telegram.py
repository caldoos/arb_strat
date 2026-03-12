"""Manual Telegram smoke test for local notifier verification."""

from __future__ import annotations

import pathlib
import sys

from dotenv import load_dotenv

project_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")

from arb_strat.notifications.telegram import TelegramNotifier


def main() -> None:
    """Send a one-off Telegram test message using the local .env configuration."""
    notifier = TelegramNotifier()
    if not notifier.enabled():
        raise SystemExit(
            "Telegram notifier is not configured "
            "(missing TELEGRAM_TOKEN or TELEGRAM_NOTIFICATION_CHAT_ID)."
        )

    if not notifier.send_message("arb_strat test notification: Telegram integration OK."):
        raise SystemExit("Telegram send failed. Check bot token, chat id, and bot access.")

    print("Telegram test message sent successfully.")


if __name__ == "__main__":
    main()
