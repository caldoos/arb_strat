"""Command-line entrypoint and argument parsing for arb_strat."""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from arb_strat.config import load_config
from arb_strat.logging_config import configure_logging
from arb_strat.notifications import CommandHandlers, TelegramCommandBot, TelegramNotifier
from arb_strat.service import ArbitrageBot


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser used to control scan and execution modes."""
    parser = argparse.ArgumentParser(
        description="arb_strat - triangular and cross-exchange scanner",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to the JSON config file.",
    )
    parser.add_argument(
        "--strategy",
        choices=("triangular", "cross", "all"),
        default="all",
        help="Which scanner to run.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan cycle and exit.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Generate and send order plans. Without --live this stays in paper mode.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow live order submission. Requires --execute and exchange credentials.",
    )
    return parser


def main() -> int:
    """Parse CLI arguments, start the bot, and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args()
    if args.live and not args.execute:
        parser.error("--live requires --execute")

    load_dotenv()
    config = load_config(args.config)
    notifier = TelegramNotifier.from_env(
        token_env=config.telegram.token_env,
        notification_chat_id_env=config.telegram.notification_chat_id_env,
        logs_chat_id_env=config.telegram.logs_chat_id_env,
    )
    configure_logging(config, notifier=notifier)
    bot = ArbitrageBot(config, notifier=notifier)
    command_bot = TelegramCommandBot(
        notifier=notifier,
        settings=config.telegram,
        handlers=CommandHandlers(
            status=bot.format_status,
            balances=bot.format_balances,
            positions=bot.format_positions,
            mode=bot.format_mode,
            last=bot.format_last_opportunities,
            orders=bot.format_orders,
            open_orders=bot.format_open_orders,
            fills=bot.format_fills,
            realized_pnl=bot.format_realized_pnl,
            errors=bot.format_errors,
            pnl=bot.format_pnl,
            risk=bot.format_risk,
            daily_summary=bot.format_daily_summary,
            pause=bot.pause_execution,
            resume=bot.resume_execution,
            heartbeat=bot.handle_heartbeat_command,
        ),
    )

    try:
        command_bot.start()
        return bot.run(
            strategy=args.strategy,
            once=args.once,
            execute=args.execute,
            live=args.live,
        )
    finally:
        command_bot.stop()
        bot.close()
