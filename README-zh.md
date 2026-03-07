# PolyMatrix Engine

> 中文说明 | [English](README.md)

**面向 [Polymarket](https://polymarket.com) 的准机构级自动化做市与流动性引擎。**  
异步架构、内存优先状态、差分报价与经过实战检验的风控——在零手续费、全抵押环境下稳定赚取 Maker 返佣与流动性激励。*Python Web3 做市的天花板。*

---

## 为什么选 PolyMatrix Engine？

PolyMatrix Engine 不是玩具脚本，而是为 Polymarket 零手续费、全抵押环境设计的 **类自营（prop）交易内核**。每个设计都围绕三件事：**吞吐、资金安全、时间优先**。

- **热路径零数据库读取。** Tick 逻辑只从内存状态管理器读库存；成交先更新内存再异步落库。这是多数 Python 量化系统在负载下崩掉的主要瓶颈，我们直接拿掉。

- **差分报价，而不是「全撤再挂」。** 只撤掉与目标网格不一致的订单，只补缺失档位。既有挂单保留排队位置，API 调用量大幅下降。

- **带时间保护的对账。** 在「最近一次本地成交」之后的若干秒内，风控不会用远端 REST 数据覆盖本地账本，避免「刚成交就被延迟 API 数据抹掉」。

- **资金保护内置。** 下单前按敞口预算检查总买量，超预算自动缩档或砍档；Maker 价格被严格限制在盘口内侧，绝不跨盘口造成意外 Taker 成本。

适合 **基金与团队** 在 Polymarket 上运行可插拔、可审计的做市引擎——无论是赚流动性激励、做结构化做市，还是作为更高层 Alpha 的执行层。

---

## 核心能力一览

| 维度 | 我们做了什么 |
|------|--------------|
| **性能** | 内存优先库存；Tick 循环内零 Postgres 读取；有界异步落库队列，关闭时优雅排空。 |
| **执行** | 差分报价（按 side/price/size 签名保留/撤/补）；保留时间优先，减少 CLOB 请求。 |
| **风控** | Kill Switch、熔断器、单市场敞口上限、带时间保护的对账、下单前余额预检。 |
| **Maker 纪律** | 跨盘口保护（SELL ≥ best_bid + tick，BUY ≤ best_ask - tick），杜绝意外 Taker。 |
| **激励** | Gamma 激励参数（最小 size、最大 spread）；在安全熔断下做自适应 size，不超预算。 |
| **运维** | Streamlit 驾驶舱（选市场、启停、敞口、日志、紧急平仓）；FastAPI 控制面；完整 .env 配置。 |

---

## 功能特性

- **行情网关（Market Data Gateway）** — 通过 Polymarket Market WebSocket + REST 快照维护本地订单簿；合并增量、发布 Top-N BBO 到 Redis 供引擎消费。
- **内存优先库存状态** — `InventoryStateManager` 作为热路径唯一真相源；成交先更新内存并入队异步写库；有界队列 + 关闭时排空，避免 OOM 与丢数。
- **统一定价（AlphaModel）** — 以 YES 盘口（mid + OBI 偏斜）为单一锚点，NO 侧派生；动态 spread + 库存感知状态机（QUOTING / LIQUIDATING / LOCKED_BY_OPPOSITE）。
- **差分报价** — 按（方向、价格、数量）对比当前挂单与目标网格；只撤过期、只补缺失；保留时间优先、降低 CLOB 流量。
- **余额预检** — 下单前检查总买量是否超过可用预算；自动缩档或砍档，避免 API 报「余额不足」。
- **跨盘口保护** — SELL 不低于 best_bid + tick，BUY 不高于 best_ask - tick，保证纯 Maker。
- **对账时间保护** — 风控在「最近一次本地成交」后 N 秒内不拿 REST 数据覆盖本地账本（可配置），避免延迟数据覆盖刚发生的成交。
- **激励耕作就绪** — 从 Gamma 读取激励最小 size、最大 spread；在安全范围内自适应 size，否则回退基础 size；驾驶舱展示激励资格。
- **OMS + 熔断器** — 通过 `py-clob-client` 下单/撤单；瞬时错误触发熔断；非瞬时（如 400）不触发；「已成交订单无法撤」按成功处理（INFO）。
- **风控守护（Watchdog）** — 单市场敞口检查；超限即 suspend + 撤光该市场挂单；定期与 Polymarket 持仓 API 对账，带容差与时间保护。
- **Streamlit 驾驶舱** — Gamma 选市场、启停/强平、库存与 PnL、活动订单、引擎状态、日志尾查。

---

## 架构概览

```
                          ┌─────────────────────────────────────┐
                          │       dashboard (Streamlit)          │
                          │  选市场 | 控制 | 敞口 | 日志         │
                          └──────────────────┬───────────────────┘
                                              │ HTTP
                                              ▼
                          ┌─────────────────────────────────────┐
                          │           app/main.py (FastAPI)      │
                          │  /markets/{id}/start|stop|liquidate  │
                          │  /markets/status | /orders/active    │
                          └──────────────────┬───────────────────┘
                                              │
         ┌───────────────────────────────────┼───────────────────────────────────┐
         │                                   │                                   │
         ▼                                   ▼                                   ▼
┌─────────────────────┐           ┌─────────────────────────┐           ┌─────────────────────┐
│ market_data/gateway │           │ core/inventory_state    │           │ oms/core            │
│ WS + REST → 本地   │── tick ──▶│ 内存优先库存            │           │ 下单/撤单 +         │
│ 订单簿 → Redis     │           │ 异步落库队列             │           │ 熔断器              │
└─────────────────────┘           └───────────┬─────────────┘           └──────────┬──────────┘
         │                                    │                                    │
         │ tick:{token}                       │ get_snapshot()                     │ create/cancel
         │                                    │ (循环内无 DB)                       │
         ▼                                    ▼                                    ▲
┌─────────────────────┐           ┌─────────────────────────┐                     │
│ quoting/engine      │           │ risk/watchdog           │                     │
│ • AlphaModel, FV    │           │ • 敞口检查               │─────────────────────┘
│ • 差分报价          │──────────▶│ • 对账 + 时间缓冲        │   cancel_market_orders
│ • 余额预检          │ control   │ • suspend 发布          │
│ • 跨盘口保护        │           └─────────────────────────┘
└─────────────────────┘
         │
         │ order_status (user_stream 的 FILLED/CANCELED)
         ▼
┌─────────────────────┐
│ market_data/        │
│ user_stream         │ → handle_fill → inventory_state.apply_fill → 入队落库
│ (User WS)           │ → handle_cancel → order_status 发布
└─────────────────────┘
```

**数据流简述：** Market WS 与 User WS 驱动 gateway 与 user_stream。Gateway 向 Redis 发布 **tick**。引擎订阅 **tick** 与 **control**，**仅从内存**（InventoryStateManager）读库存，算 fair value 与网格，再通过 OMS **差分报价**（撤过期、补缺失）。成交先更新 **内存库存** 并入队落库。Watchdog 监控敞口并与 Polymarket REST 对账，**在缓冲时间内跳过覆盖**，避免延迟数据覆盖刚发生的本地成交。

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
| max_incentive_spread（美分） | `rewardsMaxSpread` → `rewards_max_spread`（价格） | 美分除以 100。引擎保持 `target_spread ≤ rewards_max_spread * 0.90` 并在下单前校验。 |
| 每日奖励率 | `rewardsDailyRate` 或 `clobRewards[0].rewardsDailyRate` → `reward_rate_per_day` | 驾驶舱「奖励/天」列；用于展示与调参。 |

我们 **不实现** 官方那套完整奖励公式（订单位置评分、采样、时期归一化），只通过 **满足各市场的最小规模与最大点差** 来获得奖励资格；计分与发放由 Polymarket 完成。

---

## 代码结构说明

| 路径 | 描述 |
|------|------|
| `app/core/inventory_state.py` | **内存库存状态。** 热路径唯一真相源；有界异步落库队列；关闭时优雅排空。 |
| `app/market_data/gateway.py` | Market WS + REST 维护本地订单簿；向 Redis `tick:{token}` / `ob:{token}` 发布快照。 |
| `app/market_data/user_stream.py` | User WS：成交/撤单事件；更新 OrderJournal 与 **inventory_state**（内存 + 入队落库）；发布 order_status 供引擎清理活动订单。 |
| `app/quoting/engine.py` | QuotingEngine：订阅 tick + control + order_status；**从内存读库存**；AlphaModel + 网格；**差分报价**（sync_orders_diff）；余额预检；跨盘口保护；激励感知 size。 |
| `app/oms/core.py` | OMS：py-clob-client、熔断器、下单/撤单；「已成交无法撤」按成功处理。 |
| `app/risk/watchdog.py` | 敞口检查；与 Polymarket 持仓 API 对账；**时间保护**（本地成交后短时间内不覆盖）。 |
| `app/models/` | OrderJournal、InventoryLedger、MarketMeta。 |
| `app/core/` | 配置、Redis、DB 会话。 |
| `dashboard/` | Streamlit：Gamma 选市场、启停/强平、库存与风控、活动订单、引擎状态、日志。 |

---

## 驾驶舱与选市场（Screener）

Streamlit 驾驶舱（端口 **8501**）提供：

- **选市场** — 从 Gamma 拉取活跃市场（`active=true`，`closed=false`）。过滤：仅二元、按模式（Conservative / Normal / Aggressive / Ultra）的流动性/成交量/价格。硬性过滤：流动性与 24h 量 ≥ 5k、YES 价格在 [0.20, 0.80]、排除危险词。
- **打分** — 每个市场 0–100 分（换手率 40%、价格居中 30%、绝对流动性 30%、点差惩罚最多 -20）。1–5 星对应分数段；表格展示星级、分数、奖励/天、门槛、点差(¢)、**竞争度**（Gamma `competitive`，越低越容易拿奖励）。
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
# BASE_ORDER_SIZE、GRID_LEVELS、MAX_EXPOSURE_PER_MARKET 等。
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
| `BASE_ORDER_SIZE` | 每笔订单 size（最小 5） | 如 `5.0` |
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
| `MAX_EXPOSURE_PER_MARKET` | 单市场最大敞口（USDC） | `10` |
| `EXPOSURE_TOLERANCE` | 对账覆盖阈值 | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | 本地成交后跳过覆盖的秒数 | `8.0` |

### 做市与报价

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `BASE_ORDER_SIZE` | 每笔订单 size（USDC） | `5.0` |
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
