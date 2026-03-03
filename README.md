# PolyMatrix Engine

PolyMatrix Engine 是一个针对 Polymarket 的自动化高频做市与统计套利引擎，采用异步架构与严格的订单状态管理，目标是在零手续费环境下，通过提供网格化流动性与风险严格受控的方式，长期、稳定地赚取 Maker 收益与流动性补贴。

## 功能特性

- **Market Data Gateway（订单簿网关）**

## 架构概览

```
                                     ┌─────────────────────────────┐
                                     │     dashboard/app.py        │
                                     │ (Streamlit 控制 + Gamma 筛选)│
                                     └──────────┬──────────────────┘
                                                │
                                   HTTP/REST   │
                                                ▼
                             ┌────────────────────────────────┐
                             │          app/main.py            │
                             │ FastAPI 控制面：start/stop/liq │
                             │ /admin/wipe /markets/status     │
                             └──────────┬─────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
        ▼                               ▼                               ▼
┌─────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────┐
│ app/market_data/    │      │ app/quoting/engine.py     │      │ app/oms/core.py       │
│ gateway.py (WS)     │──────│ QuotingEngine + AlphaModel │──────│ OrderManagementSystem │
│ LocalOrderbook      │ tick │ ScoreEngine（计划中）      │ diff │ CircuitBreaker, OMS   │
└─────────────────────┘      └──────────┬───────────────┘      └──────────┬───────────┘
                                        │                                 │
                                        │ Redis pub/sub (tick/control)   │
                                        ▼                                 ▼
                             ┌──────────────────────────┐      ┌──────────────────────┐
                             │ app/core/redis.py        │      │ app/models/db_models.py│
                             │ RedisManager + state API │      │ OrderJournal, Inventory│
                             └──────────────────────────┘      │ Ledger, MarketMeta     │
                                                                 └──────────────────────┘
                                                                            ▲
                                                                            │
                                                    ┌───────────────────────┴────────┐
                                                    │ app/risk/watchdog.py             │
                                                    │ 1s 敞口监控 + control:{cid} pub  │
                                                    │ 5min 对账 Polymarket 数据 API     │
                                                    └──────────────────────────────────┘
```

- **Market Data Gateway（订单簿网关）**

## 代码结构说明

| 路径 | 描述 |
| ---- | ---- |
| `app/market_data/` | 通过 Market WS + REST 组合维护 `LocalOrderbook`，向 Redis `tick:{token}`/`ob:{token}` 发布完整快照。 |
| `app/quoting/` | QuotingEngine + AlphaModel/ScoreEngine 策略运行时，监听 Redis tick/control 事件，计算 fair value、spread 和网格，然后调用 `oms` 执行。 |
| `app/oms/` | Order Management System，封装 `py-clob-client`、CircuitBreaker、DB 状态机，负责下单、撤单和审计。 |
| `app/risk/` | Watchdog 定时发现敞口超限、触发 `control:{condition_id}`、以及对接 Polymarket 的持仓对账 API。 |
| `app/models/` | ORM 定义：`OrderJournal`、`InventoryLedger`、`MarketMeta`，后端全靠这三张表审计每笔挂单与敞口。 |
| `app/core/` | 通用基础设施（配置、Redis 管理、数据库会话、辅助工具）。 |
| `dashboard/` | Streamlit 监控面板、Gamma 筛选、日志浏览、紧急控制；通过 REST/日志/Redis 观察引擎状态。 |
| `spec/` | 场景与策略文档（如 `architecture_summary.md`、即将新增的 `strategy-enhancements/plan.md`），记录架构、计划与验证。 |

- 当前架构中的 `AlphaModel` 正在拓展为 ScoreEngine + GridDiff + 更丰富 logging，相关计划与图示存放在 `spec/strategy-enhancements/`。
- README 下方的 “主要 API/配置/运行” 章节可以快速引导部署与试运行。

- **继续保留** 之前的“功能特性/Quoting Engine/OMS/Risk Watchdog/Dashboard”等描述，便于快速理解每层责任。
  - 通过官方 Market WebSocket 订阅目标 `token_id` 的盘口增量更新。
  - 使用本地 `LocalOrderbook` 缓存，将增量数据合并成完整订单簿，再生成 **Top-5 BBO 快照**。
  - 对每个 token：
    - 持久化最新快照到 Redis：`ob:{token_id}`
    - 推送 Tick 到 Redis：`tick:{token_id}`，供 QuotingEngine 消费。
  - 在 `/markets/{condition_id}/start` 时，会先通过 CLOB REST API `GET /book?token_id=...` 触发一次**初始全量快照**，避免低波动市场长期“无 Tick”导致引擎不工作。

- **Quoting Engine（动态做市引擎）**
  - 通过 Redis PubSub 订阅：
    - `tick:{token_id}`：盘口快照
    - `control:{condition_id}`：控制信号（`suspend` / `resume`）
  - 使用 `AlphaModel` 基于：
    - BBO 中间价 `mid`
    - 盘口失衡度 OBI：\((bid\_size - ask\_size)/(bid\_size + ask\_size)\)
    - 当前 inventory（Yes/No 敞口）
    计算：
    - **动态 Fair Value（含盘口/库存 skew）**
    - **动态 Spread 宽度**
  - V1.1 实现 **库存感知的不对称甩货算法 (Inventory-Aware Asymmetric Quoting)**：
    - **状态 A（Neutral / 轻仓）**：当当前 token 敞口 \(|exposure| < 5\) 时，引擎只在价格区间的买方铺设 BUY 网格（按 `GRID_LEVELS` 和 `BASE_ORDER_SIZE` 分层），专注“接盘建仓”，默认不挂 SELL 端，避免在小资金下无效占用保证金。
    - **状态 B（Long / 重仓）**：当敞口 \(\ge 5\) 时，引擎自动切换到“甩货模式”，停止所有 BUY，下发单一或少量 **Aggressive SELL**：
      - 定价公式：`Ask = min(FairValue + 0.01, BestAsk - 0.01)` 并裁剪在 `[0.01, 0.99]`。
      - size 至少为 5（Polymarket `orderMinSize`），且不超过当前持仓，尽快在盘口上方“砸盘出货”以回收资金。
    - `BASE_ORDER_SIZE` 在引擎内部会被 `max(5.0, BASE_ORDER_SIZE)` 处理，保证任何真实报单都满足 Polymarket 的最小下单 size 约束。
  - 在每个有效 tick 上：
    - 先从数据库读取 `InventoryLedger` 和 `MarketMeta`，计算当前 exposure（Yes 或 No），并将其传入 `AlphaModel`。
    - Debounce：若新 Fair Value 与上一次锚定价差小于阈值，则跳过重置，减少不必要改价与 cancel/post。
    - 按上述状态机生成订单指令（Neutral 只买、Long 只卖），形成不对称网格。
    - **严格顺序执行：先 cancel 当前全部活动订单，再按新网格下单**，保证不会产生长期幽灵挂单。
  - 支持控制信号：
    - `suspend`：设置内部 `suspended=True`，同步执行 `cancel_all_orders()`，形成真正的 Kill Switch。
    - `resume`：恢复正常响应 tick。

- **OMS（Order Management System，订单管理系统）**
  - 使用 `py-clob-client` 与 Polymarket CLOB 集成。
  - 订单生命周期：
    1. 在 DB 中插入 `OrderJournal`：`PENDING` 状态（本地临时 `local_...` ID）。
    2. 通过 `CircuitBreaker` 调用 `client.create_and_post_order()`（通过 `asyncio.to_thread` 下沉到线程池，不阻塞事件循环）。
    3. 成功则将 `order_id` 更新为真实链上/系统 ID，并标记为 `OPEN`；失败则标记为 `FAILED` 并写入错误 payload。
  - 撤单：
    - 正常模式：调用 `client.cancel(order_id)`，成功后标记 `CANCELED`。
    - 附加：撤单后会同步调用 `client.get_order(order_id)` 检查 `size_matched`，用于发现“先部分成交再撤单”等粉尘情况，并记录到 payload 中，方便对账。
  - **CircuitBreaker 熔断器：**
    - 连续多次 API 级错误（如 400 余额不足 / 最小订单尺寸不满足等）会累积失败计数。
    - 达到阈值后进入 `OPEN` 状态，暂时阻止新的下单/撤单请求，防止在资金不足 / 配置错误时疯狂轰炸 API。
    - 隔一段时间会自动尝试从 `OPEN` → `HALF_OPEN` → `CLOSED`。
  - **Dry-Run / 实盘切换：**
    - 当 `.env` 中 `LIVE_TRADING_ENABLED=False` 或 `ClobClient` 未成功初始化时：
      - 所有下单/撤单都会以 `[DRY-RUN]` 方式在本地 DB 内模拟 `OPEN/CANCELED` 状态，不会真正访问 CLOB。

- **Risk Watchdog（风控守护进程）**
  - 独立协程常驻后台，周期性读取 `InventoryLedger` 等数据：
    - 监控每个 `market_id` 的 Yes / No 敞口。
    - 若敞口绝对值超过 `MAX_EXPOSURE_PER_MARKET`：
      - 通过 Redis 发布 `control:{condition_id}` → `{"action": "suspend"}`。
      - 调用 `oms.cancel_market_orders(condition_id)` 撤光该市场全部 `OPEN/PENDING` 挂单。

- **Dashboard（Streamlit 驾驶舱）**
  - **Control Panel：**
    - 支持直接输入 **Condition ID 或 Polymarket URL**（例如 `https://polymarket.com/event/<slug>`），解析为 `condition_id`。
    - 使用侧边栏表单 + **二次确认** 触发 `/markets/{condition_id}/start`：
      - 第一步：在表单中输入 ID/URL 并勾选确认框：`I understand this may place real orders with current config`。
      - 第二步：表单下方出现黄色确认区域，显示将要启动的 `condition_id`，必须点击 `✅ Confirm Start` 才会真正调用后端 API。
    - 成功后会：
      - 从 Gamma 获取 CLOB tokenIds；
      - 订阅 Market WS + User WS；
      - 启动双 token 的 QuotingEngine；
      - 触发 REST 初始快照 + 第一轮网格挂单。
  - **Emergency Controls：**
    - 输入 `condition_id` 后：
      - `🛑 Stop`：进入二次确认区，用户确认后调用 `/markets/{id}/stop` → 软撤单 + 挂牌暂停。
      - `☢️ Liquidate All`：进入二次确认区，用户确认后调用 `/markets/{id}/liquidate` → 先撤单，再按 0.01 价位砸盘清仓 Yes/No 敞口。
  - **Inventory & Risk：**
    - 顶部以三张 Metric 卡片形式展示：
      - 当前 Active Markets 数量。
      - Total Realized PnL（USDC）。
      - Total Gross Exposure（全市场 Yes/No 绝对敞口之和）。
    - `Market Exposures (USDC)` 图表被折叠在 `st.expander` 中，只有在存在非零敞口时才默认展开，避免单市场零敞口时出现大块空白图。
    - 下方 `Inventory Ledger` 表：
      - 展示 `market_id`、Yes/No 敞口、realized PnL 与 `updated_at`。
      - `gamma_link`: 跳转到 `https://gamma-api.polymarket.com/markets?condition_ids=...`。
      - `polymarket_link`: 跳转到 `https://polymarket.com/event/{slug}`。
  - **Active Orders：**
    - 来自 `orders_journal` 的 `OPEN/PENDING` 订单列表，含价格、方向、按 **Asia/Shanghai（东八区）** 显示的创建时间等。
  - **Market Screener (Gamma)：**
    - 从 Gamma API 一次拉取最多 500 个 `active=true, closed=false` 的市场，并在本地进行严格过滤：
      - 仅保留 **Binary** 市场（outcomes 为 Yes/No）。
      - 删除所有 Sports / Live 盘口（基于 `tags`/`category`/`slug` 中出现 `sports, nfl, nba, soccer, ...` 等关键词，或 question 中包含 `win the match, in-play, live odds, halftime` 等字样）。
      - 对政治、选举、流行文化等优质赛道做语义高亮：根据 `category/tags` 以及 question/slug 文本中是否包含 `president, election, senate, mayor, oscars, movie, series, ...` 等词，将 `Category/Tag` 列标记为 `⭐ Politics` / `⭐ Culture` / `⭐ Premium`。
    - 提供四档筛选模式：
      - **Conservative**：DTE ≥ 7 天、24h 成交量 > 50k、流动性 > 10k、YES 价 0.25–0.75。
      - **Normal**：DTE ≥ 3 天、24h 成交量 > 10k、流动性 > 3k、YES 价 0.20–0.80。
      - **Aggressive**：DTE ≥ 1 天、24h 成交量 > 1k、流动性 > 500、YES 价 0.10–0.90。
      - **Ultra**：仅保留 Binary + 赛道风控（Sports/Live 黑名单），不再做 DTE/体量/赔率过滤，尽量接近“全市场池子”。
    - Screener 表格支持交互选中：
      - 左侧 `Select` 列为可勾选列，当前选中的市场用勾选框标记。
      - 选中行会同步到下方的 **Selected market 卡片** 与启动按钮。
    - 启动方式：
      - 先在表格中勾选目标市场，下面卡片会高亮展示完整 Question、Condition ID、价格与流动性等信息。
      - 点击 `Start from Screener` 后，会出现二次确认区域，用户确认后才会调用 `/markets/{condition_id}/start` 启动策略。
  - **系统日志视图 (System Logs)**：
    - 后端使用 `logging.handlers.RotatingFileHandler` 将所有核心日志写入 `data/logs/trading.log`，单文件 5MB、最多 3 个备份。
    - Dashboard 中提供 `📝 System Logs (Tail & Search)` 折叠面板：
      - 通过 `tail_logs` 从日志文件末尾读取最近 500 行。
      - 支持按日志级别（ALL/INFO/WARNING/ERROR）和关键字（substring）过滤。
      - 使用 `st.code(..., language="log")` 以黑底代码块形式展示，配有 `🔄 Refresh Logs`。

- **日志与时区**
  - FastAPI 进程启动时强制设置 `TZ=Asia/Shanghai` 并 `time.tzset()`，所有 `%(asctime)s` 日志使用北京时间（UTC+8）。

## 环境依赖

- Docker 和 Docker Compose
- PostgreSQL、Redis（由 docker-compose 提供）
- Polymarket 账户与资金（USDC.e）
- Polymarket CLOB API 的私钥 (`PK`) 与 `FUNDER_ADDRESS`

## 安装与运行

整个应用通过 Docker 一键部署。

1. **克隆仓库**

```bash
git clone https://github.com/liukaining/PolyMatrixEngine.git
cd PolyMatrixEngine
```

2. **配置环境变量**

```bash
cp .env.example .env
# 编辑 .env，至少配置：
# PK, FUNDER_ADDRESS, LIVE_TRADING_ENABLED, BASE_ORDER_SIZE, GRID_LEVELS, MAX_EXPOSURE_PER_MARKET
```

3. **启动服务**

```bash
docker compose up --build -d
```

- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501`

4. **查看日志**

```bash
docker compose logs -f api
```

## Dashboard & Monitoring

PolyMatrix Engine 自带一个基于 Streamlit 的监控面板。

- 打开浏览器访问：`http://localhost:8501`
- 面板功能：
  - **Control Panel**：输入 Condition ID 或 Polymarket URL，并勾选确认框，一键启动 Quoting Engine。
  - **Inventory & Risk**：条形图展示每个市场 Yes/No 敞口与已实现 PnL，并附带 Gamma/Polymarket 链接。
  - **Active Orders**：展示当前所有 `OPEN` / `PENDING` 订单，时间统一为东八区。
  - **Market Explorer**：从 Gamma 拉取热门活跃市场，一键挑选并启动做市。
  - **Emergency Controls**：对指定 `condition_id` 执行 Stop / Liquidate。

## 主要 API

- 健康检查

```bash
curl http://localhost:8000/health
```

- 启动某个市场的做市

```bash
curl -X POST http://localhost:8000/markets/{condition_id}/start
```

- 停止 / 强平

```bash
curl -X POST http://localhost:8000/markets/{condition_id}/stop
curl -X POST http://localhost:8000/markets/{condition_id}/liquidate
```

- 查看市场风险

```bash
curl http://localhost:8000/markets/{condition_id}/risk
```

- 查看当前活动订单

```bash
curl http://localhost:8000/orders/active
```

## 关键配置项（.env）

- `LIVE_TRADING_ENABLED`：`True` 实盘 / `False` Dry-Run。
- `MAX_EXPOSURE_PER_MARKET`：单市场最大敞口上限（USDC 名义）。
- `BASE_ORDER_SIZE`：每笔限价单的下单 size（需 ≥ Polymarket `orderMinSize`）。
- `GRID_LEVELS`：每一侧（买/卖）的网格层数。
- 其他基础设施和网络配置：
  - `PM_WS_URL` / `PM_API_URL` / `PM_CHAIN_ID`
  - `DATABASE_URL` / `REDIS_URL`
  - `ALCHEMY_RPC_URL`（当前版本仅预留）

## Disclaimer

This software is provided for educational and experimental purposes. Using it to trade on Polymarket carries significant financial risk. The developers assume no responsibility for any trading losses.
