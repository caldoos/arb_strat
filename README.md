# arb_strat

arb_strat is a crypto arbitrage trading framework focused on triangular and cross-exchange opportunity scanning, with safe defaults and a maintainable structure.

It currently includes two scanners:

- triangular arbitrage on a single exchange
- cross-exchange arbitrage across multiple exchanges

The initial exchange set is:

- Binance
- Coinbase
- OKX

The triangular scanner is centered on BTC, ETH, and SOL, with USD and USDT used as settlement assets so it can look for practical loops like `USDT -> BTC -> ETH -> USDT`.

## Core features

- modular Python structure
- dry-run by default
- exchange access behind one adapter layer
- tests for config loading and strategy math
- a config file that is easy to extend

## Layout

```text
main.py
config.json
config.example.json
arb_strat/
  app.py
  config.py
  logging_config.py
  models.py
  service.py
  execution/
  exchanges/
  strategies/
tests/
```

## Setup

Local environment:

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Working files:

- `config.json` is the runtime config used by the app by default.
- `.env` is the local credential file loaded automatically on startup if present.
- `config.example.json` and `.env.example` are kept as reference templates.
- local logs are written to `logs/arb_strat.log` by default.

## Credentials

Dry-run scanning does not require API keys.

For live order submission later, credentials can be loaded from shell environment variables or from `.env`. `.env` is loaded automatically on startup if present.

Examples:

```powershell
$env:BINANCE_API_KEY="..."
$env:BINANCE_API_SECRET="..."
```

Coinbase and OKX also need a passphrase variable if your account setup requires it.

Environment variables used by the default config:

- Binance
  - `BINANCE_API_KEY`
  - `BINANCE_API_SECRET`
- Coinbase
  - `COINBASE_API_KEY`
  - `COINBASE_API_SECRET`
  - `COINBASE_API_PASSPHRASE`
- OKX
  - `OKX_API_KEY`
  - `OKX_API_SECRET`
  - `OKX_API_PASSPHRASE`
- Telegram
  - `TELEGRAM_TOKEN`
  - `TELEGRAM_NOTIFICATION_CHAT_ID`
- `TELEGRAM_LOGS_CHAT_ID`

For live trading, use API keys with trading permission only. Withdrawal permission should stay disabled.

Telegram behavior is controlled through the `telegram` block in `config.json`.

- `enabled`
  - turns Telegram notifications on or off
- `commands_enabled`
  - enables background polling for Telegram slash commands
- `notify_on_startup`
  - sends a startup message when the bot begins running
- `notify_on_shutdown`
  - sends a completion message for one-shot runs or graceful exits
- `notify_on_opportunity`
  - sends opportunity alerts
- `heartbeat_enabled`
  - enables periodic bot-alive messages
- `heartbeat_interval_cycles`
  - controls how often the heartbeat is sent
- `min_edge_bps`
  - only sends opportunity notifications above this edge threshold
- `daily_summary_enabled`
  - enables one scheduled daily Telegram summary
- `daily_summary_hour_utc`
  - the hour when the daily summary is sent in the configured summary timezone
- `daily_summary_timezone`
  - the timezone used for the daily summary schedule, for example `Asia/Singapore`

Supported Telegram commands:

- `/help`
  - shows the available command list
- `/status`
  - current runtime status, cycles, and last scan summary
- `/balances`
  - fetches balances from each configured exchange
- `/positions`
  - shows the latest stored wallet snapshots without refetching
- `/mode`
  - shows current strategy and execution mode
- `/last`
  - shows the most recent in-memory opportunities
- `/orders`
  - shows recent paper/live execution records
- `/open_orders`
  - fetches and shows currently open exchange orders
- `/fills`
  - shows recent normalized fills from reconciled live orders
- `/realized_pnl`
  - shows reconciled realized pnl from the SQLite trade ledger
- `/errors`
  - shows recent scanner and execution errors
- `/pnl`
  - shows accumulated simulated paper pnl totals
- `/risk`
  - shows the current execution guardrails
- `/daily_summary`
  - shows the current rolling daily summary window
- `/pause`
  - pauses live execution while scanning continues
- `/resume`
  - resumes live execution
- `/heartbeat [on|off|status]`
  - inspects or toggles Telegram heartbeat messages at runtime

## Paper mode vs live mode

The bot is safe by default.

- Scan mode
  - fetches market data
  - evaluates opportunities
  - does not attempt execution
- Paper mode
  - enabled with `--execute`
  - logs the order plan that would be sent
  - still does not place live orders
- Live mode
  - enabled only when both `--execute` and `--live` are passed
  - sends limit orders through the configured exchange adapter

Default safe run:

```powershell
python main.py --once
```

Paper execution preview:

```powershell
python main.py --execute --once
```

## Usage

One-shot scan of both strategies:

```powershell
python main.py --once
```

One-shot triangular scan:

```powershell
python main.py --strategy triangular --once
```

Continuous dry-run scan:

```powershell
python main.py
```

Paper execution run:

```powershell
python main.py --execute --once
```

Live execution run:

```powershell
python main.py --execute --live --strategy cross --once
```

Live mode is intentionally gated behind both `--execute` and `--live`.

## Current execution rules

Live trading is still conservative by default.

- live triangular execution is off
- live cross-exchange execution is on
- minimum order notional is `10`
- maximum order notional is `250`
- maximum opportunity notional is `500`
- minimum net profit is `0.25` in USD/USDT terms
- minimum live net edge is `5 bps`
- maximum total open notional is `500`
- maximum daily live loss estimate is `50`
- balance reserve is `5%`
- max allowed quote drift before rejection is `10 bps`
- max quote age before rejection is `750 ms` for cross-exchange and `1000 ms` for triangular
- max live orders per cycle is `2`
- per-exchange inventory caps are configured for BTC, ETH, SOL, USD, and USDT
- expected slippage is estimated from top-of-book depth and spread before execution
- any partial live fill pauses execution immediately
- live orders are reconciled after submission for up to `3` polls with `1s` spacing
- accepted legs are canceled on later-leg failure when possible
- after `3` consecutive live execution failures, live execution is paused automatically

These rules are configured in the `risk` block in `config.json`, and `/risk` returns the current runtime view.

## State tracking

Runtime state is stored under `state/` by default.

- `runtime_state.json`
  - latest runtime snapshot, balances, recent executions, recent order statuses, recent fills, recent errors
- `events.jsonl`
  - append-only event log for executions, order statuses, fills, balance snapshots, pause state changes, and errors
- `arb_strat.db`
  - SQLite ledger for execution groups, order statuses, fills, and realized pnl

The bot now keeps:

- recent opportunities in memory
- recent execution records
- recent error records
- latest wallet snapshots by exchange
- simulated paper pnl totals by currency
- reconciled realized pnl for completed live cross-exchange groups
- execution pause state
- rolling daily summary counters

## Daily summary

When `telegram.daily_summary_enabled` is on, the bot sends one summary per day after `telegram.daily_summary_hour_utc` is reached in `telegram.daily_summary_timezone`.

The summary currently includes:

- cycles run
- opportunity batches
- total opportunities seen
- opportunities by strategy
- execution status counts
- expected pnl totals by currency
- error count

You can also request the current in-progress window manually with:

```text
/daily_summary
```

The live config is currently set to:

- timezone: `Asia/Singapore`
- send hour: `08:00`

## First run

Typical first local run on Windows:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py --once
```

What that command does:

- the bot loads the configured exchanges
- scans the configured symbols and triangular loops
- logs any opportunities that pass the configured threshold
- exits after one cycle because of `--once`

If no edge is available at that moment, the expected output is:

```text
No opportunities found.
```

That is a valid result and just means the current quotes did not pass the configured thresholds.

## Notes

- The exchange layer is built on `ccxt`.
- Public market data now comes from WebSocket feeds for Binance, Coinbase, and OKX, then falls back to REST only if the cache is cold and `market_data.rest_fallback` is left on.
- Order placement is still REST-based, which is normal for a first live version but still slower than a colocated low-latency stack.
- The WebSocket layer is intended to reduce stale-quote risk, not eliminate execution risk. Slippage, partial fills, and exchange-side latency still matter.
- Live execution now passes through risk checks before any order is submitted: strategy allowlist, notional caps, exchange min limits, free-balance checks, slippage guardrails, and an automatic pause on repeated failures.
- Live execution now also applies a post-cost profit gate: expected pnl is reduced by an estimated slippage cost before the trade is allowed through.
- A portfolio-level open-notional cap and a daily live-loss estimate are now enforced to keep live deployment conservative.
- Logging goes to both stdout and `logs/arb_strat.log`.
- Warning and error logs can also be forwarded to the Telegram logs chat when Telegram is enabled.
- Quote and fee assumptions are still simplified. Before using meaningful capital, add order-status reconciliation, partial-fill recovery, persistent trade history, and venue-specific maker/taker routing.
- Live accounting now uses a local SQLite ledger so fills and realized pnl survive process restarts on the same machine.
- Adding more exchanges later is straightforward: extend the supported exchange list, add credentials if needed, and confirm symbol coverage for the markets you care about.

## Market data path

- Binance uses public `bookTicker` streams.
- Coinbase uses the public `ticker` channel.
- OKX uses the public `bbo-tbt` channel.
- The shared in-memory quote cache is warmed up before each scan cycle.
- If `market_data.rest_fallback` is set to `false`, scanners will fail fast when the WebSocket cache is missing instead of silently falling back to REST.

## Tests

```powershell
pytest
```
