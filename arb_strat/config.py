"""Configuration models and JSON loading helpers for the bot."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_EXCHANGES = {"binance", "coinbase", "okx"}


@dataclass(frozen=True)
class ExchangeSettings:
    """Exchange-specific runtime settings such as fees and credential env vars."""
    name: str
    enabled: bool = True
    sandbox: bool = False
    taker_fee_bps: float = 10.0
    api_key_env: str | None = None
    secret_env: str | None = None
    password_env: str | None = None


@dataclass(frozen=True)
class TriangularSettings:
    """Settings that control the single-exchange triangular scanner."""
    enabled: bool = True
    exchanges: tuple[str, ...] = ("binance", "coinbase", "okx")
    base_assets: tuple[str, ...] = ("BTC", "ETH", "SOL")
    settlement_assets: tuple[str, ...] = ("USDT", "USD")
    min_edge_bps: float = 8.0
    max_opportunities: int = 5


@dataclass(frozen=True)
class CrossExchangeSettings:
    """Settings that control the cross-exchange spread scanner."""
    enabled: bool = True
    symbols: tuple[str, ...] = (
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BTC/USD",
        "ETH/USD",
        "SOL/USD",
    )
    min_edge_bps: float = 10.0
    max_opportunities: int = 5


@dataclass(frozen=True)
class LoggingSettings:
    """Settings for console/file logging and Telegram log forwarding."""

    file_path: str = "logs/arb_strat.log"
    max_bytes: int = 5_000_000
    backup_count: int = 3
    telegram_level: str = "WARNING"


@dataclass(frozen=True)
class TelegramSettings:
    """Settings for optional Telegram notifications and heartbeats."""

    enabled: bool = False
    token_env: str = "TELEGRAM_TOKEN"
    notification_chat_id_env: str = "TELEGRAM_NOTIFICATION_CHAT_ID"
    logs_chat_id_env: str = "TELEGRAM_LOGS_CHAT_ID"
    commands_enabled: bool = False
    command_poll_seconds: float = 2.0
    command_long_poll_seconds: int = 10
    notify_on_startup: bool = True
    notify_on_shutdown: bool = True
    notify_on_opportunity: bool = True
    heartbeat_enabled: bool = False
    heartbeat_interval_cycles: int = 30
    min_edge_bps: float = 0.0
    daily_summary_enabled: bool = False
    daily_summary_hour_utc: int = 0
    daily_summary_timezone: str = "UTC"


@dataclass(frozen=True)
class MarketDataSettings:
    """Settings for WebSocket market-data ingestion and REST fallback behavior."""

    enabled: bool = True
    warmup_seconds: float = 2.0
    rest_fallback: bool = True


@dataclass(frozen=True)
class RiskSettings:
    """Execution guardrails used for paper/live risk checks."""

    enabled: bool = True
    allow_live_triangular: bool = False
    allow_live_cross_exchange: bool = True
    min_order_notional: float = 10.0
    max_order_notional: float = 250.0
    max_opportunity_notional: float = 500.0
    reserve_balance_pct: float = 0.05
    max_slippage_bps: float = 10.0
    max_quote_age_ms: int = 3_000
    max_live_orders_per_cycle: int = 2
    max_consecutive_failures: int = 3
    pause_on_partial_fill: bool = True
    pause_on_execution_error: bool = True
    max_asset_balance_by_exchange: dict[str, dict[str, float]] = field(default_factory=dict)
    reconcile_live_orders: bool = True
    reconciliation_poll_seconds: float = 1.0
    reconciliation_max_attempts: int = 3
    cancel_on_partial_failure: bool = True


@dataclass(frozen=True)
class StateSettings:
    """Runtime state persistence settings for executions, errors, and snapshots."""

    enabled: bool = True
    directory: str = "state"
    snapshot_file: str = "runtime_state.json"
    event_log_file: str = "events.jsonl"
    max_recent_records: int = 50


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration assembled from the JSON config file."""
    log_level: str = "INFO"
    poll_interval_seconds: float = 5.0
    quote_capital: float = 1000.0
    dry_run: bool = True
    exchanges: tuple[ExchangeSettings, ...] = field(default_factory=tuple)
    triangular: TriangularSettings = field(default_factory=TriangularSettings)
    cross_exchange: CrossExchangeSettings = field(default_factory=CrossExchangeSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    market_data: MarketDataSettings = field(default_factory=MarketDataSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    state: StateSettings = field(default_factory=StateSettings)

    def enabled_exchange_names(self) -> tuple[str, ...]:
        """Return the names of exchanges that are enabled in the config."""
        return tuple(exchange.name for exchange in self.exchanges if exchange.enabled)


def load_config(path: str | Path) -> AppConfig:
    """Load the JSON config file and convert it into typed config objects."""
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))

    exchanges = tuple(
        _parse_exchange_settings(item) for item in raw.get("exchanges", [])
    )
    if not exchanges:
        raise ValueError("Config must define at least one exchange.")

    return AppConfig(
        log_level=str(raw.get("log_level", "INFO")).upper(),
        poll_interval_seconds=float(raw.get("poll_interval_seconds", 5.0)),
        quote_capital=float(raw.get("quote_capital", 1000.0)),
        dry_run=bool(raw.get("dry_run", True)),
        exchanges=exchanges,
        triangular=_parse_triangular_settings(raw.get("triangular", {})),
        cross_exchange=_parse_cross_settings(raw.get("cross_exchange", {})),
        logging=_parse_logging_settings(raw.get("logging", {})),
        telegram=_parse_telegram_settings(raw.get("telegram", {})),
        market_data=_parse_market_data_settings(raw.get("market_data", {})),
        risk=_parse_risk_settings(raw.get("risk", {})),
        state=_parse_state_settings(raw.get("state", {})),
    )


def _parse_exchange_settings(raw: dict[str, object]) -> ExchangeSettings:
    """Validate and normalize one exchange block from the raw config."""
    name = str(raw["name"]).lower()
    if name not in SUPPORTED_EXCHANGES:
        supported = ", ".join(sorted(SUPPORTED_EXCHANGES))
        raise ValueError(f"Unsupported exchange '{name}'. Supported: {supported}.")

    return ExchangeSettings(
        name=name,
        enabled=bool(raw.get("enabled", True)),
        sandbox=bool(raw.get("sandbox", False)),
        taker_fee_bps=float(raw.get("taker_fee_bps", 10.0)),
        api_key_env=_maybe_string(raw.get("api_key_env")),
        secret_env=_maybe_string(raw.get("secret_env")),
        password_env=_maybe_string(raw.get("password_env")),
    )


def _parse_triangular_settings(raw: dict[str, object]) -> TriangularSettings:
    """Build triangular strategy settings from the raw config dictionary."""
    return TriangularSettings(
        enabled=bool(raw.get("enabled", True)),
        exchanges=tuple(
            str(item).lower()
            for item in raw.get("exchanges", ("binance", "coinbase", "okx"))
        ),
        base_assets=tuple(
            str(item).upper()
            for item in raw.get("base_assets", ("BTC", "ETH", "SOL"))
        ),
        settlement_assets=tuple(
            str(item).upper()
            for item in raw.get("settlement_assets", ("USDT", "USD"))
        ),
        min_edge_bps=float(raw.get("min_edge_bps", 8.0)),
        max_opportunities=int(raw.get("max_opportunities", 5)),
    )


def _parse_cross_settings(raw: dict[str, object]) -> CrossExchangeSettings:
    """Build cross-exchange strategy settings from the raw config dictionary."""
    return CrossExchangeSettings(
        enabled=bool(raw.get("enabled", True)),
        symbols=tuple(
            str(item).upper()
            for item in raw.get(
                "symbols",
                (
                    "BTC/USDT",
                    "ETH/USDT",
                    "SOL/USDT",
                    "BTC/USD",
                    "ETH/USD",
                    "SOL/USD",
                ),
            )
        ),
        min_edge_bps=float(raw.get("min_edge_bps", 10.0)),
        max_opportunities=int(raw.get("max_opportunities", 5)),
    )


def _parse_logging_settings(raw: dict[str, object]) -> LoggingSettings:
    """Build logging settings from the raw config dictionary."""
    return LoggingSettings(
        file_path=str(raw.get("file_path", "logs/arb_strat.log")),
        max_bytes=int(raw.get("max_bytes", 5_000_000)),
        backup_count=int(raw.get("backup_count", 3)),
        telegram_level=str(raw.get("telegram_level", "WARNING")).upper(),
    )


def _parse_telegram_settings(raw: dict[str, object]) -> TelegramSettings:
    """Build Telegram notification settings from the raw config dictionary."""
    return TelegramSettings(
        enabled=bool(raw.get("enabled", False)),
        token_env=str(raw.get("token_env", "TELEGRAM_TOKEN")),
        notification_chat_id_env=str(
            raw.get("notification_chat_id_env", "TELEGRAM_NOTIFICATION_CHAT_ID")
        ),
        logs_chat_id_env=str(raw.get("logs_chat_id_env", "TELEGRAM_LOGS_CHAT_ID")),
        commands_enabled=bool(raw.get("commands_enabled", False)),
        command_poll_seconds=float(raw.get("command_poll_seconds", 2.0)),
        command_long_poll_seconds=int(raw.get("command_long_poll_seconds", 10)),
        notify_on_startup=bool(raw.get("notify_on_startup", True)),
        notify_on_shutdown=bool(raw.get("notify_on_shutdown", True)),
        notify_on_opportunity=bool(raw.get("notify_on_opportunity", True)),
        heartbeat_enabled=bool(raw.get("heartbeat_enabled", False)),
        heartbeat_interval_cycles=int(raw.get("heartbeat_interval_cycles", 30)),
        min_edge_bps=float(raw.get("min_edge_bps", 0.0)),
        daily_summary_enabled=bool(raw.get("daily_summary_enabled", False)),
        daily_summary_hour_utc=int(raw.get("daily_summary_hour_utc", 0)),
        daily_summary_timezone=str(raw.get("daily_summary_timezone", "UTC")),
    )


def _parse_market_data_settings(raw: dict[str, object]) -> MarketDataSettings:
    """Build market-data settings from the raw config dictionary."""
    return MarketDataSettings(
        enabled=bool(raw.get("enabled", True)),
        warmup_seconds=float(raw.get("warmup_seconds", 2.0)),
        rest_fallback=bool(raw.get("rest_fallback", True)),
    )


def _parse_risk_settings(raw: dict[str, object]) -> RiskSettings:
    """Build execution risk settings from the raw config dictionary."""
    return RiskSettings(
        enabled=bool(raw.get("enabled", True)),
        allow_live_triangular=bool(raw.get("allow_live_triangular", False)),
        allow_live_cross_exchange=bool(raw.get("allow_live_cross_exchange", True)),
        min_order_notional=float(raw.get("min_order_notional", 10.0)),
        max_order_notional=float(raw.get("max_order_notional", 250.0)),
        max_opportunity_notional=float(raw.get("max_opportunity_notional", 500.0)),
        reserve_balance_pct=float(raw.get("reserve_balance_pct", 0.05)),
        max_slippage_bps=float(raw.get("max_slippage_bps", 10.0)),
        max_quote_age_ms=int(raw.get("max_quote_age_ms", 3_000)),
        max_live_orders_per_cycle=int(raw.get("max_live_orders_per_cycle", 2)),
        max_consecutive_failures=int(raw.get("max_consecutive_failures", 3)),
        pause_on_partial_fill=bool(raw.get("pause_on_partial_fill", True)),
        pause_on_execution_error=bool(raw.get("pause_on_execution_error", True)),
        reconcile_live_orders=bool(raw.get("reconcile_live_orders", True)),
        reconciliation_poll_seconds=float(raw.get("reconciliation_poll_seconds", 1.0)),
        reconciliation_max_attempts=int(raw.get("reconciliation_max_attempts", 3)),
        cancel_on_partial_failure=bool(raw.get("cancel_on_partial_failure", True)),
        max_asset_balance_by_exchange=_parse_nested_float_mapping(
            raw.get("max_asset_balance_by_exchange", {})
        ),
    )


def _parse_state_settings(raw: dict[str, object]) -> StateSettings:
    """Build runtime state persistence settings from the raw config dictionary."""
    return StateSettings(
        enabled=bool(raw.get("enabled", True)),
        directory=str(raw.get("directory", "state")),
        snapshot_file=str(raw.get("snapshot_file", "runtime_state.json")),
        event_log_file=str(raw.get("event_log_file", "events.jsonl")),
        max_recent_records=int(raw.get("max_recent_records", 50)),
    )


def _maybe_string(value: object) -> str | None:
    """Convert optional config values to strings while preserving missing values."""
    if value in (None, ""):
        return None
    return str(value)


def _parse_nested_float_mapping(raw: object) -> dict[str, dict[str, float]]:
    """Normalize a nested exchange->asset->limit mapping from raw config data."""
    if not isinstance(raw, dict):
        return {}

    parsed: dict[str, dict[str, float]] = {}
    for exchange_name, asset_mapping in raw.items():
        if not isinstance(asset_mapping, dict):
            continue
        parsed[str(exchange_name).lower()] = {
            str(asset).upper(): float(limit)
            for asset, limit in asset_mapping.items()
        }
    return parsed
