## PolyMatrix Engine: 产品需求文档 (PRD)

### 1. 产品愿景与定位

PolyMatrix Engine 面向 Polymarket，提供自动化高频做市与轻量统计套利能力。

- 将每个预测事件视作 **二元期权**，而不是“单向买涨买跌”的投机工具。
- 主要收益来自：
  - 提供连续、深度、双边的网格流动性，赚取 **官方 Maker 激励** 与 **价差收益**；
  - 在极端情绪下做 **概率回归** 的统计套利。

系统追求的是：

- **资金安全优先**：严格的敞口上限、CircuitBreaker、Kill Switch。
- **架构鲁棒**：任何异常（API 错误、WS 掉线、对账偏差）优先进入“安全模式”，而不是盲目继续交易。
- **运营易用**：提供驾驶舱 Dashboard，可以一键启动/停止做市，清晰看见持仓和订单。

### 2. 用户与使用场景

- **量化开发者 / 策略研究员**
  - 希望在 Polymarket 上快速迭代做市策略，把精力放在定价模型，而不是底层连接、订单对账。

- **小资金测试 / Builder Program 申请者**
  - 在资金与仓位极度受限的情况下，希望通过高频、严格封控的小规模做市行为，展示技术能力与稳定性。

典型使用场景：

1. 设置较小的 `BASE_ORDER_SIZE` 与严苛的 `MAX_EXPOSURE_PER_MARKET`（比如 5–20 USDC）。
2. 在 1–3 个高流动性市场上启动做市引擎。
3. 通过 Dashboard 实时观察：
   - 网格挂单是否在前端正常展现；
   - Inventory Ledger 是否按成交变化；
   - Risk Watchdog 是否能在极端行情下及时“踩刹车”。

### 3. 功能需求（按模块）

#### 3.1 市场数据中心 (Market Data Hub)

**目标：** 保证 Quoting Engine 永远在“有视野”的订单簿状态下决策，而不是盲目乱挂单。

- **F1 — 初始快照**
  - 在某个 `condition_id` 启动做市时，系统必须：
    - 通过 Gamma API 获取 Yes/No `token_id`；
    - 通过 `GET /book?token_id=...` 拉取最新订单簿；
    - 将完整快照写入 Redis 并立刻推送第一条 `tick` 给 QuotingEngine。

- **F2 — 本地订单簿缓存**
  - 对每个 `asset_id/token_id` 维护内存级 LocalOrderbook：
    - 收到 `book` 事件时，全量覆盖；
    - 收到 `price_change` 事件时，逐档更新/删除。
  - 任何时候推给策略的都是**完整的双边 Top-N 快照**。

- **F3 — 可靠性与重连**
  - 实现心跳 `PING/PONG` 机制；
  - WS 掉线后指数退避重连，成功后自动 `resubscribe` 上次订阅的 asset 列表。

#### 3.2 动态定价与做市引擎 (Quoting Engine)

**目标：** 在给定资金约束下，提供智能、弹性的 Maker 网格。

- **F4 — Dynamic Fair Value & Spread**
  - 使用 `AlphaModel`：
    - 基于 BBO 中间价计算基础 fair value。
    - 根据一档深度的 OBI（买卖量失衡）调整价格偏移（OBI skew）。
    - 根据当前 Yes/No 仓位调整价格偏移（Inventory skew）。
    - 根据 OBI 大小自适应调节 spread 宽度（越失衡 → spread 越宽）。
  - Fair Value 必须限制在 [0.01, 0.99] 价格区间内。

- **F5 — Grid 配置**
  - 每边网格层数可配置：`GRID_LEVELS`。
  - 每单下单数量可配置：`BASE_ORDER_SIZE`，但必须 ≥ 市场 `orderMinSize`。
  - 动态网格示例：
    - 给定 fair value 和 spread/2 为 anchor：
      - Buy：`fair_value - spread/2, fair_value - spread/2 - tick_size * (i)`。
      - Sell：`fair_value + spread/2, fair_value + spread/2 + tick_size * (i)`。

- **F6 — Debounce 与节流**
  - 当新的 fair value 与上次 anchor 的差异小于 `price_offset_threshold` 时，放弃重置网格，减少频繁 cancel/post。

- **F7 — 原子级网格刷新**
  - 任意一次网格重置过程必须是：
    - 先 cancel 全部当前 `active_orders`；
    - 再下新网格；
    - Cancel 失败的订单被标记错误并保留，以便下次 Kill Switch 重试。

- **F8 — 控制信号**
  - `suspend`：引擎内部状态转为 `suspended`，同步执行 `cancel_all_orders`。
  - `resume`：恢复正常响应 tick。

#### 3.3 Order Management System

**目标：** 确保每一笔请求在 DB 中有完整、可追溯的状态轨迹。

- **F9 — 订单状态机**
  - 状态流转必须经过：
    - `PENDING`（本地写入） →
    - `OPEN` / `FAILED`（收到回执后更新）。
  - 对每笔订单保留原始 CLOB API 响应 payload（JSONB），支持之后对账/审计。

- **F10 — Dry-Run 支持**
  - 在未配置私钥或关闭 `LIVE_TRADING_ENABLED` 时：
    - 所有下单/撤单仅在本地 DB 生效，模拟状态流转；
    - 不访问真实 CLOB API，确保可以在无资金环境下演练策略。

- **F11 — 熔断保护**
  - 连续多次错误（如 `not enough balance / allowance` / `size < min_order_size` 等）达到阈值后：
    - CircuitBreaker 进入 OPEN 状态；
    - 阻断新的下单请求，避免在资金不足情况下不断失败。

#### 3.4 风险监控与 Kill Switch

- **F12 — 敞口上限**
  - 对每个 `market_id` 计算：
    - Yes / No exposure；
    - 若任一绝对值超过 `MAX_EXPOSURE_PER_MARKET`，触发：
      - 发布 `suspend` 控制信号；
      - `cancel_market_orders(condition_id)` 撤销该市场所有挂单。

- **F13 — 一键强平 (Liquidate)**
  - 提供后台 API `/markets/{condition_id}/liquidate`：
    - 先 suspend + cancel；
    - 再读取 `InventoryLedger`，对 Yes/No exposure 分别在 0.01 价位挂出 SELL 单，以最大概率立即成交，清空仓位。

- **F14 — 用户层 Kill Switch**
  - Dashboard 中提供 **Stop / Liquidate 按钮**：
    - 通过用户输入的 `condition_id` 调用上述 API。
    - 操作后自动 `st.rerun()` 刷新页面，确保状态一致。

#### 3.5 监控与可视化 (Dashboard)

- **F15 — Inventory & Risk 面板**
  - 展示所有正在做市/有仓位的市场列表：
    - `market_id`、Yes/No 敞口、realized PnL。
    - 可视化 bar chart。
    - Gamma / Polymarket 直接跳转链接。

- **F16 — Active Orders**
  - 展示当前 `OPEN/PENDING` 订单列表：
    - 价格、方向、数量、时间戳，帮助排查具体某一市场的网格结构。

- **F17 — 系统状态**
  - 至少提供：
    - FastAPI `/health` 状态；
    - 引导用户通过 `docker compose logs -f api` 查看实时撮合日志。

### 4. 非功能性需求

- **可靠性**
  - WS 重连、Redis/DB 初始失败重试。
  - 任意错误优先 fail-safe：宁可停机 / 熔断，也不做“不知道自己在干嘛”的交易。

- **可观察性**
  - 日志必须打印：
    - Grid 决策要点（Fair Value、Spread、Top Book、指令 payload）。
    - 关键风险事件：熔断 OPEN、Kill Switch 执行、对账重写等。
  - 日志时间统一使用北京时间。

- **可配置性**
  - 所有关键交易参数（敞口上限、单笔 size、grid 层数、是否实盘）都通过 `.env` 控制，无需改代码。