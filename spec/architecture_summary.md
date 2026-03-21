# PolyMatrix Engine 架构总览

## 构建目标
PolyMatrix Engine 是一个面向 Polymarket 的自动做市引擎，它将实时市场数据、用户成交流、做市策略、风控守护与可视化监控统一编排在一个全栈系统内。

## 核心模块
1. **Data Plane（数据面）**
   - `market_data/` 负责消费 Polymarket 的 Market WS 与 User WS，维护本地 Orderbook 快照、同步成交和撤单，并持续更新 `OrderJournal` 与 `InventoryLedger`。
   - `gamma_client` 为 condition_id 提供 token_id 解析，并支持市场筛选功能。
2. **Quoting & Execution Plane（做市与执行）**
   - `quoting/engine.py` 中的 AlphaModel 结合订单簿平衡 + 库存偏移，驱动智能定价与动态 spread，再交由 `oms/core.py` 进行风险感知的下单/撤单循环。
   - OMS 中包含 Dry-Run 模式、Circuit Breaker 熔断和对接 py-clob-client 的真实下单。
3. **Risk Plane（风控）**
   - `risk/watchdog.py` 每秒检查风险敞口（以 `capital_used` 为准），超限时 suspend/cancel；并按 `RECONCILIATION_INTERVAL_SEC`（默认 60s）周期与 Polymarket **Data API** 对账；`quoting/engine.py` 在 **Periodic Hard Reset** 后调用 `reconcile_single_market(..., force=True)`，失败则本 tick 冻结新 BUY。
4. **API Plane（控制面）**
   - `main.py` 提供 REST 接口：`/markets/{id}/start|stop|liquidate` 等控制做市生命周期，和管理员命令如 `/admin/wipe`。
5. **Dashboard（监控与控制）**
   - `dashboard/app.py` 基于 Streamlit 展示库存、订单、日志、Gamma 筛选器，并提供一键 start/stop/liquidate/Wipe 等操作。

## 数据持久层
- PostgreSQL 存储 `MarketMeta`、`OrderJournal`、`InventoryLedger`；Schema 由 Alembic 管理。
- Redis 作为 pub/sub 总线与 KV 缓存：`tick:{token}` 推送行情快照，`control:{condition_id}` 传达 start/stop/liq 等指令。

## 架构图（ASCII）
```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                            PolyMatrix Engine 架构图                                  │
└─────────────────────────────────────────────────────────────────────────────────────┘

  ┌──────────────────────── Polymarket External Services ─────────────────────────┐
  │                                                                               │
  │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐  ┌────────────┐  │
  │  │ Market WS       │  │ User WS         │  │ CLOB REST    │  │ Gamma API  │  │
  │  │ (Orderbook)     │  │ (Fills/Cancels) │  │ (Orders)     │  │ (Markets)  │  │
  │  │ wss://.../market │  │ wss://.../user  │  │ clob.poly... │  │ gamma-api  │  │
  │  └────────┬────────┘  └────────┬────────┘  └──────┬───────┘  └─────┬──────┘  │
  │           │                    │                   │                │          │
  └───────────┼────────────────────┼───────────────────┼────────────────┼──────────┘
              │ WebSocket          │ WebSocket          │ HTTP           │ HTTP
              ▼                    ▼                   ▼                ▼
┌─────────────────────────────── App Layer ───────────────────────────────────────────┐
│                                                                                     │
│  ┌─────────────────────┐    ┌─────────────────────┐         ┌───────────────────┐  │
│  │  MarketDataGateway  │    │  UserStreamGateway   │         │  GammaAPIClient   │  │
│  │  (gateway.py)       │    │  (user_stream.py)    │         │  (gamma_client.py)│  │
│  │                     │    │                      │         │                   │  │
│  │  ┌───────────────┐  │    │  • handle_fill()     │         │  • get_market_    │  │
│  │  │LocalOrderbook │  │    │    → OrderJournal    │         │    tokens_by_     │  │
│  │  │ • seed()      │  │    │    → InventoryLedger │         │    condition_id() │  │
│  │  │ • apply_event │  │    │  • handle_cancel()   │         └───────────────────┘  │
│  │  │ • snapshot()  │  │    │    → OrderJournal    │                                │
│  │  └───────┬───────┘  │    └──────────┬───────────┘                                │
│  │          │          │               │                                            │
│  │   publish snap      │          DB write (FOR UPDATE)                             │
│  └──────────┼──────────┘               │                                            │
│             │                          │                                            │
│             ▼                          ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │                         Redis (Message Bus)                          │           │
│  │                                                                      │           │
│  │   ┌─────────────┐   ┌──────────────────┐   ┌─────────────────────┐  │           │
│  │   │ ob:{token}   │   │ tick:{token}     │   │ control:{cond_id}  │  │           │
│  │   │ (KV Cache)   │   │ (PubSub Channel) │   │ (PubSub Channel)   │  │           │
│  │   └─────────────┘   └────────┬─────────┘   └──────────┬──────────┘  │           │
│  └──────────────────────────────┼────────────────────────┼──────────────┘           │
│                                 │                        │                          │
│                    subscribe    │           subscribe     │                          │
│                                 ▼                        ▼                          │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │                      QuotingEngine (engine.py)                       │           │
│  │                   (每个 token_id 一个实例)                            │           │
│  │                                                                      │           │
│  │   on_tick(data)                          on_control_message(data)    │           │
│  │      │                                       │                      │           │
│  │      ▼                                       ▼                      │           │
│  │   ┌──────────────────────┐            ┌──────────────┐              │           │
│  │   │     AlphaModel       │            │  suspend →   │              │           │
│  │   │                      │            │  cancel_all  │              │           │
│  │   │  mid = (bid+ask)/2   │            │              │              │           │
│  │   │  OBI skew            │            │  resume →    │              │           │
│  │   │  Inventory skew      │            │  flag=false  │              │           │
│  │   │  → fair_value        │            └──────────────┘              │           │
│  │   │  → dynamic_spread    │                                          │           │
│  │   └──────────┬───────────┘                                          │           │
│  │              │                                                      │           │
│  │              ▼                                                      │           │
│  │   ┌──────────────────────────────────────────────┐                  │           │
│  │   │          Strategy State Machine              │                  │           │
│  │   │                                              │                  │           │
│  │   │  exposure < 5 ?                              │                  │           │
│  │   │    ├── YES → Mode A: NEUTRAL_ACCUMULATE      │                  │           │
│  │   │    │         (BUY-only grid, 少而精)          │                  │           │
│  │   │    │                                         │                  │           │
│  │   │    └── NO  → Mode B: LIQUIDATE_LONG          │                  │           │
│  │   │              (SELL-only, aggressive unwind)   │                  │           │
│  │   └──────────────────────┬───────────────────────┘                  │           │
│  │                          │                                          │           │
│  │              ① cancel_all_orders()                                  │           │
│  │              ② place_orders(new_grid)                               │           │
│  └──────────────────────────┼──────────────────────────────────────────┘           │
│                             │                                                      │
│                             ▼                                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │                   OMS - OrderManagementSystem (oms/core.py)          │           │
│  │                                                                      │           │
│  │   create_order()                        cancel_order()              │           │
│  │      │                                     │                        │           │
│  │      ▼                                     ▼                        │           │
│  │   DB: PENDING ──┐                    CLOB cancel() ──┐              │           │
│  │                 │                                    │              │           │
│  │                 ▼                                    ▼              │           │
│  │   ┌──────────────────┐                 DB: CANCELED                 │           │
│  │   │  CircuitBreaker   │                + check size_matched         │           │
│  │   │  (5 failures →   │                  (dust detection)            │           │
│  │   │   OPEN → block)  │                                              │           │
│  │   └────────┬─────────┘                                              │           │
│  │            │                                                        │           │
│  │    ┌───────┴────────┐                                               │           │
│  │    │  LIVE_TRADING?  │                                               │           │
│  │    ├── No  → DRY-RUN │  (simulate OPEN in DB)                       │           │
│  │    └── Yes → py-clob │──→ asyncio.to_thread() ──→ CLOB API POST    │           │
│  │              -client  │                                              │           │
│  │               │       │                                              │           │
│  │               ▼       │                                              │           │
│  │         DB: OPEN      │  (real orderID from CLOB)                   │           │
│  │         or FAILED     │                                              │           │
│  └──────────────────────────────────────────────────────────────────────┘           │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │                   RiskMonitor / Watchdog (risk/watchdog.py)          │           │
│  │                                                                      │           │
│  │   ┌─────────────────────┐      ┌─────────────────────────────┐      │           │
│  │   │ check_exposure()    │      │ reconciliation_loop()       │      │           │
│  │   │ (every 1s)          │      │ (RECONCILIATION_INTERVAL_   │      │           │
│  │   │                     │      │  SEC, default 60s)          │      │           │
│  │   │ IF capital_used >   │      │ Fetch real positions from   │      │           │
│  │   │    MAX_EXPOSURE:    │      │ Polymarket Data API         │      │           │
│  │   │                     │      │ Compare with DB ledger      │      │           │
│  │   │ → publish suspend   │      │ Overwrite if diff > 1 USDC │      │           │
│  │   │ → cancel_market_    │      └─────────────────────────────┘      │           │
│  │   │   orders()          │                                           │           │
│  │   └─────────────────────┘                                           │           │
│  └──────────────────────────────────────────────────────────────────────┘           │
│                                                                                     │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │                   FastAPI (main.py) — REST Endpoints                 │           │
│  │                                                                      │           │
│  │  POST /markets/{id}/start     → subscribe WS + start engines        │           │
│  │  POST /markets/{id}/stop      → suspend + cancel all                │           │
│  │  POST /markets/{id}/liquidate → suspend + cancel + dump @ 0.01      │           │
│  │  GET  /markets/{id}/risk      → read InventoryLedger                │           │
│  │  GET  /orders/active          → read OrderJournal (OPEN/PENDING)    │           │
│  │  POST /admin/wipe             → truncate DB + flush Redis           │           │
│  └──────────────────────────────┬───────────────────────────────────────┘           │
│                                 │ HTTP :8000                                        │
└─────────────────────────────────┼───────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                     Dashboard (Streamlit) — :8501                                    │
│                                                                                     │
│  ┌────────────┐ ┌──────────────┐ ┌──────────────┐ ┌─────────┐ ┌────────────────┐  │
│  │ Control    │ │ Inventory &  │ │ Market       │ │ Active  │ │ System Logs    │  │
│  │ Panel      │ │ Risk Panel   │ │ Screener     │ │ Orders  │ │ (Tail+Search)  │  │
│  │            │ │              │ │ (Gamma API)  │ │         │ │                │  │
│  │ • Start    │ │ • Metrics    │ │ • 4-mode     │ │ • Table │ │ • 500 lines    │  │
│  │ • Stop     │ │ • Bar Chart  │ │   filter     │ │ • TZ=   │ │ • Level filter │  │
│  │ • Liquidate│ │ • Ledger     │ │ • Score/Star │ │   +0800 │ │ • Keyword      │  │
│  │ • Wipe     │ │ • Links      │ │ • 1-click    │ │         │ │   search       │  │
│  └────────────┘ └──────────────┘ │   start      │ └─────────┘ └────────────────┘  │
│                                  └──────────────┘                                   │
│                                  i18n: EN / ZH                                      │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────── Persistence ─────────────────────────────────────────┐
│                                                                                     │
│  ┌──────────────────────────────┐      ┌──────────────────────────────┐             │
│  │  PostgreSQL :5432             │      │  Redis :6379                  │             │
│  │                              │      │                              │             │
│  │  ┌────────────────────────┐  │      │  KV:  ob:{token_id}  (snap) │             │
│  │  │ markets_meta           │  │      │  Pub:  tick:{token_id}       │             │
│  │  │  • condition_id (PK)   │  │      │  Pub:  control:{cond_id}    │             │
│  │  │  • yes_token_id        │  │      │                              │             │
│  │  │  • no_token_id         │  │      └──────────────────────────────┘             │
│  │  │  • status              │  │                                                   │
│  │  └────────────┬───────────┘  │                                                   │
│  │               │ 1:N          │                                                   │
│  │  ┌────────────▼───────────┐  │                                                   │
│  │  │ orders_journal         │  │                                                   │
│  │  │  • order_id (PK)       │  │                                                   │
│  │  │  • market_id (FK)      │  │                                                   │
│  │  │  • side / price / size │  │                                                   │
│  │  │  • status (enum)       │  │                                                   │
│  │  │  • payload (JSON)      │  │                                                   │
│  │  └────────────────────────┘  │                                                   │
│  │               │ 1:1          │                                                   │
│  │  ┌────────────▼───────────┐  │                                                   │
│  │  │ inventory_ledger       │  │                                                   │
│  │  │  • market_id (PK/FK)   │  │                                                   │
│  │  │  • yes_exposure        │  │                                                   │
│  │  │  • no_exposure         │  │                                                   │
│  │  │  • realized_pnl        │  │                                                   │
│  │  └────────────────────────┘  │                                                   │
│  └──────────────────────────────┘                                                   │
└─────────────────────────────────────────────────────────────────────────────────────┘

                            ┌─────────────────────┐
                            │  Docker Compose      │
                            │                     │
                            │  api        :8000   │
                            │  dashboard  :8501   │
                            │  postgres   :5433   │
                            │  redis      :6380   │
                            └─────────────────────┘
```

## 数据流概述
1. Market WS → MarketDataGateway → Redis tick:{token} → QuotingEngine
2. QuotingEngine 调用 AlphaModel 计算 fair value & spread → Strategy 状态机判定挂单方向 → OMS 发单到 CLOB
3. User WS → UserStreamGateway → OrderJournal/InventoryLedger 更新 ∞ Watchdog 风控上下文
4. REST API 提供控制与遥测，Dashboard 通过 HTTP/RDS 可视化与执行手动操作
