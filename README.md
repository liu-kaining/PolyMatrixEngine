# PolyMatrix Engine

> English | [中文](README-zh.md)

**Institutional-grade automated market-making and liquidity engine for [Polymarket](https://polymarket.com).**  
Async architecture, memory-first state, diff quoting, and battle-tested risk controls—built to earn maker rebates and liquidity rewards without blowing up. *Python Web3 trading at its ceiling.*

---

## Why PolyMatrix Engine?

PolyMatrix Engine is not a hobby script. It is a **proprietary-style (prop) trading core** designed for Polymarket’s zero-fee, full-collateral environment. Every design choice targets three things: **throughput**, **capital safety**, and **time priority**.

- **No DB in the hot path.** Tick logic reads inventory from an in-memory state manager; fills update memory first and persist to Postgres asynchronously. That removes the main bottleneck that kills most Python quant systems under load.

- **Diff quoting, not “cancel all then post”.** Only orders that no longer match the target grid are cancelled; missing levels are created. Your resting orders keep their place in the queue and you burn far less API rate limit.

- **Reconciliation with a time guard.** A configurable buffer after the last local fill prevents the watchdog from overwriting your book with stale REST data, so you never get “just filled, then wiped by delayed API”.

- **Capital protection by design.** Before sending orders, the engine checks that total BUY notional fits within your exposure budget and can auto-shrink or drop levels. Maker prices are clamped so you never cross the book and accidentally pay taker cost.

Suitable for **funds and teams** that want a pluggable, auditable engine to run on Polymarket—whether for liquidity rewards, structured market-making, or as the execution layer for higher-level alpha.

---

## Key Capabilities

| Area | What we do |
|------|------------|
| **Performance** | Memory-first inventory; zero Postgres reads in the tick loop; async persist queue with bounded size and graceful drain on shutdown. |
| **Execution** | Diff quoting (keep/cancel/create by signature); preserves time priority and cuts API churn. |
| **Risk** | Kill switch, circuit breaker, per-market exposure cap, reconciliation with timestamp guard, balance precheck before sending orders. |
| **Maker discipline** | Crosses-the-book guard (SELL ≥ best_bid + tick, BUY ≤ best_ask - tick); no accidental taker flow. |
| **Rewards** | Gamma-driven rewards params (min size, max spread); adaptive sizing with a safety fuse so grid budget is never exceeded. |
| **Ops** | Streamlit dashboard (screener, exposure, logs, emergency stop/liquidate); FastAPI control plane; full .env configuration. |

---

## Features

- **Market Data Gateway** — Order book via Polymarket Market WebSocket + REST snapshot; local orderbook merge and top-N BBO published to Redis for engines.
- **In-Memory Inventory State** — `InventoryStateManager` single source of truth for exposure in the hot path; fills update memory immediately and enqueue async DB writes; bounded queue and drain-on-shutdown to avoid OOM and data loss.
- **Unified Pricing (AlphaModel)** — Single anchor from YES book (mid + OBI skew); NO side derived; dynamic spread and inventory-aware state machine (QUOTING / LIQUIDATING / LOCKED_BY_OPPOSITE).
- **Diff Quoting** — Compare current active orders to desired grid by (side, price, size); cancel only stale, create only missing; preserves time priority and reduces CLOB traffic.
- **Balance Precheck** — Before placing a batch, check total BUY notional vs available budget; auto-shrink or drop levels so you don’t hit “not enough balance” from the API.
- **Crosses-the-Book Guard** — Clamp SELL to ≥ best_bid + tick and BUY to ≤ best_ask - tick so orders stay maker-only.
- **Reconciliation Timestamp Guard** — Watchdog skips overwriting local ledger with REST data when a local fill happened within the last N seconds (configurable), avoiding stale overwrites.
- **Rewards Farming Ready** — Read rewards min size and max spread from Gamma; adapt size when safe, else fall back to base size; dashboard shows rewards eligibility.
- **OMS + Circuit Breaker** — Orders and cancels via `py-clob-client`; transient errors trip a circuit breaker; non-transient (e.g. 400) do not; “matched orders can't be canceled” treated as success (INFO).
- **Risk Watchdog** — Per-market exposure check; suspend + cancel all on breach; periodic Polymarket positions sync with tolerance and timestamp guard.
- **Streamlit Dashboard** — Gamma screener, start/stop/liquidate, inventory & PnL, active orders, real-time engine status, logs tail.

---

## Architecture Overview

```
                          ┌─────────────────────────────────────┐
                          │       dashboard (Streamlit)          │
                          │  Screener | Control | Risk | Logs    │
                          └──────────────────┬───────────────────┘
                                              │ HTTP
                                              ▼
                          ┌─────────────────────────────────────┐
                          │           app/main.py (FastAPI)     │
                          │  /markets/{id}/start|stop|liquidate │
                          │  /markets/status | /orders/active   │
                          └──────────────────┬───────────────────┘
                                              │
         ┌───────────────────────────────────┼───────────────────────────────────┐
         │                                   │                                   │
         ▼                                   ▼                                   ▼
┌─────────────────────┐           ┌─────────────────────────┐           ┌─────────────────────┐
│ market_data/gateway │           │ core/inventory_state    │           │ oms/core            │
│ WS + REST → Local   │── tick ──▶│ Memory-first inventory  │           │ place/cancel +      │
│ Orderbook → Redis   │           │ Async persist queue     │           │ CircuitBreaker       │
└─────────────────────┘           └───────────┬─────────────┘           └──────────┬──────────┘
         │                                    │                                    │
         │ tick:{token}                       │ get_snapshot()                     │ create/cancel
         │                                    │ (no DB in loop)                    │
         ▼                                    ▼                                    ▲
┌─────────────────────┐           ┌─────────────────────────┐                     │
│ quoting/engine      │           │ risk/watchdog            │                     │
│ • AlphaModel, FV    │           │ • Exposure check        │─────────────────────┘
│ • Diff quoting      │──────────▶│ • Reconcile + buffer    │   cancel_market_orders
│ • Balance precheck  │ control   │ • suspend pub           │
│ • Cross-book guard  │           └─────────────────────────┘
└─────────────────────┘
         │
         │ order_status (FILLED/CANCELED from user_stream)
         ▼
┌─────────────────────┐
│ market_data/        │
│ user_stream         │ → handle_fill → inventory_state.apply_fill → queue persist
│ (User WS)           │ → handle_cancel → order_status pub
└─────────────────────┘
```

**Data flow (short):** Market WS and User WS feed gateway and user_stream. Gateway publishes **ticks** to Redis. Engines subscribe to **tick** and **control**; they read **inventory from memory only** (InventoryStateManager), compute fair value and grid, then **diff-quote** (cancel stale, create missing) via OMS. Fills update **inventory in memory** and enqueue DB persist. Watchdog monitors exposure and reconciles with Polymarket REST, **skipping overwrite** when a local fill is within the buffer window.

---

## Code Layout

| Path | Description |
|------|-------------|
| `app/core/inventory_state.py` | **In-memory inventory state.** Single source of truth for exposure on the hot path; async bounded queue for DB persist; graceful drain on shutdown. |
| `app/market_data/gateway.py` | Local orderbook from Market WS + REST; publishes snapshots to Redis `tick:{token}` / `ob:{token}`. |
| `app/market_data/user_stream.py` | User WS: trade/cancel events; updates OrderJournal and **inventory_state** (memory + enqueue persist); publishes order_status for engine active-order cleanup. |
| `app/quoting/engine.py` | QuotingEngine: subscribes to tick + control + order_status; **reads inventory from memory**; AlphaModel + grid; **diff quoting** (sync_orders_diff); balance precheck; crosses-the-book guard; rewards-aware sizing. |
| `app/oms/core.py` | OMS: py-clob-client, CircuitBreaker, place/cancel, “matched can’t cancel” as success. |
| `app/risk/watchdog.py` | Exposure check; reconciliation with Polymarket positions API; **timestamp guard** (skip overwrite shortly after local fill). |
| `app/models/` | OrderJournal, InventoryLedger, MarketMeta. |
| `app/core/` | Config, Redis, DB session. |
| `dashboard/` | Streamlit: Gamma screener, start/stop/liquidate, inventory & risk, active orders, engine status, logs. |

---

## Requirements

- Docker and Docker Compose
- PostgreSQL and Redis (provided by docker-compose)
- Polymarket account and USDC
- CLOB API private key (`PK`) and `FUNDER_ADDRESS`

## Install & Run

1. **Clone**

```bash
git clone https://github.com/liukaining/PolyMatrixEngine.git
cd PolyMatrixEngine
```

2. **Configure**

```bash
cp .env.example .env
# Edit .env: PK, FUNDER_ADDRESS, LIVE_TRADING_ENABLED,
# BASE_ORDER_SIZE, GRID_LEVELS, MAX_EXPOSURE_PER_MARKET, etc.
```

3. **Start**

```bash
docker compose up --build -d
```

- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501`

4. **Logs**

```bash
docker compose logs -f api
```

## Main API

- Health: `GET /health`
- Start: `POST /markets/{condition_id}/start`
- Stop: `POST /markets/{condition_id}/stop`
- Liquidate: `POST /markets/{condition_id}/liquidate`
- Risk: `GET /markets/{condition_id}/risk`
- Active orders: `GET /orders/active`
- Status (all markets): `GET /markets/status`

## Environment variables (.env)

Loaded from project root `.env` via `app/core/config.py`. Key variables:

| Variable | Meaning | Default / note |
|----------|---------|----------------|
| `LIVE_TRADING_ENABLED` | Real CLOB orders vs dry-run | `False` = simulate only |
| `MAX_EXPOSURE_PER_MARKET` | Cap per market (USDC); watchdog kill switch | e.g. `15` |
| `EXPOSURE_TOLERANCE` | Ledger vs API diff above which we overwrite | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | Seconds after last local fill to skip REST overwrite | `8.0` |
| `BASE_ORDER_SIZE` | Size per order (min 5) | e.g. `5.0` |
| `GRID_LEVELS` | Grid levels per side | `2` |
| `QUOTE_BASE_SPREAD` | Spread around fair value | `0.02` |

Full reference: see `.env.example` and the tables in [README-zh.md](README-zh.md) (环境变量说明).

## Disclaimer

This software is for education and experimentation. Trading on Polymarket involves significant financial risk. The authors are not responsible for any trading losses.
