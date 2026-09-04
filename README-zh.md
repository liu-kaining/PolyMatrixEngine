# PolyMatrix Engine

> 中文说明 | [English](README.md)

**面向 [Polymarket](https://polymarket.com) 的实验性自动化做市与流动性引擎。**

> **部署状态：默认锁定。** 实盘执行代码路径已经完成离线工程整改，但仓库本身不能替代有资金部署所需的外部证据。历史账本重建、哈希固定的样本外 Alpha 证据、钱包资金/授权及部署网络检查仍须由操作者完成。本次审查没有连接交易所，也没有发送任何实盘订单。

## 安全整改状态

- `TRADING_MODE=disabled` 是默认值，并且不会启动行情、用户流、Watchdog 或 Router。
- `paper` 使用明确标记的保守事件驱动撮合，绝不发送交易所命令；`live` 才可能申请真实执行。
- 旧 `LIVE_TRADING_ENABLED=True` **单独无法解锁实盘**。live 还要求钱包白名单、未来 24 小时内到期的 arm、显式预算 ceiling、构建 commit、与当前源码/参数完全匹配的 alpha 证据及全部运行时 readiness。奖励排名 Auto-Router 被硬性限制为 paper-only。
- `open_orders_reconciled` 只有在认证 `get_orders` 与逐单终态查询均和本地成交/reservation 一致后才会通过；仅仅“不在开放订单列表”绝不会释放风险。
- CLOB 边界固定为 `polymarket-client==0.6.0`；SDK union、状态前缀、异步分页和 human/e6 数量单位在业务层之前统一归一化，认证 trade history 可幂等回填 User WS 漏单。
- Postgres 钱包租约阻止双实例同时执行，并在每次提交前立即续约；sticky halt 跨重启保留，只能在 disabled 且本地平仓/无未决单后显式确认。数据流不确定或地域判断不确定都会停止新增风险。
- wallet-wide Periodic Hard Reset 及其 `cancel_all` helper 已从代码删除；`0.01` 全量强平实现和 Dashboard 按钮也已删除，`/liquidate` 固定返回 HTTP 410。
- BUY 在提交前先由 Postgres 原子预占资金；下单或撤单结果不确定时保留 reservation，并阻止新增风险直至权威对账。
- 行情快照携带接收时间、交易所时间、snapshot hash/可选 sequence 和有效性；当前无 sequence 的官方流使用严格 timestamp、hash 与周期 REST 重同步。每个市场的 tick/min-size 动态约束会被强制执行。
- 自动激进退出受可见买盘深度、最大价格冲击和基于成本的已实现亏损下限约束。
- 每个已处理 fill 都有一条不可变的带符号现金事实及对应库存版本；明确手续费进入成本/损益，缺失手续费保持 `UNKNOWN`，净 PnL 失效并阻止新增实盘风险，绝不按 0 猜测。
- 任何 REST 仓位覆盖都会把会计版本降级为 `unverified_external`；只有最新确定性重放为 `SAFE` 且账本为验证过的 `v2` 时，API/Dashboard 才展示净已实现 PnL，否则显示 `N/A`。
- `OFFLINE_VALIDATED_ALPHA_ENABLED=True` 不再足以开启新增风险；还必须由内置离线评估器生成并 SHA-256 固定 `alpha-evidence-v2` 报告。策略 ID、关键参数、关键交易源码包和构建 commit 必须与运行时完全匹配。
- 完整的本地数据契约和报告命令见[离线 Alpha 评估手册](spec/OFFLINE_ALPHA_EVALUATION.md)。
- 分阶段部署检查、arm 顺序、放行决策与事故规则见[有资金部署手册](spec/LIVE_DEPLOYMENT_RUNBOOK.md)。
- start/stop/halt/wipe 全部要求至少 32 字符的 `ADMIN_API_TOKEN` Bearer Token。
- `/admin/wipe` 默认关闭，只允许在 `disabled`、无活动引擎、无未决订单、无非零仓位且带精确确认请求头时使用。
- Docker 构建通过 `.dockerignore` 排除 `.env`、私钥、虚拟环境、Git 和日志；Dashboard 不再挂载包含钱包私钥的 `.env`。

生成与钱包、过期时间和预算绑定的 arm token：

```bash
python -m app.core.trading_safety \
  --funder 0xYOUR_FUNDER \
  --expires-at 2026-07-18T12:00:00+00:00 \
  --budget 50
```

---

## 为什么选 PolyMatrix Engine？

PolyMatrix Engine 是一个**正在主动进行安全整改的实验性交易内核**。当前工程目标是确定性会计、有界风险和可审计执行；仓库本身不能证明策略能够盈利。

- **报价 Tick 热路径零数据库读取。** Tick 只读取带版本的内存快照；成交走更安全的路径：inbox、reservation、库存、现金事实和订单状态在 Postgres 原子提交，再把已提交版本同步到内存。

- **差分报价，而不是「全撤再挂」。** 只撤掉与目标网格不一致的订单，只补缺失档位。既有挂单保留排队位置，API 调用量大幅下降。

- **Fail-closed 对账。** live 模式下，已知与活跃 token 使用认证 CLOB 条件代币余额作为数量事实；公共 **Data API** 仅发现未知仓位并提供可核对的成本元数据。任何仓位数量覆盖都会使精确成本、手续费和 PnL 失效并触发 sticky Halt，绝不会被描述成“账务修复成功”；遗留 Periodic Hard Reset 已删除。

- **资金保护内置。** 下单前按 **`MAX_EXPOSURE_PER_MARKET`** 与 **`GLOBAL_MAX_BUDGET`** 检查 BUY 名义，口径为 **MTM 持仓 + 挂单 BUY 名义**（严格路径），超预算自动缩档或砍档；Maker 价格限制在盘口内侧，避免意外 Taker。

在全部状态/readiness 门禁通过，并由离线回放、费用归因和 markout 分析证明盈利能力之前，不得为有资金部署解除锁定。工程检查通过不等于策略能够盈利。

---

## 核心能力一览

| 维度 | 我们做了什么 |
|------|--------------|
| **Auto-Router 研究工具** | paper-only 奖励排名实验，带事件地平线与赛道限额；不是已验证 alpha，live 模式无法启动。 |
| **性能** | 带版本的内存快照；报价 Tick 内零 Postgres 读取；durable fill 事务；`EngineSupervisor` 单例任务注册表。 |
| **执行** | 差分报价（按 side/price/size 签名保留/撤/补）；保留时间优先，减少 CLOB 请求。SDK 原生流在断线、重连缺口或有界队列丢弃时 fail closed。 |
| **风控** | 原子 BUY reservation + 冷仓位成本口径，共用单市场/全局限额；下单/撤单未知状态保留资本并阻止新增风险。 |
| **Maker 纪律** | 常规报价显式 post-only 并按动态 tick 对齐；只有有界库存退出可单独允许 Taker。 |
| **奖励观测** | Gamma 奖励参数只用于展示/离线归因，不得改变 size、spread、订单保活或 live 准入。 |
| **运维** | Streamlit 驾驶舱（选市场、敞口、日志、认证紧急停止）；FastAPI 控制面；fail-closed 默认配置。 |

---

## 功能特性

- **Paper-only Auto-Router** — 扫描奖励市场并执行事件地平线/赛道限制，仅用于研究；中央安全闸在 live 模式永久拒绝该奖励排名策略。
- **引擎监督者 (Engine Supervisor)** — 每个 Market / Token 单例运行；stop/shutdown 先尝试确认撤单再销毁任务，未确认撤单会 sticky Halt。
- **行情网关（Market Data Gateway）** — 全量快照 + 原子 delta，检查 sequence gap、交易所时间、接收新鲜度、非法数值、空盘和交叉盘；无效行情会撤已知订单而不是继续定价。
- **带版本库存状态** — `InventoryStateManager` 为热路径提供已提交快照；user-stream fill 先原子提交 Postgres，再把精确 `state_version` 同步到内存，旧快照不能覆盖新成交。
- **确定性会计重放** — 每个 fill 一条不可变现金事实、连续库存版本、明确手续费、平均成本净已实现 PnL，以及启动/管理端重放；旧账、外部覆盖、缺费或无法重放的账本一律显示 `N/A` 并阻止 live。
- **统一定价（AlphaModel）** — 以 YES 盘口（mid + OBI 偏斜）为单一锚点，NO 侧派生；这是实验模型，不构成盈利证明。
- **差分报价** — 按（方向、价格、数量）对比当前挂单与目标网格；只撤过期、只补缺失；保留时间优先、降低 CLOB 流量。
- **双向原子 reservation** — 下单落账/提交前，BUY 在 Postgres 预占 USDC 名义，SELL 预占持仓 shares；部分成交只释放已处理部分。
- **净边际闸门** — 默认禁止新增 BUY 风险。只有离线验证后显式开启，且扣除执行成本、逆向选择和最小净边际后仍为正才允许报价；奖励不能让亏损报价过闸。
- **跨盘口保护** — SELL 不低于 best_bid + tick，BUY 不高于 best_ask - tick，保证纯 Maker。
- **对账时间保护** — 风控在「最近一次本地成交」后 N 秒内不拿 REST 数据覆盖本地账本（可配置），避免延迟数据覆盖刚发生的成交。
- **奖励与执行解耦** — Dashboard 可观测奖励元数据，但奖励不会改变 size、spread、旧单保留或报价准入。
- **无 wallet-wide reset 路径** — 原 v6.4 `cancel_all` + 本地 `force_evict` 实现及相关配置项已从运行时代码彻底删除。
- **严格 MTM 预算** — 单市场「已占用」= **盯市(YES/NO 持仓×公允价) + pending_yes_buy + pending_no_buy**；达 **`MAX_EXPOSURE_PER_MARKET`** 则新 BUY **size 强制为 0**；网格循环递减剩余名义，防止多档累加越线。
- **保守对账节奏** — Watchdog 仓位比较默认 **`RECONCILIATION_INTERVAL_SEC=300`**；差异会使会计失效并 Halt，而不是作为常规修复被接受。
- **OMS + 熔断器** — 稳定 local/exchange ID、durable fill inbox 与幂等会计；下单结果不明或撤单发现已成交但尚未入账时进入 `UNKNOWN`，保留 reservation 并触发安全停机。
- **风控守护（Watchdog）** — 冷/热仓位成本与 durable BUY reservation 共用单市场/全局超限口径；全局超限会 Halt、suspend 并尝试撤单。
- **Streamlit 驾驶舱** — Gamma 选市场、认证启停、库存与 PnL、活动订单、引擎状态和日志；无界强平操作已删除。

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
                          │  启停/Halt/审计 | 状态               │
                          │  + 可选 Auto-Router 后台任务         │
                          └──────────────────┬──────────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │                                    │                                    │
         ▼                                    ▼                                    ▼
┌─────────────────────┐           ┌─────────────────────────┐           ┌─────────────────────┐
│ market_data/gateway │           │ core/inventory_state    │           │ oms/core            │
│ WS+REST→本地订单簿  │── tick ──▶│ 已提交版本化库存快照     │           │ CLOB + 熔断器       │
│ → Redis ob/tick     │           │ fill 先原子落库           │           │ 延迟加载交易 SDK    │
└─────────────────────┘           └───────────┬─────────────┘           └──────────┬──────────┘
         │                                    │                                    │
         │ tick:{token}                       │ get_snapshot()（tick 内无 DB）      │ 下单
         │ ob:{token}                         │ 双侧 pending BUY 名义              │
         ▼                                    ▼                                    ▲
┌─────────────────────┐           ┌─────────────────────────┐                     │
│ quoting/engine      │◀─────────▶│ risk/watchdog           │─────────────────────┘
│ • 统一 FV、MTM 预算 │ 对账       │ • ~1s 敞口检查          │   Kill：撤单+suspend
│ • 报价风险闸门      │ 认证余额  │ • 周期比较               │
│ • 差分报价          │           │   (RECONCILIATION_…     │
│ • 无效行情→         │           │    INTERVAL_SEC)        │
│   撤单/暂停         │           │ • 差异→Halt              │
│ • 差分报价→OMS      │           └─────────────────────────┘
└─────────────────────┘
         │
         │ order_status:{condition}:{token}
         ▼
┌─────────────────────┐
│ market_data/        │
│ user_stream         │ → durable DB fill 事务 → 已提交缓存快照
│                     │ → order_status → 引擎 active_orders
└─────────────────────┘
```

**数据流简述：** Gateway 与 user_stream 消费 **Market / User** WebSocket，并通过 Redis 发布 tick。QuotingEngine 每轮只读已提交的 `inventory_state` 快照，执行风险/经济性闸门后经 OMS 差分报价。成交先把 inbox、reservation 释放、手续费会计、现金事实和订单状态原子提交，再把同一 `state_version` 同步到内存。Watchdog 高频检查本地限额，并按 `RECONCILIATION_INTERVAL_SEC`（默认 **300s**）比较认证条件代币余额；Data API 只用于发现未知仓位与提供数量一致时的成本元数据。远端数量差异会使账本失效并 Halt，而不是用估算 PnL 静默覆盖。

---

## 与 Polymarket 做市商文档的对应关系

PolyMatrix Engine 实现的是 **Polymarket 官方做市商（MM）流程**，逻辑与数据源与下述文档一致。

| 文档 | 内容概要 | 我们的对应实现 |
|------|----------|----------------|
| [概述](https://docs.polymarket.com/cn/market-makers/overview) | 做市商 = 持续挂限价单、提供流动性；用 WebSocket + Gamma + CLOB | 使用 **Gamma API** 取元数据、固定版本的统一 **polymarket-client** V2 adapter 执行；常规报价强制 post-only。 |
| [入门指南](https://docs.polymarket.com/trading/quickstart) | 准备抵押品、配置钱包与代币授权 | 固定版本统一 SDK 从 `PK` 创建/派生认证凭据，并核对其钱包与 `FUNDER_ADDRESS` 一致。充值与授权仍由操作者在应用外完成。 |
| [流动性奖励](https://docs.polymarket.com/cn/market-makers/liquidity-rewards) | 交易所奖励资格元数据 | 只读取并展示，用于离线归因；奖励不调参，也不能让亏损报价通过。 |
| [Maker 返利](https://docs.polymarket.com/cn/market-makers/maker-rebates) | 在收费市场（如加密货币）中，taker 手续费的一部分按日以 USDC 返给被成交的 maker | 我们只挂限价单，天然是 maker；返利由 Polymarket 发放，我们不计算。 |

### 流动性奖励：字段映射

官方文档使用 **min_incentive_size**、**max_incentive_spread**（来自 Markets API）。我们从 Gamma 取同一套数据：

| 官方 / Markets API | Gamma / 本仓库 | 说明 |
|--------------------|----------------|------|
| min_incentive_size（份额） | `rewardsMinSize` → `rewards_min_size` | 只作展示/资格观测，不增加订单规模。 |
| max_incentive_spread（美分） | `rewardsMaxSpread` → `rewards_max_spread`（价格） | 只换算展示，不压窄策略点差。 |
| 每日奖励率 | `rewardsDailyRate` 或 `clobRewards[0].rewardsDailyRate` → `reward_rate_per_day` | 只用于展示与离线归因。 |

系统不优化也不承诺奖励资格；盈利证据在结构上排除 rewards。

---

## 代码结构说明

| 路径 | 描述 |
|------|------|
| `app/core/inventory_state.py` | **带版本的已提交库存快照。** 报价 Tick 无需读 DB，旧版本快照会被拒绝。 |
| `app/market_data/gateway.py` | Market WS + REST 维护本地订单簿；向 Redis `tick:{token}` / `ob:{token}` 发布快照。 |
| `app/market_data/user_stream.py` | User WS adapter：成交进入确定性 durable fill processor；只在提交后发布订单状态。 |
| `app/quoting/engine.py` | QuotingEngine：tick/control/order status；已提交内存快照；行情、风险和净边际闸门；有界退出与差分报价。 |
| `app/oms/core.py` | OMS：统一 V2 adapter、新单熔断、优先撤单、提交前租约续期、稳定 local/exchange ID 与保守未知状态。 |
| `app/risk/watchdog.py` | **~1s** cold/hot 成本 + reservation 检查；认证 token 余额对账并由 Data API 发现未知仓位；差异使会计失效；Kill → halt + 撤单 + suspend。 |
| `app/core/alpha_evaluator.py` / `strategy_fingerprint.py` | 严格本地证据生成器与规范化参数/源码指纹；两者都不访问交易所。 |
| `app/core/auto_router.py` | 可选 paper-only 奖励元数据研究 Router；live 启动无条件拒绝。 |
| `app/models/` | 订单、fills、不可变现金事实、库存、reservations 与对账/审计记录。 |
| `app/core/` | 配置、Redis、DB 会话。 |
| `dashboard/` | Streamlit：Gamma 选市场、认证启停/Halt、已验证库存与 PnL、活动订单、引擎状态、日志。 |

---

## 驾驶舱与选市场（Screener）

Streamlit 驾驶舱（端口 **8501**）提供：

- **研究筛选** — 从 Gamma 拉取活跃二元市场并展示奖励/流动性元数据；排名不是预期收益、安全评级或 alpha。
- **观测评分** — 只按原始奖励/流动性比、价格距离和换手率做相对排序；Dashboard 已禁止从该奖励排名表直接启动策略。
- **池子规模** — 文案「候选池 **X** 个（已加载 **Y** 个）」：X = 通过筛选的市场数，Y = 从 Gamma 拉取的总数。环境变量 `GAMMA_MAX_MARKETS`（默认 5 万）、`GAMMA_PAGE_LIMIT`（默认 2000）控制拉取规模。
- **筛选** — 可选：仅 4 星以上、仅带奖励市场、仅竞争度低（&lt; 60%）。
- **控制** — 认证启停/紧急 Halt；已验证库存与 PnL；活动订单；引擎状态；日志尾查。强平操作已删除。

---

## 环境依赖

- Docker 与 Docker Compose
- PostgreSQL、Redis（由 docker-compose 提供）
- Polymarket 账户与当前 V2 抵押资产 pUSD
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

首次部署保持 `TRADING_MODE=disabled`。这里只启动本地控制面与依赖；在历史会计与 Alpha 证据、钱包/授权、地域和部署网络检查完成前，不得解除实盘锁定。

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
- 启动: `POST /markets/{condition_id}/start`（需要管理员 Bearer Token）
- 停止: `POST /markets/{condition_id}/stop`（需要管理员 Bearer Token）
- 已移除的不安全强平：`POST /markets/{condition_id}/liquidate` 固定返回 HTTP 410。
- 紧急 Halt: `POST /admin/halt`（需要管理员 Bearer Token）
- 风险: `GET /markets/{condition_id}/risk`
- 活动订单: `GET /orders/active`
- 全市场状态: `GET /markets/status`
- 订单事实对账: `POST /admin/reconciliation/orders`（管理员；不向交易所写入；要求引擎已停止）
- 对账审计摘要: `GET /admin/reconciliation/orders/latest`（管理员；不返回原始交易所 payload）
- 本地会计重放: `POST /admin/accounting/audits`（管理员；仅 disabled；不访问交易所）
- 会计审计历史: `GET /admin/accounting/audits/latest`（管理员）

## 环境变量（.env）

由项目根目录 `.env` 经 `app/core/config.py` 加载。常用变量：

| 变量 | 含义 | 默认/说明 |
|------|------|-----------|
| `TRADING_MODE` | `disabled` / `paper` / `live` 总控 | `disabled` |
| `LIVE_TRADING_ENABLED` | live 的第二确认；单独设置无效 | `False` |
| `LIVE_ARM_EXPIRES_AT` / `LIVE_ARM_TOKEN` | 24h 内短时授权 | 空 = 禁止 live |
| `LIVE_ALLOWED_FUNDER_ADDRESSES` | live 钱包白名单 | 空 = 禁止 live |
| `LIVE_BUDGET_CAP_USD` | 显式 live 预算 ceiling | `100.0` |
| `ADMIN_API_TOKEN` | 写接口 Bearer Token，至少 32 字符 | 空 = 写接口关闭 |
| `OFFLINE_VALIDATED_ALPHA_ENABLED` | 申请开启新增 BUY；仍需 hash 固定且通过校验的证据报告 | `False` |
| `ALPHA_VALIDATION_REPORT_PATH` / `ALPHA_VALIDATION_REPORT_SHA256` | 内置评估器生成的 `alpha-evidence-v2` JSON 与精确内容 hash | 空/不匹配 = alpha readiness 失败 |
| `ALPHA_EVIDENCE_MIN_FILLS` / `ALPHA_EVIDENCE_MIN_MARKETS` | 样本外最小成交/市场覆盖 | `1000` / `20` |
| `ALPHA_EVIDENCE_MIN_DATASET_DAYS` / `ALPHA_EVIDENCE_MAX_AGE_DAYS` | 最小评估窗口/报告最大年龄 | `30` / `30` 天 |
| `MIN_EXPECTED_NET_EDGE` | 扣除成本与逆向选择缓冲后的最小净边际 | `0.02` |
| `EXECUTION_COST_BUFFER` / `ADVERSE_SELECTION_BUFFER` | 每 share 的保守成本扣减 | `0.002` / `0.01` |
| `MARKET_DATA_MAX_AGE_SEC` | 行情超过该年龄即撤单/暂停 | `5.0` |
| `MARKET_DATA_REQUIRE_SEQUENCE_LIVE` | 数据契约提供 sequence 时强制连续性 | 当前官方流默认 `False` |
| `MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE` | live 必须有交易所 timestamp | `True` |
| `MARKET_DATA_REQUIRE_SNAPSHOT_ID_LIVE` / `MARKET_DATA_REST_RESYNC_SEC` | 强制 hash 并周期重建盘口 | `True` / `30` |
| `EXECUTION_LEASE_TTL_SEC` / `ORDER_RECONCILIATION_INTERVAL_SEC` | 单钱包执行租约 / 权威订单对账周期 | `15` / `60` |
| `MAX_EXPOSURE_PER_MARKET` | 单市场敞口上限（USDC）；触发 Watchdog 强停 | 如 `15` |
| `EXPOSURE_TOLERANCE` | 账本与 API 差异超过此值才覆盖 | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | 最近一次本地成交后多少秒内不覆盖 | `8.0` |
| `RECONCILIATION_INTERVAL_SEC` | Watchdog 认证 token 余额对账间隔（秒） | `300` |
| `BASE_ORDER_SIZE` | 每笔订单 outcome shares；运行时自动提高到市场当前 `min_order_size` | 如 `10.0` |
| `GRID_LEVELS` | 每侧网格档数 | `2` |
| `QUOTE_BASE_SPREAD` | 相对 fair value 的价差 | `0.02` |
| `APP_CODE_COMMIT` / `ALPHA_STRATEGY_ID` | 构建身份与策略 ID，必须与证据匹配 | 空/不匹配 = 禁止 live |
| `AUTO_TUNE_FOR_REWARDS` | 仅为旧 `.env` 兼容保留；执行层忽略，`True` 会阻止 live | `False` |
| `GAMMA_MAX_MARKETS` | 驾驶舱选市场从 Gamma 拉取的最大市场数 | `50000` |
| `GAMMA_PAGE_LIMIT` | 驾驶舱拉取 Gamma 列表时每页条数 | `2000` |

完整说明见 `.env.example` 与下方「环境变量说明」表格。

### 应用与运行模式

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `PROJECT_NAME` | 应用显示名 | `PolyMatrix Engine` |
| `DEBUG` | 调试模式 | `False` |
| `TRADING_MODE` | 总体运行模式 | `disabled` / `paper` / `live` |
| `LIVE_TRADING_ENABLED` | live 第二确认，不能单独解锁 | `False` |

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
| `AUTO_ROUTER_MAX_MARKETS` | paper 研究 Router 最大同时市场数 | `8` |
| `AUTO_ROUTER_SCAN_INTERVAL_SEC` | 路由器扫描 Gamma 的间隔秒数 | `3600` |
| `AUTO_ROUTER_MIN_HOLD_HOURS` | paper 研究市场掉出 Top N 后最少保留小时数 | `2.0` |
| `AUTO_ROUTER_MIN_REWARD_POOL` | **V7.0** — 日奖励池（USD）低于此值的 Gamma 市场直接跳过 | `50.0` |
| `POLY_BUILDER_CODE` | 统一 SDK 的订单归因 code | `""` |
| `POLY_BUILDER_API_KEY` / `SECRET` / `PASSPHRASE` | 已删除的旧本地签名字段；非空会阻止 live | 必须为空 |
| `EVENT_HORIZON_HOURS` | 临近结算/已过期市场的避险窗口 | `72.0` |
| `MAX_EXPOSURE_PER_SECTOR` | 单赛道/标签最大允许敞口（USDC） | `300.0` |
| `MAX_SLOTS_PER_SECTOR` | 单赛道/标签最大同时做市名额 | `2` |
| `GLOBAL_MAX_BUDGET` | 跨全市场绝对资金红线 (USDC) | `280.0` |
| `MAX_EXPOSURE_PER_MARKET` | 二元市场上限；原子准入/Watchdog 使用成本 + durable BUY reservation | `40.0` |
| `MAX_EXPOSURE_CATEGORICAL` | 遗留分类市场上限；当前 lifecycle 会拒绝非二元市场 | `30.0` |
| `EXPOSURE_TOLERANCE` | 对账覆盖阈值 | `0.01` |
| `RECONCILIATION_BUFFER_SECONDS` | 本地成交后跳过覆盖的秒数 | `8.0` |
| `RECONCILIATION_INTERVAL_SEC` | Watchdog 认证 token 余额对账间隔（秒） | `300` |

### 做市与报价

| 变量 | 含义 | 示例/默认 |
|------|------|-----------|
| `BASE_ORDER_SIZE` | 每笔订单 **份额**（非 USDC；BUY 名义≈价×份额） | `10.0` |
| `GRID_LEVELS` | 每侧网格层数 | `2` |
| `QUOTE_BASE_SPREAD` | 报价边距 | `0.02` |
| `QUOTE_PRICE_OFFSET_THRESHOLD` | 触发网格刷新的价格移动 | `0.01` |
| `QUOTE_BID_ONE_TICK_BELOW_TOUCH` | 首档买价是否允许 best_bid 下一档 | `true` / `false` |
| `EXIT_MAX_BOOK_IMPACT` | 自动激进退出允许消耗的最大可见盘口冲击 | `0.02` |
| `EXIT_MAX_REALIZED_LOSS_FRACTION` | 自动退出相对成本的亏损下限 | `0.10` |

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
