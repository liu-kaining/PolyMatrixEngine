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
| **Auto-Router (V4)** | Background portfolio manager. Scans Gamma for highest ROI opportunities, gracefully exits deteriorating markets, and seamlessly swaps in new ones without exceeding capital limits. |
| **Performance** | Memory-first inventory; zero Postgres reads in the tick loop; `EngineSupervisor` task registry; async persist queue with bounded size and graceful drain on shutdown. |
| **Execution** | Diff quoting (keep/cancel/create by signature); preserves time priority and cuts API churn. Fail-closed resilient WS with heartbeat dropping detection. |
| **Risk** | Global max budget (`GLOBAL_MAX_BUDGET`) enforcing across all engines. Per-market kill switch, circuit breaker, reconciliation with timestamp guard, and balance precheck. |
| **Maker discipline** | Crosses-the-book guard (SELL ≥ best_bid + tick, BUY ≤ best_ask - tick); no accidental taker flow. |
| **Rewards** | Gamma-driven rewards params (min size, max spread); adaptive sizing with a safety fuse so grid budget is never exceeded. |
| **Ops** | Streamlit dashboard (screener, exposure, logs, emergency stop/liquidate); FastAPI control plane; full .env configuration. |

---

## Features

- **V4.0 Auto-Router (Portfolio Manager)** — Fully automated mode. When enabled, it continuously scans the Polymarket Gamma API, ranks binary markets by Daily ROI (Rewards per Day / Min Size), filters out blacklisted markets, and maintains a top-N portfolio. It handles graceful eviction (sell-only mode) for falling markets while rotating in new ones.
- **Engine Supervisor** — Strict, concurrency-safe lifecycle manager. Guarantees exactly one async task per market side, preventing duplicate instances and ensuring total cleanup upon exit to avoid "ghost orders".
- **Market Data Gateway** — Order book via Polymarket Market WebSocket + REST snapshot; local orderbook merge and top-N BBO published to Redis for engines. Includes a 30s silent-drop detection for resilient reconnects.
- **In-Memory Inventory State** — `InventoryStateManager` single source of truth for exposure in the hot path; fills update memory immediately and enqueue async DB writes; bounded queue and drain-on-shutdown to avoid OOM and data loss.
- **Unified Pricing (AlphaModel)** — Single anchor from YES book (mid + OBI skew); NO side derived; dynamic spread and inventory-aware state machine (QUOTING / GRACEFUL_EXIT / LIQUIDATING / LOCKED_BY_OPPOSITE).
- **Diff Quoting** — Compare current active orders to desired grid by (side, price, size); cancel only stale, create only missing; preserves time priority and reduces CLOB traffic.
- **Balance Precheck** — Before placing a batch, check total BUY notional against BOTH `MAX_EXPOSURE_PER_MARKET` and `GLOBAL_MAX_BUDGET`. Auto-shrinks or drops levels to absolutely prevent hitting capital ceilings.
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

## Alignment with Polymarket market-maker docs

PolyMatrix Engine implements the **official Polymarket market-maker (MM) workflow**. The logic and data sources match the docs below.

| Doc | What it describes | How we align |
|-----|--------------------|--------------|
| [Overview](https://docs.polymarket.com/cn/market-makers/overview) | MM = post limit orders, provide liquidity, use WebSocket + Gamma + CLOB | We use **Gamma API** for metadata, **CLOB REST** (py-clob-client) for orders; no DB in tick loop; cross-book guard so we stay maker-only. |
| [Getting started](https://docs.polymarket.com/cn/market-makers/getting-started) | Top up USDC.e, deploy wallet, approve tokens, derive API creds from wallet | We use **ClobClient** with `create_or_derive_api_creds()` and POLY_PROXY (gasless). Top-up and approvals are done outside the app. |
| [Liquidity rewards](https://docs.polymarket.com/cn/market-makers/liquidity-rewards) | Orders **within max spread** and **≥ min size** count for daily rewards; params from Markets API | We read **rewardsMinSize** / **rewardsMaxSpread** (and **rewardsDailyRate** from Gamma / `clobRewards`); engine enforces size ≥ min and spread ≤ max (with margin). |
| [Maker rebates](https://docs.polymarket.com/cn/market-makers/maker-rebates) | In fee-enabled markets (e.g. crypto), taker fees fund daily USDC rebates to makers whose orders get filled | We are makers by design (limit orders only). Rebates are paid by Polymarket; we do not compute them. |

### Liquidity rewards: field mapping

Official docs use **min_incentive_size** and **max_incentive_spread** (from Markets API). We use the same data from Gamma:

| Official / Markets API | Gamma / our code | Notes |
|------------------------|------------------|--------|
| min_incentive_size (shares) | `rewardsMinSize` → `rewards_min_size` | Engine uses `max(base_size, rewards_min_size)` and, when `AUTO_TUNE_FOR_REWARDS=True`, targets `rewards_min_size * 1.05` with a safety cap. |
| max_incentive_spread (cents) | `rewardsMaxSpread` → `rewards_max_spread` (price) | We divide cents by 100. Engine keeps `target_spread ≤ rewards_max_spread * 0.90` and validates before placing. |
| Daily reward rate | `rewardsDailyRate` or `clobRewards[0].rewardsDailyRate` → `reward_rate_per_day` | Shown in dashboard “Rewards/day”; used for display and tuning. |

We **do not** implement the full reward formula (order scoring, sampling, epoch normalization). We only **qualify** for rewards by posting orders that satisfy the market’s min size and max spread; Polymarket runs the scoring and distribution.

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

## Dashboard & screener

The Streamlit dashboard (port **8501**) provides:

- **Market screener** — Load active markets from Gamma (`active=true`, `closed=false`). V3.0 Farming: binary only, hard filters (rewards_min_size 1–250, liquidity ≥ 1k), sports/gambling blacklist. No mode selector.
- **Scoring** — V3.0 Farming Score 0–100: yield (50), safety skew (30), quietness (20). Stars 1–5 from score bands; table shows Stars, Score, Rewards/day, Min size, Spread (¢), **Competition** (Gamma `competitive`).
- **Pool size** — Caption shows “Pool: **X** (from **Y** loaded)”: X = markets passing the screener, Y = raw count from Gamma. Env `GAMMA_MAX_MARKETS` (default 50k) and `GAMMA_PAGE_LIMIT` (default 2k) control fetch size.
- **Filters** — Optional: 4+ stars only, rewards-only (min size &gt; 0), low competition (&lt; 60%).
- **Control** — Start / stop / liquidate per market; inventory & PnL; active orders; engine status; log tail.

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
| `AUTO_ROUTER_ENABLED` | Enable V4 Auto-Router background portfolio manager | `False` |
| `AUTO_ROUTER_MAX_MARKETS` | Max concurrent markets managed by the router | `4` |
| `AUTO_ROUTER_SCAN_INTERVAL_SEC` | Time between Gamma API scans for rebalancing | `3600` |
| `GLOBAL_MAX_BUDGET` | Absolute max USDC deployed across ALL markets | `1000.0` |
| `MAX_EXPOSURE_PER_MARKET` | Cap per market (USDC); watchdog kill switch | e.g. `50.0` |
| `EXPOSURE_TOLERANCE` | Ledger vs API diff above which we overwrite | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | Seconds after last local fill to skip REST overwrite | `8.0` |
| `BASE_ORDER_SIZE` | Size per order (min 5) | e.g. `5.0` |
| `GRID_LEVELS` | Grid levels per side | `2` |
| `QUOTE_BASE_SPREAD` | Spread around fair value | `0.02` |
| `AUTO_TUNE_FOR_REWARDS` | When true, size/spread adapt to Gamma rewards min size and max spread (within risk limits) | `True` |
| `GAMMA_MAX_MARKETS` | Max markets to fetch in dashboard screener (Gamma list) | `50000` |
| `GAMMA_PAGE_LIMIT` | Per-page limit for Gamma list (dashboard) | `2000` |

Full reference: see `.env.example` and the tables in [README-zh.md](README-zh.md) (环境变量说明).

## References

- [Polymarket Market Makers — Overview](https://docs.polymarket.com/cn/market-makers/overview)
- [Polymarket Market Makers — Getting started](https://docs.polymarket.com/cn/market-makers/getting-started)
- [Polymarket — Liquidity rewards](https://docs.polymarket.com/cn/market-makers/liquidity-rewards)
- [Polymarket — Maker rebates](https://docs.polymarket.com/cn/market-makers/maker-rebates)
- [Polymarket Rewards (product)](https://polymarket.com/zh/rewards)

## Disclaimer

This software is for education and experimentation. Trading on Polymarket involves significant financial risk. The authors are not responsible for any trading losses.
