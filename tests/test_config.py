"""Tests for config loading and normalization."""

import json

from arb_strat.config import load_config


def test_load_config(tmp_path):
    """Ensure a minimal JSON config is loaded into the typed config model correctly."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "exchanges": [
                    {"name": "binance", "enabled": True, "taker_fee_bps": 10},
                    {"name": "okx", "enabled": True, "taker_fee_bps": 10}
                ],
                "triangular": {"base_assets": ["BTC", "ETH", "SOL"]}
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.enabled_exchange_names() == ("binance", "okx")
    assert config.triangular.base_assets == ("BTC", "ETH", "SOL")
    assert config.telegram.daily_summary_enabled is False
    assert config.telegram.daily_summary_timezone == "UTC"
