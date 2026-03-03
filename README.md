# PolyMatrix Engine

> English | [中文](README-zh.md)

PolyMatrix Engine is an automated market-making and statistical arbitrage engine for [Polymarket](https://polymarket.com). It uses an async architecture and strict order-state management to earn maker rebates and liquidity incentives in a zero-fee environment, with grid-style liquidity and tight risk controls.

## Features

- **Market Data Gateway** — order book ingestion and tick distribution

## Architecture Overview

```
                                     ┌─────────────────────────────┐
                                     │     dashboard/app.py        │
                                     │ (Streamlit UI + Gamma filter)│
                                     └──────────┬──────────────────┘
                                                │
                                   HTTP/REST   │
                                                ▼
                             ┌────────────────────────────────┐
                             │          app/main.py            │
                             │ FastAPI: start/stop/liquidate   │
                             │ /admin/wipe /markets/status     │
                             └──────────┬─────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
        ▼                               ▼                               ▼
┌─────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────┐
│ app/market_data/    │      │ app/quoting/engine.py      │      │ app/oms/core.py       │
│ gateway.py (WS)     │──────│ QuotingEngine + AlphaModel │──────│ OrderManagementSystem │
│ LocalOrderbook      │ tick │ ScoreEngine (planned)     │ diff │ CircuitBreaker, OMS   │
└─────────────────────┘      └──────────┬───────────────┘      └──────────┬───────────┘
                                        │                                 │
                                        │ Redis pub/sub (tick/control)    │
                                        ▼                                 ▼
                             ┌──────────────────────────┐      ┌──────────────────────┐
                             │ app/core/redis.py        │      │ app/models/db_models.py│
                             │ RedisManager + state API │      │ OrderJournal, Inventory│
                             └──────────────────────────┘      │ Ledger, MarketMeta     │
                                                                 └──────────────────────┘
                                                                            ▲
                                                                            │
                                                    ┌───────────────────────┴────────┐
                                                    │ app/risk/watchdog.py            │
                                                    │ 1s exposure check + control pub │
                                                    │ 5min Polymarket positions sync  │
                                                    └──────────────────────────────────┘
```

## Code Layout

| Path | Description |
|------|-------------|
| `app/market_data/` | Maintains `LocalOrderbook` via Market WS + REST; publishes snapshots to Redis `tick:{token}` / `ob:{token}`. |
| `app/quoting/` | QuotingEngine + AlphaModel/ScoreEngine: subscribes to Redis tick/control, computes fair value, spread, and grid, then calls OMS to execute. |
| `app/oms/` | Order Management System: wraps `py-clob-client`, CircuitBreaker, and DB state; places/cancels orders and audits. |
| `app/risk/` | Watchdog: checks exposure limits, publishes `control:{condition_id}`, and reconciles with Polymarket positions API. |
| `app/models/` | ORM: `OrderJournal`, `InventoryLedger`, `MarketMeta` — all order and exposure audit state. |
| `app/core/` | Config, Redis, DB session, shared utilities. |
| `dashboard/` | Streamlit dashboard: Gamma screener, logs, emergency controls; observes engine via REST/logs/Redis. |
| `spec/` | Architecture and strategy docs (e.g. `architecture_summary.md`). |

### Market Data Gateway

- Subscribes to Polymarket Market WebSocket for target `token_id` order book updates.
- Merges deltas into a local `LocalOrderbook` and produces **Top-5 BBO** snapshots.
- For each token: writes latest snapshot to Redis `ob:{token_id}`, publishes ticks to `tick:{token_id}` for QuotingEngine.
- On `/markets/{condition_id}/start`, triggers an initial full book snapshot via CLOB REST `GET /book?token_id=...` so the engine works even when the market is quiet.

### Quoting Engine

- Subscribes to Redis: `tick:{token_id}` (book snapshot), `control:{condition_id}` (`suspend` / `resume`).
- AlphaModel uses BBO mid, order-book imbalance (OBI), and current Yes/No inventory to compute **dynamic Fair Value** and **dynamic Spread**.
- **Inventory-aware asymmetric quoting (V1.1):**
  - **Neutral (light inventory):** When \|exposure\| &lt; 5, engine only posts BUY grid (by `GRID_LEVELS` / `BASE_ORDER_SIZE`), no SELL side by default.
  - **Long (heavy inventory):** When exposure ≥ 5, engine switches to liquidation: no BUY, single or few **Aggressive SELL** at `Ask = min(FairValue + 0.01, BestAsk - 0.01)` clipped to [0.01, 0.99], size ≥ 5 and ≤ position.
- `BASE_ORDER_SIZE` is enforced as `max(5.0, BASE_ORDER_SIZE)` to satisfy Polymarket min size.
- On each tick: read `InventoryLedger` / `MarketMeta`, compute exposure, pass to AlphaModel; debounce small fair-value moves; generate orders (Neutral = buy only, Long = sell only); **always cancel all active orders first, then post new grid**.
- Control: `suspend` → set `suspended=True`, `cancel_all_orders()` (kill switch); `resume` → back to normal.

### OMS (Order Management System)

- Integrates with Polymarket CLOB via `py-clob-client`.
- Order flow: insert `OrderJournal` as `PENDING` (local ID) → `CircuitBreaker` → `create_and_post_order()` (in thread pool) → on success set `OPEN` with real order ID, on failure set `FAILED` with error payload.
- Cancel: `client.cancel(order_id)`, then mark `CANCELED`; optionally check `size_matched` after cancel for partial-fill cases.
- **CircuitBreaker:** repeated API errors (e.g. 400 balance/min size) increment failure count; above threshold → stop new orders/cancels; later auto-recovery to closed.
- **Dry-run:** When `LIVE_TRADING_ENABLED=False` or ClobClient not initialized, all place/cancel are simulated in DB only (`[DRY-RUN]`), no real CLOB calls.

### Risk Watchdog

- Background loop: read `InventoryLedger`, check per-market Yes/No exposure.
- If exposure &gt; `MAX_EXPOSURE_PER_MARKET`: publish Redis `control:{condition_id}` → `{"action": "suspend"}`, then `oms.cancel_market_orders(condition_id)`.

### Dashboard (Streamlit)

- **Control Panel:** Enter condition ID or Polymarket URL; two-step confirm to call `/markets/{condition_id}/start` (Gamma tokenIds, Market WS + User WS, QuotingEngine, initial snapshot + first grid).
- **Emergency:** `Stop` → confirm → `/markets/{id}/stop` (cancel + suspend); `Liquidate All` → confirm → `/markets/{id}/liquidate` (cancel + dump at 0.01).
- **Inventory & Risk:** Active markets count, total realized PnL, total gross exposure; expandable Market Exposures chart; Inventory Ledger table with Gamma/Polymarket links.
- **Active Orders:** OPEN/PENDING from `orders_journal` (times in Asia/Shanghai).
- **Market Screener (Gamma):** Fetch active binary markets, filter out Sports/Live; Conservative / Normal / Aggressive / Ultra filters; select row → confirm → start.
- **System Logs:** Tail and search `data/logs/trading.log` (RotatingFileHandler, 5MB × 3 backups).

Logs use `TZ=Asia/Shanghai` (UTC+8) for timestamps.

## Requirements

- Docker and Docker Compose
- PostgreSQL and Redis (provided by docker-compose)
- Polymarket account and USDC.e
- CLOB API private key (`PK`) and `FUNDER_ADDRESS`

## Install & Run

1. **Clone**

```bash
git clone https://github.com/liukaining/PolyMatrixEngine.git
cd PolyMatrixEngine
```

2. **Configure environment**

```bash
cp .env.example .env
# Edit .env: at least PK, FUNDER_ADDRESS, LIVE_TRADING_ENABLED,
# BASE_ORDER_SIZE, GRID_LEVELS, MAX_EXPOSURE_PER_MARKET. See "Environment variables" below.
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

## Dashboard & Monitoring

Open `http://localhost:8501` for the Streamlit dashboard: Control Panel, Inventory & Risk, Active Orders, Market Screener, Emergency Stop/Liquidate.

## Main API

- Health: `curl http://localhost:8000/health`
- Start market: `curl -X POST http://localhost:8000/markets/{condition_id}/start`
- Stop / Liquidate: `curl -X POST http://localhost:8000/markets/{condition_id}/stop` and `.../liquidate`
- Market risk: `curl http://localhost:8000/markets/{condition_id}/risk`
- Active orders: `curl http://localhost:8000/orders/active`

## Environment variables (.env)

Configuration is loaded from the project root `.env` via `app/core/config.py` (Pydantic Settings). Below is a grouped reference.

### App & mode

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `PROJECT_NAME` | App display name | `PolyMatrix Engine` | Logs and API title only. |
| `DEBUG` | Debug mode | `False` | Set `True` for verbose logs; keep `False` in production. |
| `LIVE_TRADING_ENABLED` | Live trading | `True` / `False` | **Critical:** `True` = real CLOB place/cancel; `False` = dry-run (DB-only simulation). |

### Polymarket network

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `PM_WS_URL` | Market WebSocket URL | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Order book / tick stream. |
| `PM_API_URL` | CLOB REST base URL | `https://clob.polymarket.com` | Orders and book snapshot. |
| `PM_CHAIN_ID` | Chain ID | `137` | Polygon Mainnet. |

### Credentials (do not commit)

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `PK` | Wallet private key (hex) | 64-char hex | Must match `FUNDER_ADDRESS`; used to sign CLOB orders. |
| `FUNDER_ADDRESS` | Trading wallet address | `0x...` (EIP-55) | Used for orders, reconciliation, and positions API. |

### Persistence (Postgres & Redis)

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `DATABASE_URL` | Async Postgres URL | `postgresql+asyncpg://user:pass@host:port/dbname` | Use `postgres:5432` inside Docker. |
| `REDIS_URL` | Redis URL | `redis://localhost:6380/0` | Use `redis://redis:6379/0` inside Docker. |

### Risk

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `MAX_EXPOSURE_PER_MARKET` | Max per-market exposure (USDC) | `10` | Watchdog triggers kill switch (suspend + cancel all) when exceeded. |
| `EXPOSURE_TOLERANCE` | Reconciliation overwrite threshold | `0.01` | Every 5 min, Watchdog compares DB ledger to Polymarket positions API; if Yes or No diff &gt; this, DB is overwritten with API (e.g. 0.01 fixes 5.0 vs 4.3). |
| `ALCHEMY_RPC_URL` | Polygon RPC (e.g. kill switch) | Alchemy/Infura URL | Optional; reserved for future use. |

### Quoting

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `BASE_ORDER_SIZE` | Nominal size per order (USDC) | `5.0` | Enforced as `max(5.0, BASE_ORDER_SIZE)` for Polymarket min. |
| `GRID_LEVELS` | Grid levels per side | `2` | More levels = more orders and churn. |
| `QUOTE_BASE_SPREAD` | Spread around fair value | `0.02` | Bid ≈ fair_value - spread/2, ask ≈ fair_value + spread/2. |
| `QUOTE_PRICE_OFFSET_THRESHOLD` | Price move to refresh grid | `0.01` | Larger = fewer cancel/repost. |
| `QUOTE_BID_ONE_TICK_BELOW_TOUCH` | Allow first bid 1 tick below best bid | `true` / `false` | `true` = more fills, ~1¢ edge; `false` = strict at best bid. |

### Docker host ports (mapping only)

| Variable | Meaning | Example / default | Notes |
|----------|---------|--------------------|-------|
| `DB_PORT` | Host port for Postgres | `5433` | Only for `docker-compose` `ports`; app uses `DATABASE_URL`. |
| `REDIS_PORT` | Host port for Redis | `6380` | Same; avoids conflict with host Redis. |

**Quick start:** Copy `.env.example` to `.env`, set at least `PK`, `FUNDER_ADDRESS`, `LIVE_TRADING_ENABLED`, `BASE_ORDER_SIZE`, `GRID_LEVELS`, `MAX_EXPOSURE_PER_MARKET`. With Docker, ensure `.env` is mounted (see `docker-compose.yml`).

## Disclaimer

This software is provided for educational and experimental purposes. Using it to trade on Polymarket carries significant financial risk. The developers assume no responsibility for any trading losses.
