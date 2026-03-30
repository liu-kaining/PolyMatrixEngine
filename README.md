# PolyMatrix Engine

> English | [中文](README-zh.md)

**Institutional-grade automated market-making and liquidity engine for [Polymarket](https://polymarket.com).**  
Async architecture, memory-first state, diff quoting, and battle-tested risk controls—built to earn maker rebates and liquidity rewards without blowing up. *Python Web3 trading at its ceiling.*

**Latest hardening (v6.3.3+ / v6.4):** on each **Periodic Hard Reset**, the engine first calls the CLOB **`cancel_all`** (wallet-wide) via **`oms.physical_clob_cancel_all_for_hard_reset()`**, then **`asyncio.sleep`** (`HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC`, default **3s**) so USDC locks release, then **local** `cancel_all_orders` + **Data API** reconcile. If reconcile **fails**, the tick **freezes new BUYs**. Per-market exposure uses **strict MTM + pending BUY notional**. Background reconciliation defaults to **60s** (`RECONCILIATION_INTERVAL_SEC`).

---

## Why PolyMatrix Engine?

PolyMatrix Engine is not a hobby script. It is a **proprietary-style (prop) trading core** designed for Polymarket’s zero-fee, full-collateral environment. Every design choice targets three things: **throughput**, **capital safety**, and **time priority**.

- **No DB in the hot path.** Tick logic reads inventory from an in-memory state manager; fills update memory first and persist to Postgres asynchronously. That removes the main bottleneck that kills most Python quant systems under load.

- **Diff quoting, not “cancel all then post”.** Only orders that no longer match the target grid are cancelled; missing levels are created. Your resting orders keep their place in the queue and you burn far less API rate limit.

- **Reconciliation with a time guard — plus post-reset truth.** Periodic full-book reconcile uses Polymarket **Data API** positions with a buffer after local fills to avoid stale overwrites. Separately, after **Periodic Hard Reset** the quoting engine **always** calls `reconcile_single_market(..., force=True)` before rebuilding bids; on **failure/timeout** it enters **BUY freeze** for that tick only (`POST_RESET_RECONCILE_FREEZE`).

- **Capital protection by design.** Before sending orders, the engine checks BUY notional against **`MAX_EXPOSURE_PER_MARKET`** and **`GLOBAL_MAX_BUDGET`** using **MTM inventory + active pending BUYs** (strict path), and can auto-shrink or drop levels. Maker prices are clamped so you never cross the book and accidentally pay taker cost.

Suitable for **funds and teams** that want a pluggable, auditable engine to run on Polymarket—whether for liquidity rewards, structured market-making, or as the execution layer for higher-level alpha.

---

## Key Capabilities

| Area | What we do |
|------|------------|
| **Auto-Router (V6.3)** | Background portfolio manager with strict risk controls. Scans Gamma for high-yield opportunities, enforces event-horizon halts and sector limits, and biases selection toward deep-liquidity mid-term markets. |
| **Performance** | Memory-first inventory; zero Postgres reads in the tick loop; `EngineSupervisor` task registry; async persist queue with bounded size and graceful drain on shutdown. |
| **Execution** | Diff quoting (keep/cancel/create by signature); preserves time priority and cuts API churn. Fail-closed resilient WS with heartbeat dropping detection. |
| **Risk** | Global max budget (`GLOBAL_MAX_BUDGET`). Per-market kill switch on **capital_used**, circuit breaker, **Data API** reconciliation (default **60s** loop + **forced** sync after hard reset), **BUY freeze** if post-reset reconcile fails, **strict MTM** per-market budget, timestamp guard on stale REST overwrites, and balance precheck. |
| **Maker discipline** | Crosses-the-book guard (SELL ≥ best_bid + tick, BUY ≤ best_ask - tick); no accidental taker flow. |
| **Rewards** | Gamma-driven rewards params (min size, max spread); adaptive sizing with a safety fuse so grid budget is never exceeded. |
| **Ops** | Streamlit dashboard (screener, exposure, logs, emergency stop/liquidate); FastAPI control plane; full .env configuration. |

---

## Features

- **V6.3 Auto-Router (Portfolio Manager)** — Fully automated mode. Scans Gamma, ranks markets by a liquidity-aware score, and maintains a top-N portfolio. Enforces **event-horizon halts** (do not hold into binary resolution), **sector/tag concentration limits**, and a **mid-term bias** to avoid dead long-dated markets.
- **Auto-Router scoring (V6.3)** — Hard liquidity filter (`liquidity ≥ 20k`) and time-decay penalty by days-to-resolution to avoid long-dated stagnant markets; expired markets are treated as event-horizon and avoided.
- **Engine Supervisor** — Strict, concurrency-safe lifecycle manager. Guarantees exactly one async task per market side, preventing duplicate instances and ensuring total cleanup upon exit to avoid "ghost orders".
- **Market Data Gateway** — Order book via Polymarket Market WebSocket + REST snapshot; local orderbook merge and top-N BBO published to Redis for engines. Includes a 30s silent-drop detection for resilient reconnects.
- **In-Memory Inventory State** — `InventoryStateManager` single source of truth for exposure in the hot path; fills update memory immediately and enqueue async DB writes; bounded queue and drain-on-shutdown to avoid OOM and data loss.
- **Unified Pricing (AlphaModel)** — Single anchor from YES book (mid + OBI skew); NO side derived; dynamic spread and inventory-aware state machine (QUOTING / GRACEFUL_EXIT / LIQUIDATING / LOCKED_BY_OPPOSITE).
- **Diff Quoting** — Compare current active orders to desired grid by (side, price, size); cancel only stale, create only missing; preserves time priority and reduces CLOB traffic.
- **Balance Precheck** — Before placing a batch, check total BUY notional against BOTH `MAX_EXPOSURE_PER_MARKET` and `GLOBAL_MAX_BUDGET`, using the same **strict MTM + pending** basis as the quoting loop. Auto-shrinks or drops levels to prevent breaching capital ceilings.
- **Crosses-the-Book Guard** — Clamp SELL to ≥ best_bid + tick and BUY to ≤ best_ask - tick so orders stay maker-only.
- **Reconciliation Timestamp Guard** — Watchdog skips overwriting local ledger with REST data when a local fill happened within the last N seconds (configurable), avoiding stale overwrites.
- **Rewards Farming Ready** — Read rewards min size and max spread from Gamma; adapt size when safe, else fall back to base size; dashboard shows rewards eligibility.
- **Ghost order hard reset + CLOB Cancel-All (v6.4)** — Every **~5 minutes**, **Periodic Hard Reset** runs **`client.cancel_all()`** (wallet-wide; fallback: `get_orders` + `cancel_orders`) with **try/except + timeout**, then **mandatory sleep** (`HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC`) and optional **`get_balance_allowance`** for logging. Then **local** `cancel_all_orders(force_evict)` and **`reconcile_single_market(force=True)`**. If reconcile **fails**, that tick **skips new BUYs** (**`POST_RESET_RECONCILE_FREEZE`**).
- **Strict MTM budgeting** — Per-market “used” budget for new BUYs is **`MTM(held YES/NO @ fair value) + pending_yes_buy_notional + pending_no_buy_notional`**. At or above **`MAX_EXPOSURE_PER_MARKET`**, new BUY size is forced to **zero**; grid loop tracks remaining notional so cumulative new bids cannot breach the cap.
- **Faster reconciliation loop** — Watchdog background positions sync interval is **`RECONCILIATION_INTERVAL_SEC`** (default **60** seconds), reducing drift between local state and API when WS is flaky.
- **OMS + Circuit Breaker** — Orders and cancels via `py-clob-client`; transient errors trip a circuit breaker; non-transient (e.g. 400) do not; “matched orders can't be canceled” treated as success (INFO).
- **Risk Watchdog** — Per-market kill switch based on **actual spent capital** (`yes_capital_used` + `no_capital_used`), not pending notional. **`reconcile_positions()`** vs Data API on a timer; **`reconcile_single_market()`** for one condition (used by engine after hard reset). Tolerance + timestamp guard + proportional **capital_used** adjustment on overwrite.
- **Streamlit Dashboard** — Gamma screener, start/stop/liquidate, inventory & PnL, active orders, real-time engine status, logs tail.

---

## Architecture Overview

**External services (Polymarket + infra)**

```
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌─────────────────────────┐
  │ Market WS    │ │ User WS      │ │ CLOB REST    │ │ Gamma API    │ │ Data API                │
  │ (orderbook)  │ │ fills/cancels│ │ orders       │ │ markets/meta │ │ GET /positions?user=…   │
  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └────────────┬────────────┘
         │                │                │                │                       │
         ▼                ▼                ▼                ▼                       ▼
   gateway.py      user_stream.py     oms/core.py    auto_router.py         risk/watchdog.py
   (local book)    (journal+inv)      (place/cancel) (optional portfolio)   reconcile_*()
```

**In-process control & data plane**

```
                          ┌─────────────────────────────────────┐
                          │       dashboard (Streamlit)         │
                          │  Screener | Control | Risk | Logs   │
                          └──────────────────┬──────────────────┘
                                              │ HTTP
                                              ▼
                          ┌─────────────────────────────────────┐
                          │        app/main.py (FastAPI)         │
                          │  start|stop|liquidate | status       │
                          │  + optional Auto-Router task         │
                          └──────────────────┬──────────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │                                    │                                    │
         ▼                                    ▼                                    ▼
┌─────────────────────┐           ┌─────────────────────────┐           ┌─────────────────────┐
│ market_data/gateway │           │ core/inventory_state    │           │ oms/core            │
│ WS+REST → local OB  │── tick ──▶│ Memory-first inventory  │           │ CLOB + circuit brk. │
│ → Redis ob/tick     │           │ Async persist queue     │           │                     │
└─────────────────────┘           └───────────┬─────────────┘           └──────────┬──────────┘
         │                                    │                                    │
         │ tick:{token}                       │ get_snapshot() (no DB in tick)     │ orders
         │ ob:{token}                         │ pending BUY notional per side      │
         ▼                                    ▼                                    ▲
┌─────────────────────┐           ┌─────────────────────────┐                     │
│ quoting/engine      │◀─────────▶│ risk/watchdog           │─────────────────────┘
│ • unified FV, MTM   │ reconcile │ • ~1s exposure check    │   kill: cancel+suspend
│ • hard reset →      │ Data API  │ • periodic reconcile    │
│   reconcile_single  │           │   (RECONCILIATION_…     │
│ • BUY freeze if     │           │    INTERVAL_SEC)        │
│   reconcile fails   │           │ • timestamp guard       │
│ • diff quoting →OMS │           └─────────────────────────┘
└─────────────────────┘
         │
         │ order_status:{condition}:{token}
         ▼
┌─────────────────────┐
│ market_data/        │
│ user_stream         │ → apply_fill → inventory_state (+ persist queue)
│                     │ → order_status pub → engine active_orders
└─────────────────────┘
```

**Data flow (short):** Gateway and user stream consume **Market** and **User** WebSockets. Gateway publishes **ticks** and orderbook KV to **Redis**. Each **QuotingEngine** subscribes to **tick**, **control**, and **order_status**; on every tick it reads **`inventory_state.get_snapshot()`** only (no Postgres in the hot path), computes fair value and grid, enforces **strict MTM + pending** budget and crosses-book guards, then **diff-quotes** via **OMS**. Fills hit **memory first**, then a bounded async persist queue. **Watchdog** runs **per-second** exposure checks against **capital_used** and a **periodic Data API reconciliation** (default **60s**); the **quoting engine** triggers **`reconcile_single_market(..., force=True)`** immediately after **Periodic Hard Reset**. If that call **fails**, the engine **does not** place new **BUY** orders for that tick. Routine reconciliation still **skips overwrite** shortly after a local fill (**`RECONCILIATION_BUFFER_SECONDS`**) to avoid stale API wiping fresh fills.

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
| max_incentive_spread (cents) | `rewardsMaxSpread` → `rewards_max_spread` (price) | We divide cents by 100. Engine keeps `target_spread ≤ rewards_max_spread * 0.95` and validates before placing. |
| Daily reward rate | `rewardsDailyRate` or `clobRewards[0].rewardsDailyRate` → `reward_rate_per_day` | Shown in dashboard “Rewards/day”; used for display and tuning. |

We **do not** implement the full reward formula (order scoring, sampling, epoch normalization). We only **qualify** for rewards by posting orders that satisfy the market’s min size and max spread; Polymarket runs the scoring and distribution.

---

## Code Layout

| Path | Description |
|------|-------------|
| `app/core/inventory_state.py` | **In-memory inventory state.** Single source of truth for exposure on the hot path; async bounded queue for DB persist; graceful drain on shutdown. |
| `app/market_data/gateway.py` | Local orderbook from Market WS + REST; publishes snapshots to Redis `tick:{token}` / `ob:{token}`. |
| `app/market_data/user_stream.py` | User WS: trade/cancel events; updates OrderJournal and **inventory_state** (memory + enqueue persist); publishes order_status for engine active-order cleanup. |
| `app/quoting/engine.py` | QuotingEngine: tick + control + order_status; **memory-only** inventory; unified FV; **strict MTM + pending** BUY budget; **Periodic Hard Reset** → **`reconcile_single_market(force=True)`**; **BUY freeze** on reconcile failure; **diff quoting**; balance precheck; crosses-the-book guard; rewards-aware sizing. |
| `app/oms/core.py` | OMS: py-clob-client, CircuitBreaker, place/cancel, “matched can’t cancel” as success; **`physical_clob_cancel_all_for_hard_reset()`** (v6.4 wallet **cancel_all** + settle sleep + balance log). |
| `app/risk/watchdog.py` | **~1s** exposure check vs **capital_used**; **`reconcile_positions()`** + **`reconcile_single_market()`** vs **Data API**; **`RECONCILIATION_INTERVAL_SEC`** loop; **timestamp guard** after local fills; kill switch → cancel + suspend. |
| `app/core/auto_router.py` | Optional **V6.3** portfolio manager: Gamma scan, scoring, sector/event-horizon limits, start/stop markets. |
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
# BASE_ORDER_SIZE (shares per order, not USDC), GRID_LEVELS, MAX_EXPOSURE_PER_MARKET, etc.
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
| `AUTO_ROUTER_ENABLED` | Enable Auto-Router background portfolio manager | `False` |
| `AUTO_ROUTER_MAX_MARKETS` | Max concurrent markets managed by the router | `4` |
| `AUTO_ROUTER_SCAN_INTERVAL_SEC` | Time between Gamma API scans for rebalancing | `3600` |
| `AUTO_ROUTER_MIN_HOLD_HOURS` | Min-hold before evicting dropped markets (event-horizon bypasses this) | `12.0` |
| `AUTO_ROUTER_MIN_REWARD_POOL` | **V7.0** — skip Gamma markets whose daily reward pool (USD) is below this | `50.0` |
| `POLY_BUILDER_API_KEY` | **V7.1** — official Builder API key for order attribution headers | `""` |
| `POLY_BUILDER_SECRET` | **V7.1** — official Builder secret for HMAC attribution signing | `""` |
| `POLY_BUILDER_PASSPHRASE` | **V7.1** — official Builder passphrase for attribution headers | `""` |
| `EVENT_HORIZON_HOURS` | Markets resolving within this window (or already expired) are avoided/evicted | `24.0` |
| `MAX_EXPOSURE_PER_SECTOR` | Max USD exposure allowed per tag/sector across active markets | `300.0` |
| `MAX_SLOTS_PER_SECTOR` | Max active markets allowed per tag/sector | `2` |
| `GLOBAL_MAX_BUDGET` | Absolute max USDC deployed across ALL markets | `1000.0` |
| `MAX_EXPOSURE_PER_MARKET` | Cap per **binary** market (2 CLOB outcomes); MTM+pending BUY path + watchdog `capital_used` | e.g. `50.0` |
| `MAX_EXPOSURE_CATEGORICAL` | Stricter cap (USDC) when **>2** outcomes (multi-choice); same budget semantics as above | `30.0` |
| `EXPOSURE_TOLERANCE` | Ledger vs API diff above which we overwrite | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | Seconds after last local fill to skip REST overwrite | `8.0` |
| `RECONCILIATION_INTERVAL_SEC` | Watchdog periodic **Data API** positions sync interval | `60` |
| `HARD_RESET_CLOB_CANCEL_ALL_ENABLED` | On periodic hard reset, call CLOB **cancel_all** before local cleanup | `True` |
| `HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC` | Sleep after cancel_all for USDC release | `3.0` |
| `HARD_RESET_CLOB_CANCEL_ALL_TIMEOUT_SEC` | Timeout for **cancel_all** thread call | `45.0` |
| `HARD_RESET_CLOB_BALANCE_FETCH_TIMEOUT_SEC` | Timeout for **get_balance_allowance** after sleep | `20.0` |
| `HARD_RESET_CLOB_WALLET_DEDUP_SEC` | Skip duplicate wallet **cancel_all** if another engine ran within N s (YES+NO engines) | `15.0` |
| `BASE_ORDER_SIZE` | Order **size in outcome shares** (CLOB `size`, not USDC); min **5** shares | e.g. `10.0` |
| `GRID_LEVELS` | Grid levels per side | `2` |
| `QUOTE_BASE_SPREAD` | Spread around fair value | `0.02` |
| `AUTO_TUNE_FOR_REWARDS` | When true, size/spread adapt to Gamma rewards min size and max spread (within risk limits) | `True` |
| `GAMMA_MAX_MARKETS` | Max markets to fetch in dashboard screener (Gamma list) | `50000` |
| `GAMMA_PAGE_LIMIT` | Per-page limit for Gamma list (dashboard) | `2000` |

Full reference: see `.env.example` and the tables in [README-zh.md](README-zh.md) (环境变量说明).

## Technical whitepaper

For system design details, scoring/risk math, and audited failure-mode fixes, see `docs/TECHNICAL_WHITEPAPER_V6_3.md`.

## References

- [Polymarket Market Makers — Overview](https://docs.polymarket.com/cn/market-makers/overview)
- [Polymarket Market Makers — Getting started](https://docs.polymarket.com/cn/market-makers/getting-started)
- [Polymarket — Liquidity rewards](https://docs.polymarket.com/cn/market-makers/liquidity-rewards)
- [Polymarket — Maker rebates](https://docs.polymarket.com/cn/market-makers/maker-rebates)
- [Polymarket Rewards (product)](https://polymarket.com/zh/rewards)

## Disclaimer

This software is for education and experimentation. Trading on Polymarket involves significant financial risk. The authors are not responsible for any trading losses.
