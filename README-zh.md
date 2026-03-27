# PolyMatrix Engine

> 中文说明 | [English](README.md)

**面向 [Polymarket](https://polymarket.com) 的准机构级自动化做市与流动性引擎。**  
异步架构、内存优先状态、差分报价与经过实战检验的风控——在零手续费、全抵押环境下稳定赚取 Maker 返佣与流动性激励。*Python Web3 做市的天花板。*

**最新加固（v6.3.3+ / v6.4）：** 每次 **Periodic Hard Reset** 先对 CLOB 执行钱包级 **`cancel_all`**（`oms.physical_clob_cancel_all_for_hard_reset()`），再 **`asyncio.sleep`**（默认 3s，`HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC`）等待 USDC 释放，再做 **本地** `cancel_all_orders` 与 **Data API** 对账。对账失败则冻结新 BUY。单市场敞口为 **严格 MTM + pending BUY**；后台对账间隔默认 **60s**。

---

## 为什么选 PolyMatrix Engine？

PolyMatrix Engine 不是玩具脚本，而是为 Polymarket 零手续费、全抵押环境设计的 **类自营（prop）交易内核**。每个设计都围绕三件事：**吞吐、资金安全、时间优先**。

- **热路径零数据库读取。** Tick 逻辑只从内存状态管理器读库存；成交先更新内存再异步落库。这是多数 Python 量化系统在负载下崩掉的主要瓶颈，我们直接拿掉。

- **差分报价，而不是「全撤再挂」。** 只撤掉与目标网格不一致的订单，只补缺失档位。既有挂单保留排队位置，API 调用量大幅下降。

- **带时间保护的对账 + 硬重置后强制真值。** 全量周期对账通过 Polymarket **Data API** 拉持仓，并在本地成交后的缓冲期内避免被延迟数据覆盖。另：**Periodic Hard Reset** 之后，报价引擎 **必定** 调用 `reconcile_single_market(..., force=True)` 再算网格；若 **失败/超时**，本 tick **禁止新 BUY**（模式 **`POST_RESET_RECONCILE_FREEZE`**）。

- **资金保护内置。** 下单前按 **`MAX_EXPOSURE_PER_MARKET`** 与 **`GLOBAL_MAX_BUDGET`** 检查 BUY 名义，口径为 **MTM 持仓 + 挂单 BUY 名义**（严格路径），超预算自动缩档或砍档；Maker 价格限制在盘口内侧，避免意外 Taker。

适合 **基金与团队** 在 Polymarket 上运行可插拔、可审计的做市引擎——无论是赚流动性激励、做结构化做市，还是作为更高层 Alpha 的执行层。

---

## 核心能力一览

| 维度 | 我们做了什么 |
|------|--------------|
| **自动路由 (V6.3)** | 带严格风控的后台组合管理器。扫描 Gamma、按流动性与期限偏好打分，强制执行事件地平线 (event horizon) 避免临近/已过期市场，并做赛道隔离（sector/tag 限额），优先深流动性中短期市场。 |
| **性能** | 内存优先库存；Tick 循环内零 Postgres 读取；`EngineSupervisor` 单例防泄漏任务注册表；有界异步落库队列，关闭时优雅排空。 |
| **执行** | 差分报价（按 side/price/size 签名保留/撤/补）；保留时间优先，减少 CLOB 请求。Fail-Closed 的 Websocket 掉线/假死重连。 |
| **风控** | 全局预算 (`GLOBAL_MAX_BUDGET`)。Kill Switch 以 **capital_used** 为准、熔断器、**Data API** 对账（默认 **60s** 周期 + **硬重置后强制**）、对账失败 **BUY 冻结**、**严格 MTM** 单市场预算、本地成交时间保护、下单前预检。 |
| **Maker 纪律** | 跨盘口保护（SELL ≥ best_bid + tick，BUY ≤ best_ask - tick），杜绝意外 Taker。 |
| **激励** | Gamma 激励参数（最小 size、最大 spread）；在安全熔断下做自适应 size，不超预算。 |
| **运维** | Streamlit 驾驶舱（选市场、启停、敞口、日志、紧急平仓）；FastAPI 控制面；完整 .env 配置。 |

---

## 功能特性

- **V6.3 自动路由 (Portfolio Manager)** — 周期性扫描 Gamma，按「流动性+期限偏好」打分重组组合。强制执行 **事件地平线**（不持有至二元结算/已过期市场）与 **赛道隔离**（sector/tag 限额），避免长期低成交“死市场”占用资金与名额。
- **V6.3 路由打分** — 硬性过滤流动性（`liquidity ≥ 2 万`），并按「距离结算天数」做 time-decay 惩罚，避免超长周期低成交市场；已过期市场视为事件地平线，直接避开。
- **引擎监督者 (Engine Supervisor)** — 严苛的并发安全控制器。保证每个 Market / Token 绝对是单例运行，彻底杜绝多引擎对敲、挂单泄漏与假死幽灵。
- **行情网关（Market Data Gateway）** — 通过 Polymarket Market WebSocket + REST 快照维护本地订单簿；含 30 秒静默假死探测与自愈重连。
- **内存优先库存状态** — `InventoryStateManager` 作为热路径唯一真相源；成交先更新内存并入队异步写库；有界队列 + 关闭时排空，避免 OOM 与丢数。
- **统一定价（AlphaModel）** — 以 YES 盘口（mid + OBI 偏斜）为单一锚点，NO 侧派生；动态 spread + 库存感知状态机（QUOTING / GRACEFUL_EXIT / LIQUIDATING / LOCKED_BY_OPPOSITE）。
- **差分报价** — 按（方向、价格、数量）对比当前挂单与目标网格；只撤过期、只补缺失；保留时间优先、降低 CLOB 流量。
- **全局余额预检** — 下单前检查 `MAX_EXPOSURE_PER_MARKET` 与 `GLOBAL_MAX_BUDGET`，**本地口径与报价循环一致（MTM + pending）**。超预算自动缩档或砍档。
- **跨盘口保护** — SELL 不低于 best_bid + tick，BUY 不高于 best_ask - tick，保证纯 Maker。
- **对账时间保护** — 风控在「最近一次本地成交」后 N 秒内不拿 REST 数据覆盖本地账本（可配置），避免延迟数据覆盖刚发生的成交。
- **激励耕作就绪** — 从 Gamma 读取激励最小 size、最大 spread；在安全范围内自适应 size，否则回退基础 size；驾驶舱展示激励资格。
- **幽灵订单硬重置 + CLOB 全撤（v6.4）** — 约 **每 5 分钟** 先 **CLOB `cancel_all`**（全钱包；兜底 `get_orders`+`cancel_orders`），**超时/异常不崩循环**，再 **固定睡眠** 释放抵押，再 **本地** `force_evict` + **`reconcile_single_market(force=True)`**。对账失败则 **POST_RESET_RECONCILE_FREEZE**。
- **严格 MTM 预算** — 单市场「已占用」= **盯市(YES/NO 持仓×公允价) + pending_yes_buy + pending_no_buy**；达 **`MAX_EXPOSURE_PER_MARKET`** 则新 BUY **size 强制为 0**；网格循环递减剩余名义，防止多档累加越线。
- **更密对账周期** — Watchdog 后台持仓同步间隔 **`RECONCILIATION_INTERVAL_SEC`**（默认 **60** 秒），降低 WS 丢包时本地与链上漂移。
- **OMS + 熔断器** — 通过 `py-clob-client` 下单/撤单；瞬时错误触发熔断；非瞬时（如 400）不触发；「已成交订单无法撤」按成功处理（INFO）。
- **风控守护（Watchdog）** — Kill switch 以 **`yes_capital_used` + `no_capital_used`** 为准（pending 不作为硬停）。**`reconcile_positions()`** 定时全量对账；**`reconcile_single_market()`** 供引擎在硬重置后单市场强制同步。容差 + 时间保护 + 覆盖时按比例修正 **capital_used**。
- **Streamlit 驾驶舱** — Gamma 选市场、启停/强平、库存与 PnL、活动订单、引擎状态、日志尾查。

---

## 架构概览

**外部服务（Polymarket + 基础设施）**

```
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌─────────────────────────┐
  │ Market WS    │ │ User WS      │ │ CLOB REST    │ │ Gamma API    │ │ Data API                │
  │ 订单簿       │ │ 成交/撤单    │ │ 下单         │ │ 市场元数据   │ │ GET /positions?user=…   │
  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └────────────┬────────────┘
         │                │                │                │                       │
         ▼                ▼                ▼                ▼                       ▼
   gateway.py      user_stream.py     oms/core.py    auto_router.py         risk/watchdog.py
   (本地订单簿)    (流水+库存)        (下单/撤单)    (可选组合路由)          reconcile_*()
```

**进程内控制面与数据面**

```
                          ┌─────────────────────────────────────┐
                          │       dashboard (Streamlit)         │
                          │  选市场 | 控制 | 敞口 | 日志         │
                          └──────────────────┬──────────────────┘
                                              │ HTTP
                                              ▼
                          ┌─────────────────────────────────────┐
                          │        app/main.py (FastAPI)         │
                          │  启停/强平 | 状态                    │
                          │  + 可选 Auto-Router 后台任务         │
                          └──────────────────┬──────────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │                                    │                                    │
         ▼                                    ▼                                    ▼
┌─────────────────────┐           ┌─────────────────────────┐           ┌─────────────────────┐
│ market_data/gateway │           │ core/inventory_state    │           │ oms/core            │
│ WS+REST→本地订单簿  │── tick ──▶│ 内存优先库存            │           │ CLOB + 熔断器       │
│ → Redis ob/tick     │           │ 异步落库队列             │           │                     │
└─────────────────────┘           └───────────┬─────────────┘           └──────────┬──────────┘
         │                                    │                                    │
         │ tick:{token}                       │ get_snapshot()（tick 内无 DB）      │ 下单
         │ ob:{token}                         │ 双侧 pending BUY 名义              │
         ▼                                    ▼                                    ▲
┌─────────────────────┐           ┌─────────────────────────┐                     │
│ quoting/engine      │◀─────────▶│ risk/watchdog           │─────────────────────┘
│ • 统一 FV、MTM 预算 │ 对账       │ • ~1s 敞口检查          │   Kill：撤单+suspend
│ • 硬重置→           │ Data API  │ • 周期对账               │
│   reconcile_single  │           │   (RECONCILIATION_…     │
│ • 对账失败则        │           │    INTERVAL_SEC)        │
│   冻结新 BUY        │           │ • 时间保护               │
│ • 差分报价→OMS      │           └─────────────────────────┘
└─────────────────────┘
         │
         │ order_status:{condition}:{token}
         ▼
┌─────────────────────┐
│ market_data/        │
│ user_stream         │ → apply_fill → inventory_state（+ 落库队列）
│                     │ → order_status → 引擎 active_orders
└─────────────────────┘
```

**数据流简述：** Gateway 与 user_stream 消费 **Market / User** WebSocket；Gateway 向 **Redis** 发布 **tick** 与订单簿 KV。每个 **QuotingEngine** 订阅 **tick、control、order_status**；每轮 tick **只读 `inventory_state`**（热路径无 Postgres），计算公允价与网格，执行 **严格 MTM + pending** 预算与跨盘口保护，经 **OMS 差分报价**。成交 **先内存** 再入队异步落库。**Watchdog** 约 **每秒** 检查 **capital_used**，并按 **`RECONCILIATION_INTERVAL_SEC`**（默认 60s）跑 **Data API** 对账；**硬重置后** 引擎 **强制** `reconcile_single_market(..., force=True)`，**失败则本 tick 不挂新 BUY**。常规对账仍在本地成交后 **`RECONCILIATION_BUFFER_SECONDS`** 内 **跳过覆盖**，避免延迟 API 抹掉刚成交。

---

## 与 Polymarket 做市商文档的对应关系

PolyMatrix Engine 实现的是 **Polymarket 官方做市商（MM）流程**，逻辑与数据源与下述文档一致。

| 文档 | 内容概要 | 我们的对应实现 |
|------|----------|----------------|
| [概述](https://docs.polymarket.com/cn/market-makers/overview) | 做市商 = 持续挂限价单、提供流动性；用 WebSocket + Gamma + CLOB | 使用 **Gamma API** 取元数据、**CLOB REST**（py-clob-client）下单；tick 循环内无 DB；跨盘口保护保证纯 Maker。 |
| [入门指南](https://docs.polymarket.com/cn/market-makers/getting-started) | 充值 USDC.e、部署钱包、代币授权、从钱包派生 API 凭证 | 使用 **ClobClient** 的 `create_or_derive_api_creds()` 与 POLY_PROXY（免 gas）。充值与授权在应用外完成。 |
| [流动性奖励](https://docs.polymarket.com/cn/market-makers/liquidity-rewards) | 订单需在 **最大点差内** 且 **≥ 最小规模** 才计入每日奖励；参数来自 Markets API | 从 Gamma 读取 **rewardsMinSize** / **rewardsMaxSpread**（及 **rewardsDailyRate**）；引擎保证 size ≥ min、spread ≤ max（留余量）。 |
| [Maker 返利](https://docs.polymarket.com/cn/market-makers/maker-rebates) | 在收费市场（如加密货币）中，taker 手续费的一部分按日以 USDC 返给被成交的 maker | 我们只挂限价单，天然是 maker；返利由 Polymarket 发放，我们不计算。 |

### 流动性奖励：字段映射

官方文档使用 **min_incentive_size**、**max_incentive_spread**（来自 Markets API）。我们从 Gamma 取同一套数据：

| 官方 / Markets API | Gamma / 本仓库 | 说明 |
|--------------------|----------------|------|
| min_incentive_size（份额） | `rewardsMinSize` → `rewards_min_size` | 引擎使用 `max(base_size, rewards_min_size)`；`AUTO_TUNE_FOR_REWARDS=True` 时目标为 `rewards_min_size * 1.05`，并有安全上限。 |
| max_incentive_spread（美分） | `rewardsMaxSpread` → `rewards_max_spread`（价格） | 美分除以 100。引擎保持 `target_spread ≤ rewards_max_spread * 0.95` 并在下单前校验。 |
| 每日奖励率 | `rewardsDailyRate` 或 `clobRewards[0].rewardsDailyRate` → `reward_rate_per_day` | 驾驶舱「奖励/天」列；用于展示与调参。 |

我们 **不实现** 官方那套完整奖励公式（订单位置评分、采样、时期归一化），只通过 **满足各市场的最小规模与最大点差** 来获得奖励资格；计分与发放由 Polymarket 完成。

---

## 代码结构说明

| 路径 | 描述 |
|------|------|
| `app/core/inventory_state.py` | **内存库存状态。** 热路径唯一真相源；有界异步落库队列；关闭时优雅排空。 |
| `app/market_data/gateway.py` | Market WS + REST 维护本地订单簿；向 Redis `tick:{token}` / `ob:{token}` 发布快照。 |
| `app/market_data/user_stream.py` | User WS：成交/撤单事件；更新 OrderJournal 与 **inventory_state**（内存 + 入队落库）；发布 order_status 供引擎清理活动订单。 |
| `app/quoting/engine.py` | QuotingEngine：tick + control + order_status；**仅内存**库存；统一 FV；**严格 MTM + pending** BUY 预算；**硬重置 → `reconcile_single_market(force=True)`**；对账失败 **BUY 冻结**；**差分报价**；余额预检；跨盘口保护；激励 size。 |
| `app/oms/core.py` | OMS：py-clob-client、熔断器、下单/撤单；**`physical_clob_cancel_all_for_hard_reset()`**（v6.4 全钱包 cancel_all + 睡眠 + 余额日志）。 |
| `app/risk/watchdog.py` | **~1s** 敞口 vs **capital_used**；**`reconcile_positions()`** / **`reconcile_single_market()`** 对 **Data API**；**`RECONCILIATION_INTERVAL_SEC`** 周期；**时间保护**；Kill → 撤单 + suspend。 |
| `app/core/auto_router.py` | 可选 **V6.3** 组合管理：Gamma 扫描、打分、赛道/事件地平线限额、启停市场。 |
| `app/models/` | OrderJournal、InventoryLedger、MarketMeta。 |
| `app/core/` | 配置、Redis、DB 会话。 |
| `dashboard/` | Streamlit：Gamma 选市场、启停/强平、库存与风控、活动订单、引擎状态、日志。 |

---

## 驾驶舱与选市场（Screener）

Streamlit 驾驶舱（端口 **8501**）提供：

- **选市场** — 从 Gamma 拉取活跃市场（`active=true`，`closed=false`）。V3.0 赏金：仅二元、硬性过滤（rewards_min_size 1–250、liquidity ≥ 1k）、体育/博彩黑名单，无模式选择器。
- **打分** — V3.0 Farming Score 0–100：资金回报率(50)、安全偏斜(30)、盘口静默(20)。1–5 星；表格展示星级、分数、奖励/天、门槛、点差(¢)、**竞争度**。
- **池子规模** — 文案「候选池 **X** 个（已加载 **Y** 个）」：X = 通过筛选的市场数，Y = 从 Gamma 拉取的总数。环境变量 `GAMMA_MAX_MARKETS`（默认 5 万）、`GAMMA_PAGE_LIMIT`（默认 2000）控制拉取规模。
- **筛选** — 可选：仅 4 星以上、仅带奖励市场、仅竞争度低（&lt; 60%）。
- **控制** — 按市场启停/强平；库存与 PnL；活动订单；引擎状态；日志尾查。

---

## 环境依赖

- Docker 与 Docker Compose
- PostgreSQL、Redis（由 docker-compose 提供）
- Polymarket 账户与 USDC
- CLOB API 私钥（`PK`）与 `FUNDER_ADDRESS`

## 安装与运行

1. **克隆**

```bash
git clone https://github.com/liukaining/PolyMatrixEngine.git
cd PolyMatrixEngine
```

2. **配置**

```bash
cp .env.example .env
# 编辑 .env：PK、FUNDER_ADDRESS、LIVE_TRADING_ENABLED、
# BASE_ORDER_SIZE（每单份额，非 USDC）、GRID_LEVELS、MAX_EXPOSURE_PER_MARKET 等。
```

3. **启动**

```bash
docker compose up --build -d
```

- API: `http://localhost:8000`
- 驾驶舱: `http://localhost:8501`

4. **日志**

```bash
docker compose logs -f api
```

## 主要 API

- 健康检查: `GET /health`
- 启动: `POST /markets/{condition_id}/start`
- 停止: `POST /markets/{condition_id}/stop`
- 强平: `POST /markets/{condition_id}/liquidate`
- 风险: `GET /markets/{condition_id}/risk`
- 活动订单: `GET /orders/active`
- 全市场状态: `GET /markets/status`

## 环境变量（.env）

由项目根目录 `.env` 经 `app/core/config.py` 加载。常用变量：

| 变量 | 含义 | 默认/说明 |
|------|------|-----------|
| `LIVE_TRADING_ENABLED` | 真实 CLOB 下单 vs 仅模拟 | `False` = 仅模拟 |
| `MAX_EXPOSURE_PER_MARKET` | 单市场敞口上限（USDC）；触发 Watchdog 强停 | 如 `15` |
| `EXPOSURE_TOLERANCE` | 账本与 API 差异超过此值才覆盖 | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | 最近一次本地成交后多少秒内不覆盖 | `8.0` |
| `RECONCILIATION_INTERVAL_SEC` | Watchdog 周期 **Data API** 持仓对账间隔（秒） | `60` |
| `HARD_RESET_CLOB_CANCEL_ALL_ENABLED` | 硬重置时是否先调 CLOB **cancel_all** | `True` |
| `HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC` | cancel_all 后睡眠秒数（等 USDC 解锁） | `3.0` |
| `HARD_RESET_CLOB_CANCEL_ALL_TIMEOUT_SEC` | **cancel_all** 线程调用超时 | `45.0` |
| `HARD_RESET_CLOB_BALANCE_FETCH_TIMEOUT_SEC` | **get_balance_allowance** 超时 | `20.0` |
| `HARD_RESET_CLOB_WALLET_DEDUP_SEC` | 双引擎去重：N 秒内不重复全钱包 cancel_all | `15.0` |
| `BASE_ORDER_SIZE` | 每笔订单 **份额**（CLOB 的 `size`，非 USDC）；最小 **5** 份 | 如 `10.0` |
| `GRID_LEVELS` | 每侧网格档数 | `2` |
| `QUOTE_BASE_SPREAD` | 相对 fair value 的价差 | `0.02` |
| `AUTO_TUNE_FOR_REWARDS` | 为 true 时，在风控范围内按 Gamma 奖励最小规模与最大点差自适应 size/spread | `True` |
| `GAMMA_MAX_MARKETS` | 驾驶舱选市场从 Gamma 拉取的最大市场数 | `50000` |
| `GAMMA_PAGE_LIMIT` | 驾驶舱拉取 Gamma 列表时每页条数 | `2000` |

完整说明见 `.env.example` 与下方「环境变量说明」表格。

### 应用与运行模式

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `PROJECT_NAME` | 应用显示名 | `PolyMatrix Engine` |
| `DEBUG` | 调试模式 | `False` |
| `LIVE_TRADING_ENABLED` | 是否实盘下单 | `True` / `False` |

### Polymarket 网络

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `PM_WS_URL` | Market WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| `PM_API_URL` | CLOB REST 根地址 | `https://clob.polymarket.com` |
| `PM_CHAIN_ID` | 链 ID | `137` (Polygon) |

### 凭证（勿提交）

| 变量 | 含义 | 说明 |
|------|------|------|
| `PK` | 钱包私钥（Hex） | 与 FUNDER_ADDRESS 对应 |
| `FUNDER_ADDRESS` | 交易钱包地址 | EIP-55 格式 |

### 持久化

| 变量 | 含义 | 说明 |
|------|------|------|
| `DATABASE_URL` | 异步 Postgres | Docker 内可用 `postgres:5432` |
| `REDIS_URL` | Redis | Docker 内可用 `redis://redis:6379/0` |

### 风控

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `AUTO_ROUTER_ENABLED` | 是否开启全自动路由做市 | `False` |
| `AUTO_ROUTER_MAX_MARKETS` | 路由器最大同时运作市场数 | `4` |
| `AUTO_ROUTER_SCAN_INTERVAL_SEC` | 路由器扫描 Gamma 的间隔秒数 | `3600` |
| `AUTO_ROUTER_MIN_HOLD_HOURS` | 定力锁：掉出 Top N 后最少持有小时数（事件地平线驱逐会绕过） | `12.0` |
| `AUTO_ROUTER_MIN_REWARD_POOL` | **V7.0** — 日奖励池（USD）低于此值的 Gamma 市场直接跳过 | `50.0` |
| `POLY_BUILDER_API_KEY` | **V7.1** — 官方 Builder API Key，用于订单归因头 | `""` |
| `POLY_BUILDER_SECRET` | **V7.1** — 官方 Builder Secret，用于 HMAC 归因签名 | `""` |
| `POLY_BUILDER_PASSPHRASE` | **V7.1** — 官方 Builder Passphrase，用于归因头 | `""` |
| `EVENT_HORIZON_HOURS` | 临近结算/已过期市场的避险窗口 | `24.0` |
| `MAX_EXPOSURE_PER_SECTOR` | 单赛道/标签最大允许敞口（USDC） | `300.0` |
| `MAX_SLOTS_PER_SECTOR` | 单赛道/标签最大同时做市名额 | `2` |
| `GLOBAL_MAX_BUDGET` | 跨全市场绝对资金红线 (USDC) | `1000.0` |
| `MAX_EXPOSURE_PER_MARKET` | 单市场最大敞口（USDC） | `50.0` |
| `EXPOSURE_TOLERANCE` | 对账覆盖阈值 | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | 本地成交后跳过覆盖的秒数 | `8.0` |
| `RECONCILIATION_INTERVAL_SEC` | Watchdog 周期 Data API 对账间隔（秒） | `60` |
| `HARD_RESET_CLOB_CANCEL_ALL_ENABLED` | 硬重置时是否先 CLOB **cancel_all** | `True` |
| `HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC` | cancel_all 后睡眠（秒） | `3.0` |
| `HARD_RESET_CLOB_CANCEL_ALL_TIMEOUT_SEC` | cancel_all 调用超时（秒） | `45.0` |
| `HARD_RESET_CLOB_BALANCE_FETCH_TIMEOUT_SEC` | 余额查询超时（秒） | `20.0` |
| `HARD_RESET_CLOB_WALLET_DEDUP_SEC` | 双引擎去重间隔（秒） | `15.0` |

### 做市与报价

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `BASE_ORDER_SIZE` | 每笔订单 **份额**（非 USDC；BUY 名义≈价×份额） | `10.0` |
| `GRID_LEVELS` | 每侧网格层数 | `2` |
| `QUOTE_BASE_SPREAD` | 报价边距 | `0.02` |
| `QUOTE_PRICE_OFFSET_THRESHOLD` | 触发网格刷新的价格移动 | `0.01` |
| `QUOTE_BID_ONE_TICK_BELOW_TOUCH` | 首档买价是否允许 best_bid 下一档 | `true` / `false` |

### 驾驶舱选市场（可选）

| 变量 | 含义 | 默认/说明 |
|------|------|-----------|
| `GAMMA_MAX_MARKETS` | 从 Gamma 拉取的最大市场数 | `50000` |
| `GAMMA_PAGE_LIMIT` | 每页条数 | `2000` |

---

## 参考链接

- [Polymarket 做市商 — 概述](https://docs.polymarket.com/cn/market-makers/overview)
- [Polymarket 做市商 — 入门指南](https://docs.polymarket.com/cn/market-makers/getting-started)
- [Polymarket — 流动性奖励](https://docs.polymarket.com/cn/market-makers/liquidity-rewards)
- [Polymarket — Maker 返利](https://docs.polymarket.com/cn/market-makers/maker-rebates)
- [Polymarket 奖励页（产品）](https://polymarket.com/zh/rewards)

---

## Disclaimer

本软件仅供教育与实验用途。在 Polymarket 上交易存在重大财务风险。作者不对任何交易损失负责。

## 技术白皮书

系统设计、路由打分/风控口径与已修复的故障模式详见 `docs/TECHNICAL_WHITEPAPER_V6_3.md`。
