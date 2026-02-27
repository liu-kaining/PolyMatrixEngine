## PolyMatrix Engine: 技术架构说明

### 1. 总体架构

系统采用一个主 FastAPI 进程 + 多个后台异步任务的结构：

- FastAPI:
  - 暴露外部 API（/health, /markets/... 等）。
  - 在 lifespan startup 中启动：
    - MarketData Gateway（公共盘口 WS）
    - UserStream（私人订单/成交 WS）
    - Risk Watchdog（风控守护进程）
- Quoting Engine:
  - 对每个 `(condition_id, token_id)` 启动一个引擎协程（`start_quoting_engine`）。
  - 通过 Redis PubSub 与 MarketData / Risk Watchdog 通信。
- Dashboard:
  - 独立 Streamlit 进程，通过同步 SQLAlchemy + pandas 读取 Postgres。

基础设施：

- **PostgreSQL**：订单状态机与 inventory 账本。
- **Redis**：订单簿 cache 与高频 tick 通道。
- **Polymarket CLOB**：通过 `py-clob-client` 完成实际下单/撤单。
- **Gamma API**：解析 condition → CLOB tokenIds、slug 等元数据。

### 2. 关键组件设计

#### 2.1 MarketData Gateway (`app/market_data/gateway.py`)

- 使用 `websockets` 连接 `PM_WS_URL`。
- 内部维护：

  ```python
  self.orderbook.books: Dict[asset_id, {"bids": {price: size}, "asks": {...}}]
  ```

- 处理逻辑：
  - 对 `event_type == "book"`：
    - 清空该 `asset_id` 的 bids/asks，用全量数据重建本地簿。
  - 对 `event_type == "price_change"`：
    - 根据 `side` / `price` / `size` 修改对应档位（size=0 则删除）。
  - 每次更新后，调用 `snapshot(asset_id, depth=5)`：
    - 排序 bids/asks，取 Top-5；
    - 若任一侧为空，则不发布 tick。
  - 最终将：

    ```python
    {
      "asset_id": ...,
      "bids": [{"price": "0.17", "size": 948035.62}, ...],
      "asks": [...]
    }
    ```

    写入 Redis `ob:{asset_id}` 并 publish 到 `tick:{asset_id}`。

- 初始快照 `fetch_initial_snapshot(token_id)`：
  - 使用 `httpx` 调用 `GET {PM_API_URL}/book?token_id=...`。
  - 404 视为软 warning（部分冷门市场没有 REST 订单簿），不阻塞引擎。
  - 成功时将返回数据 seed 入 LocalOrderbook，并立即发布 `tick`。

#### 2.2 Gamma Client (`app/market_data/gamma_client.py`)

- 使用 `GET https://gamma-api.polymarket.com/markets?condition_ids=...`。
- 从返回 JSON 的 `clobTokenIds` 中解析出 **Yes/No 的 CLOB tokenId**：
  - 按惯例 index 0 为 Yes，index 1 为 No。
- 提供 `get_market_tokens_by_condition_id(condition_id)` 返回 `(yes_token_id, no_token_id)`。

#### 2.3 Quoting Engine (`app/quoting/engine.py`)

- **报价原则：少而精，高概率赚钱。** 不为了成交而成交：BUY 仅挂在 `fair_value - spread/2` 及以下，不贴 Best Bid；只有市场主动卖到我们挂单价时才成交，保证每笔成交都有正 edge。不轻易出手，一出手就要能高概率赚钱。

- 核心结构：

  ```python
  class QuotingEngine:
      condition_id: str
      token_id: str
      grid_levels: int         # 来自 settings.GRID_LEVELS
      base_size: float         # 来自 settings.BASE_ORDER_SIZE
      price_offset_threshold: float
      last_anchor_mid_price: Optional[float]
      is_yes_token: Optional[bool]
      active_orders: Dict[order_id, order_id]
      suspended: bool
      _trade_lock: asyncio.Lock
  ```

- 订阅 Redis：

  ```python
  await pubsub.subscribe(f"tick:{token_id}", f"control:{condition_id}")
  ```

- `AlphaModel.calculate_alpha(bids, asks, current_exposure)`：

  - 取最优一档：
    - `best_bid_price`, `best_ask_price`，对应 `size`。
  - 计算 mid：

    \[
    mid = (best\_bid\_price + best\_ask\_price) / 2
    \]

  - OBI：

    \[
    obi = \frac{bid\_size - ask\_size}{bid\_size + ask\_size}
    \]

  - OBI skew：

    \[
    obi\_skew = obi \times 0.015
    \]

  - Inventory skew：

    \[
    inv\_skew = - current\_exposure \times inventory\_skew\_factor
    \]

  - Skewed Fair Value：

    \[
    fair\_value = \text{clip}(mid + obi\_skew + inv\_skew,\ 0.01,\ 0.99)
    \]

  - Dynamic Spread：

    \[
    spread = base\_spread \times (1 + |obi|)
    \]

  - `on_tick(tick_data)` 流程（V1.1 引入库存感知的不对称甩货）：
 
  1. 若 `bids/asks` 缺失则跳过（LocalOrderbook 已保证正常情况下两边都有）。
  2. 在 `_trade_lock` 保护下：
     - 通过 DB 读取 `InventoryLedger` 和 `MarketMeta`，确定当前 exposure。
     - 调用 `AlphaModel` 计算 `fair_value` 与 `dynamic_spread`。
     - 若 `|fair_value - last_anchor_mid_price| <= price_offset_threshold`，则跳过重置。
     - 更新 `last_anchor_mid_price = fair_value`。
  3. 计算首档 bid/ask：
 
     ```python
     anchor = spread / 2
     bid_1 = round(fair_value - anchor, 2)
     ask_1 = round(fair_value + anchor, 2)
     ```
 
  4. 根据当前敞口选择网格模式：
 
     - **Neutral / 轻仓（|exposure| < 5）**：
       - 仅生成 BUY 网格：
 
         ```python
         for i in range(self.grid_levels):
             bid_price = round(bid_1 - i * tick_size, 2)
             if 0.01 <= bid_price <= 0.99:
                 orders.append(BUY at bid_price, size=base_size)
         ```
 
       - 不生成 SELL，专注于“接盘建仓”。
 
     - **Long / 重仓（exposure >= 5）**：
       - 完全停止 BUY，只生成单侧 Aggressive SELL：
 
         ```python
         best_ask = asks[0]["price"]
         aggressive_ask = min(fair_value + 0.01, best_ask - 0.01)
         ask_price = clip(round(aggressive_ask, 2), 0.01, 0.99)
         sell_size = min(exposure, max(5.0, base_size))
         orders = [SELL at ask_price, size=sell_size]
         ```
 
       - `BASE_ORDER_SIZE` 在内部会被 `max(5.0, base_size)` 处理，以满足 CLOB `orderMinSize`。
  5. 打印完整决策日志（Top Book + Alpha 输出 + 模式 / 指令 JSON）。
  6. **同步：先 `cancel_all_orders()` 再 `place_orders(orders_payload)`**。

- `cancel_all_orders` / `place_orders`：
  - `place_orders` 使用 `oms.create_order(...)` 并收集返回的真实 `order_id` 填入 `active_orders`。
  - `cancel_all_orders` 遍历 `active_orders` 调 `oms.cancel_order`，成功则从 dict 中删除，失败保留重试。

#### 2.4 OMS (`app/oms/core.py`)

- 初始化：

  ```python
  self.client = ClobClient(
      host=settings.PM_API_URL,
      key=settings.PK,
      chain_id=settings.PM_CHAIN_ID,
      signature_type=2,
      funder=settings.FUNDER_ADDRESS,
  )
  ```

- `create_order(...)`：

  1. 会话 1：写 `OrderJournal` 为 `PENDING`。
  2. 根据 `LIVE_TRADING_ENABLED` & `self.client` 决定：
     - Dry-Run：sleep 模拟延迟，直接标记为 `OPEN` 并写 mock payload。
     - Live：
       - 构造 `OrderArgs`；
       - 通过 `CircuitBreaker` 包裹的 `_place_order()` 在 `asyncio.to_thread` 中调用 `client.create_and_post_order`。
       - 成功则标记 `OPEN`、更新 `order_id` & `payload`；失败则 `FAILED` 并写错误信息。
  3. 会话 2：重新 fetch `OrderJournal`，做状态更新和 payload merge。

- `cancel_order(order_id)`：
  - Dry-Run：模拟 `CANCELED`。
  - Live：
    - 通过 `CircuitBreaker` 调用 `client.cancel(order_id)`（同样通过 `to_thread`）。
    - 成功后更新 `OrderJournal` → `CANCELED`，并尝试通过 `client.get_order(order_id)` 检查 `size_matched` 情况，写入 `status_detail` 等字段。

- `cancel_market_orders(condition_id)`：
  - 查出该市场所有 `PENDING/OPEN` 订单，逐个 `cancel_order` 并统计成功/失败数。
  - 失败会打印 `CRITICAL` 级别日志，供 Kill Switch 观察。

- `CircuitBreaker`：
  - 维护 `failures` / `state` / `last_failure_time`。
  - 在 `OPEN` 状态下直接拒绝请求；超过 `recovery_timeout` 后进入 `HALF_OPEN`，成功一次就 `CLOSED` 重置。

#### 2.5 Risk Watchdog (`app/risk/watchdog.py`)

- 长时间运行的 `watchdog.run()` 协程：
  - 周期性执行：
    - 实时敞口检查（根据 `MAX_EXPOSURE_PER_MARKET`）。
    - 可选的链上对账（当前版本重点是行级锁 + 安全覆盖）。
  - 任意超限场景会：
    - 通过 Redis Control Channel 发送 `suspend`；
    - 调 `oms.cancel_market_orders(condition_id)`。

#### 2.6 User Stream (`app/market_data/user_stream.py`)

- 通过 Polymarket 用户 WS 监听：
  - Order filled / canceled / closed 等事件。
- 对每个事件：
  - 在事务中锁对应 `OrderJournal` 行（如使用 `SELECT ... FOR UPDATE`）。
  - 累积 `filled_size`，更新 `OrderStatus` 与 `InventoryLedger` 中的 Yes/No exposure 与 realized PnL。
  - 对“先部分成交再撤单”设置 `status_detail` 标识，保证不会出现长期幽灵挂单（DB 里 `OPEN` 但实际上已经彻底关闭）。

#### 2.7 Dashboard (`dashboard/app.py`)

- 使用同步 SQLAlchemy + pandas：
  - `fetch_inventory()`：查询 `inventory_ledger`。
  - `fetch_active_orders()`：查询 `orders_journal` 中 `OPEN/PENDING` 订单。
- 侧边栏 Control Panel：
  - 表单内输入 Condition ID 或 Polymarket URL，点击 `Start Quoting` 时：
    - 若未勾选确认框或未填 ID/URL，则仅提示，不调用 API。
    - 表单验证通过后，仅将解析出的 `condition_id` 写入 `st.session_state["pending_start_condition_id"]`。
  - 表单下方渲染一个二次确认区域：
    - 显示待启动的 `condition_id`。
    - 用户点击 `✅ Confirm Start` 时才真正调用 `POST /markets/{condition_id}/start`。
- Emergency Controls：
  - Stop / Liquidate 按钮不再直接打 API，而是先在 `session_state` 里记录 `pending_kill_action` + `pending_kill_condition_id`。
  - 在侧边栏渲染一个确认块，确认后调用 `/markets/{id}/stop` 或 `/markets/{id}/liquidate`，并 `st.rerun()`。
- Inventory & Risk：
  - 顶部三个 metric：Active Markets / Total Realized PnL / Total Gross Exposure。
  - `Market Exposures (USDC)` 通过 `st.expander` 包裹，仅在有敞口时默认展开。
  - Inventory Ledger 表增加：
    - `gamma_link`：`https://gamma-api.polymarket.com/markets?condition_ids={market_id}`，在 UI 中用 `LinkColumn` 显示为 `condition_id`。
    - `polymarket_link`：通过 Gamma 解析 `slug`，显示为 `slug`，链接到 `https://polymarket.com/event/{slug}`。
- Market Screener (Gamma)：
  - 从 Gamma 拉取 `active=true, closed=false` 的市场列表（limit 约 500），然后在本地做：
    - Binary-only 过滤（outcomes 为 Yes/No）。
    - Sports / Live 盘口黑名单（基于 tags/category/slug/question 的关键词匹配）。
    - DTE / 成交量 / 流动性 / YES 赔率区间过滤（取决于 Conservative / Normal / Aggressive / Ultra 模式）。
    - 基于标签和语义（question/slug）构建 `Category/Tag` 列，高亮 `⭐ Politics` / `⭐ Culture` 等优质赛道。
  - 使用 `st.data_editor` 渲染 Screener 表格，增加 `Selected` 复选框列，并将选中行的 index 存入 `st.session_state["screener_selected_idx"]`。
  - 下方 `Selected market` 卡片通过 HTML/CSS 渲染为高亮卡片，展示 Question 全文、Condition ID、YES Price、24h Volume、Liquidity 与 End Date。
  - `Start from Screener` 按钮基于当前选中行，将 `pending_screener_start_cid` 写入 `session_state`，并在下方渲染确认区域；只有确认按钮点击后才调用 `/markets/{condition_id}/start`。
- System Logs：
  - FastAPI 通过 `RotatingFileHandler` 将业务日志写入 `/app/data/logs/trading.log`，并通过 `logs_data` 卷与 Dashboard 共享。
  - Dashboard 中提供 `tail_logs(path, lines)` 工具函数从日志文件末尾读取最近若干行。
  - `System Logs (Tail & Search)` 面板：
    - 支持按级别（ALL/INFO/WARNING/ERROR）和关键词做简单 grep。
    - 使用 `st.code(..., language="log")` 展示，并提供 `🔄 Refresh Logs` 按钮。

### 3. 数据模型

核心表（已实现）：

- `markets_meta` / `MarketMeta`
  - `condition_id, yes_token_id, no_token_id, status, ...`
- `orders_journal` / `OrderJournal`
  - `order_id, market_id, side, price, size, status, payload, created_at, ...`
- `inventory_ledger` / `InventoryLedger`
  - `market_id, yes_exposure, no_exposure, realized_pnl, updated_at, ...`

### 4. 配置与部署

- 所有环境变量通过 `pydantic-settings` 的 `Settings` 类集中管理。
- Dockerfile：
  - 安装 Python 依赖（包括 `py-clob-client`, `httpx`, `streamlit`, `psycopg2-binary` 等）。
  - 启动顺序：`alembic upgrade head` → `uvicorn app.main:app`。
- `docker-compose.yml`：
  - 暴露 API (`8000`) 和 Dashboard (`8501`)。
  - 为 `api` 服务透传关键 env：`LIVE_TRADING_ENABLED`, `MAX_EXPOSURE_PER_MARKET`, `BASE_ORDER_SIZE`, `GRID_LEVELS` 等。

### 5. 未来扩展点（与旧文档对齐说明）

- **Builder Relayer 客户端**
  - 现有实现只用 `py-clob-client` 与 CLOB API 通信。
  - 早期文档中提到的 `py-builder-relayer-client` 可作为未来优化：在 Maker 日常路径上走 relayer，在 Kill Switch 时可直连 CLOB 或合约。

- **链上 Kill Switch (Alchemy RPC)**
  - 目前 `ALCHEMY_RPC_URL` 仅存于配置中，代码未直接调用。
  - 若未来实现真正的“合约级”熔断，可以在 Watchdog 中：
    - 通过 Web3 连接 Alchemy；
    - 调用 CTF 合约接口直接取消订单或转移头寸。

- **外部 Alpha 源**
  - 目前 AlphaModel 以盘口失衡 + 库存 skew 为主。
  - PRD 中提的传统赔率 / 大模型情绪可接入 `AlphaPricingModel` 的外部数据源列表中，作为 fair value 的额外锚点。