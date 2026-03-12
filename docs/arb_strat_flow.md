# arb_strat Flow Summary

## Purpose

`arb_strat` is a crypto arbitrage bot with two scanners:

- triangular arbitrage on a single exchange
- cross-exchange arbitrage across multiple venues

The current venue set is:

- Binance
- Coinbase
- OKX

The current triangular asset universe is centered on:

- BTC
- ETH
- SOL

with:

- USD
- USDT

used as settlement assets.

## High-level architecture

The runtime pipeline is:

```text
main.py
-> arb_strat/app.py
-> load .env and config.json
-> configure logging and Telegram services
-> build ArbitrageBot
-> start WebSocket market-data hub
-> run scanners
-> produce opportunities
-> pass opportunities through risk controls
-> paper execute or live execute
-> persist state, logs, and operator data
```

At a package level, the code is split into:

- `app.py`
  - CLI entrypoint
- `config.py`
  - typed runtime configuration
- `service.py`
  - orchestration layer
- `market_data/`
  - WebSocket top-of-book ingestion
- `exchanges/`
  - exchange adapter layer
- `strategies/`
  - opportunity detection logic
- `execution/`
  - risk checks and paper/live execution
- `notifications/`
  - Telegram notifier and command bot
- `state.py`
  - runtime state persistence

## What each bot/component does

### 1. Main arbitrage bot

The main bot is the `ArbitrageBot` in [service.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/service.py).

It is responsible for:

- building exchange clients
- starting market-data subscriptions
- running the scanners
- collecting opportunities
- sending them through execution control
- updating runtime state
- exposing operator-friendly status methods for Telegram

### 2. Telegram notification bot

The notifier in [telegram.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/notifications/telegram.py) is send-only.

It is used for:

- startup/shutdown notifications
- opportunity alerts
- heartbeat alerts
- warning/error log forwarding

### 3. Telegram command bot

The command bot in [telegram_bot.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/notifications/telegram_bot.py) is the operator interface.

It polls Telegram with `getUpdates` and supports:

- `/help`
- `/status`
- `/balances`
- `/positions`
- `/mode`
- `/last`
- `/orders`
- `/open_orders`
- `/fills`
- `/errors`
- `/pnl`
- `/risk`
- `/daily_summary`
- `/pause`
- `/resume`
- `/heartbeat [on|off|status]`

This lets the bot be operated like a lightweight trading daemon instead of just a CLI script.

## Entry phase

The entry phase starts in [main.py](C:/Users/calde/OneDrive/Documents/arb_strat/main.py), which calls [app.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/app.py).

At startup the app does the following:

1. Parse CLI flags.
2. Load `.env`.
3. Load `config.json` into typed config objects.
4. Configure stdout, file logging, and optional Telegram log forwarding.
5. Create the Telegram notifier.
6. Create the `ArbitrageBot`.
7. Create the Telegram command bot.
8. Start command polling.
9. Start the main bot run loop.

Important CLI flags:

- `--once`
  - run one scan cycle and exit
- `--strategy triangular|cross|all`
  - choose which scanner path runs
- `--execute`
  - allow paper execution
- `--live`
  - allow actual exchange order submission, only valid with `--execute`

## Market-data phase

The market-data layer is in [websocket.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/market_data/websocket.py).

Its job is to maintain a shared top-of-book cache for all relevant symbols.

Current feeds:

- Binance
  - `bookTicker`
- Coinbase
  - public `ticker`
- OKX
  - `bbo-tbt`

The market-data flow is:

1. Build symbol subscriptions from configured scanners.
2. Connect to the public WebSocket endpoints.
3. Keep latest bid/ask data in `QuoteCache`.
4. Expose cached quotes to the exchange adapters.
5. Fall back to REST only if `market_data.rest_fallback` is enabled and the cache is cold.

This reduces stale-quote risk versus pure REST polling, but it does not eliminate latency or fill risk.

## Exchange adapter phase

The exchange adapter layer is in [ccxt_adapter.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/exchanges/ccxt_adapter.py).

Its responsibilities are:

- initialize CCXT clients
- load exchange market metadata
- fetch top-of-book quotes
- fetch balances
- normalize amount/price precision
- submit limit orders

This is the abstraction boundary between strategy logic and venue-specific API behavior.

## Strategy phase

The strategy layer contains two scanners.

### Triangular scanner

Implemented in [triangular.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/strategies/triangular.py).

Flow:

1. For each configured settlement asset and asset pair, form cycles like:
   - `USDT -> BTC -> ETH -> USDT`
2. Resolve the correct tradable symbols and trade direction for each leg.
3. Pull the needed quotes from the exchange adapter.
4. Simulate each leg with fees and top-of-book size constraints.
5. Compute expected pnl and edge in bps.
6. Emit an `Opportunity` if the edge exceeds threshold.

### Cross-exchange scanner

Implemented in [cross_exchange.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/strategies/cross_exchange.py).

Flow:

1. For each configured symbol, fetch quotes across venues.
2. Find the cheapest ask and highest bid on different exchanges.
3. Estimate tradable size from quote capital and book depth.
4. Apply fees.
5. Compute expected pnl and edge in bps.
6. Emit an `Opportunity` if the edge exceeds threshold.

## Opportunity model

Opportunities are normalized in [models.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/models.py).

Main shared objects:

- `Quote`
  - best bid/ask snapshot
- `OrderIntent`
  - normalized order plan
- `Opportunity`
  - strategy output with edge, pnl, and order intents
- `ExecutionRecord`
  - stored result of paper/live execution attempt
- `BalanceSnapshot`
  - stored wallet snapshot by exchange
- `ErrorRecord`
  - stored runtime error record

## Execution phase

Execution is no longer a direct call from strategy to exchange.

The current path is:

```text
Opportunity
-> ExecutionController
-> RiskManager
-> PaperExecutor or LiveExecutor
-> StateStore
```

### ExecutionController

Implemented in [controller.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/execution/controller.py).

It does the following:

- reset per-cycle execution counters
- send opportunities through the risk manager
- reject invalid opportunities cleanly
- route valid ones to paper or live execution
- record execution outcomes
- pause live trading if repeated failures occur

### PaperExecutor

Implemented in [paper.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/execution/paper.py).

It does not place orders. It logs and records the execution plan after risk checks.

### LiveExecutor

Implemented in [live.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/execution/live.py).

It submits limit orders through the exchange adapter.

Current live behavior:

- submit orders sequentially
- collect exchange responses
- record submitted order ids immediately
- optionally poll order status after submission
- normalize and store fill data when available
- raise a structured failure if any leg fails after partial submission
- attempt to cancel already-accepted legs when a later leg fails

This is safer than the earlier direct path, but still not full hedge/recovery logic.

## Risk management phase

The main risk logic is in [risk.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/execution/risk.py).

### What is checked before execution

Before paper or live execution, each opportunity is checked for:

- strategy allowlist
  - live triangular is currently disabled by default
  - live cross-exchange is currently enabled
- zero-notional rejection
- max opportunity notional
- max order notional
- min order notional
- exchange min amount / min cost limits
- current quote slippage guard
- max quote age / stale quote rejection
- free balance checks for live execution
- reserve balance buffer
- per-exchange inventory cap checks
- exchange precision normalization

### Current default execution rules

From [config.json](C:/Users/calde/OneDrive/Documents/arb_strat/config.json):

- live triangular execution: `false`
- live cross-exchange execution: `true`
- minimum order notional: `10`
- maximum order notional: `250`
- maximum opportunity notional: `500`
- reserve balance percentage: `5%`
- max slippage: `10 bps`
- max quote age: `3000 ms`
- max live orders per cycle: `2`
- pause on partial fill: `true`
- reconcile live orders: `true`
- reconciliation poll spacing: `1.0s`
- reconciliation max attempts: `3`
- cancel on partial failure: `true`
- per-exchange inventory caps:
  - BTC: `0.25`
  - ETH: `3.0`
  - SOL: `75.0`
  - USD/USDT: `5000.0`
- max consecutive live failures before pause: `3`
- pause on execution error: `true`

### What position sizing looks like now

Position size is not a fixed hardcoded amount per symbol.

It is determined by:

1. the scanner’s proposed order size
2. risk caps on order and opportunity notional
3. available free balance on the exchange
4. the reserve balance buffer
5. exchange precision rounding

So the effective order is scaled down if needed before submission.

### Current limitations of the risk model

The current risk model does **not** yet include:

- portfolio-wide exposure limits
- inventory skew limits by coin
- dynamic volatility scaling
- cross-venue hedging logic for partial fills
- kill switch by pnl drawdown
- maker/taker route optimization

## State tracking phase

Runtime state is persisted by [state.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/state.py).

Stored state includes:

- recent execution records
- recent order status records
- recent fill records
- recent error records
- latest balance snapshot per exchange
- simulated paper pnl totals
- execution pause state
- runtime summary fields
- latest opportunity summary

Files written under `state/`:

- `runtime_state.json`
  - latest snapshot
- `events.jsonl`
  - append-only event stream

This gives the bot a lightweight operator state layer without needing a database yet.

## Operator / monitoring phase

There are 3 operator surfaces now:

- stdout logs
- rotating file logs
- Telegram notifications and commands

Useful operator views:

- `/status`
  - runtime summary
- `/risk`
  - current guardrails
- `/orders`
  - recent execution records
- `/open_orders`
  - current open exchange orders
- `/fills`
  - recent normalized fills
- `/errors`
  - recent failures
- `/balances`
  - live fetched balances
- `/positions`
  - last stored wallet snapshots
- `/pnl`
  - simulated paper pnl totals
- `/pause` and `/resume`
  - execution control without stopping scanners
- `/daily_summary`
  - current rolling daily summary window

## Daily summary phase

The bot maintains a rolling daily summary window in [state.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/state.py).

This accumulates:

- cycles run
- opportunity batches
- total opportunities
- opportunity counts by strategy and venue
- execution status counts
- expected pnl totals by currency
- error count and error sources

When Telegram daily summary is enabled, [service.py](C:/Users/calde/OneDrive/Documents/arb_strat/arb_strat/service.py) sends one summary per day after the configured `daily_summary_hour_utc` is reached in `daily_summary_timezone`, then resets the window for the next report.

## Exit phase

Exit can happen in two main ways:

- `--once`
  - one cycle finishes and the process exits cleanly
- manual stop / shutdown
  - the process stops, Telegram command polling stops, and exchange resources are closed

On exit:

- shutdown notification can be sent
- market-data threads are stopped
- exchange clients are closed

## Current deployment readiness

The bot is much stronger than the initial scaffold, but not fully production-complete yet.

### What is in place

- public WebSocket market data
- structured exchange adapter layer
- strategy separation
- execution controller
- pre-trade risk checks
- pause-on-failure behavior
- runtime state persistence
- operator Telegram commands

### What still needs work before serious capital

- hedge or unwind logic after partial fills
- trade ledger persistence beyond event snapshots
- pnl/drawdown-based kill switch
- portfolio exposure limits
- deployment process manager setup
- secret handling and production config separation

## Short operational summary

If you think about the bot as phases, it is:

1. **Entry**
   - parse config, build services
2. **Market data**
   - maintain live quote cache
3. **Signal**
   - scan triangular and cross-exchange edges
4. **Risk**
   - validate, scale, and allow/reject
5. **Execution**
   - paper log or live submit, reconcile, and recover
6. **State**
   - persist executions, balances, errors, runtime info
7. **Operator control**
   - logs, notifications, Telegram commands, daily summary
8. **Exit**
   - graceful shutdown and cleanup

That is the current end-to-end pipeline for `arb_strat`.
