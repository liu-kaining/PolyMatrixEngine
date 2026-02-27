## PolyMatrix Engine V1.0 架构与功能实现全景白皮书

> 面向对象：CTO / 架构师  
> 代码基线：当前工作区 `main` 分支，包含 `app/` 全部模块 + 数据库 + Streamlit Dashboard

---

### 1. 系统全局架构与流转路径 (System Architecture & Data Flow)

#### 1.1 组件总览

核心组件与外部依赖：

- **Polymarket 外部系统**
  - CLOB WebSocket（市场盘口）：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
  - CLOB WebSocket（用户订单）：`wss://ws-subscriptions-clob.polymarket.com/ws/user`
  - CLOB REST：`https://clob.polymarket.com`
  - Gamma Markets API：`https://gamma-api.polymarket.com`
  - Data API（positions）：`https://data-api.polymarket.com`
- **内部核心模块**
  - `app/market_data/gateway.py` → `MarketDataGateway` + `LocalOrderbook`
  - `app/market_data/user_stream.py` → `UserStreamGateway`
  - `app/quoting/engine.py` → `QuotingEngine` + `AlphaModel`
  - `app/oms/core.py` → `OrderManagementSystem`（全局实例 `oms`）
  - `app/risk/watchdog.py` → `RiskMonitor`（全局实例 `watchdog`）
  - `app/main.py` → FastAPI + 生命周期 + 管理 API
  - `dashboard/app.py` → Streamlit Dashboard
  - `app/models/db_models.py` → `MarketMeta`, `OrderJournal`, `InventoryLedger`
  - `app/core/redis.py` → `RedisManager`（全局 `redis_client`）

持久化与基础设施：

- PostgreSQL：订单状态机与库存账本（通过 `DATABASE_URL`）
- Redis：orderbook cache + tick pub/sub + 其他高频 kv（`REDIS_URL`）

#### 1.2 从「市场发现」到「引擎启动」

1. **Dashboard 选择目标市场**
   - 用户在 `dashboard/app.py`：
     - 方式 A：侧边栏 `Control Panel` 输入 **condition_id 或 Polymarket URL**，例如：
       - `0x747dc8...f741f75`
       - `https://polymarket.com/event/will-the-us-confirm-that-aliens-exist-before-2027`
     - 方式 B：在 **Market Explorer (Gamma)** 中点击 `Load Top Markets`：
       - `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20`
       - 按 question / volume / liquidity 显示卡片，用户在卡片上点击「Confirm & Start Quoting」。

2. **Dashboard → FastAPI `/markets/{condition_id}/start`**
   - `dashboard/app.py` 使用 `requests.post(f"{API_URL}/markets/{condition_id}/start")` 调用后端。
   - FastAPI 端点：`app/main.py:start_market_making`：
     - 若本地 `MarketMeta` 不存在或缺少 `yes_token_id/no_token_id`：
       - 通过 `gamma_client.get_market_tokens_by_condition_id(condition_id)` 调用 Gamma：
         - `GET https://gamma-api.polymarket.com/markets?condition_ids=<condition_id>`
         - 解析 `clobTokenIds`，索引 0 为 YES，1 为 NO。
       - 写入/更新 `MarketMeta` 和对应 `InventoryLedger`（market_id = condition_id）。

3. **Live 资金预检查（Best-effort）**
   - 若 `LIVE_TRADING_ENABLED=True` 且 `oms.client` 存在：
     - 如果 `ClobClient` 暴露 `get_balance()`：
       - 若余额 < `MIN_REQUIRED_USDC`（默认 50.0），直接 `HTTP 400` 拦截，不启动引擎。
     - 否则仅打印 warning，不阻止启动（兼容 Dry-Run / SDK 变化）。

#### 1.3 行情流：Polymarket → Gateway → Redis → QuotingEngine

1. **WS 连接与订阅**
   - `MarketDataGateway.connect()` 在 FastAPI lifespan 启动：
     - `websockets.connect(settings.PM_WS_URL, ping_interval=None)`
     - 建立连接后：
       - 启动 `_heartbeat()` 定期发送 `"PING"`。
       - 若已有订阅资产，则调用 `_resubscribe()`：
         - 发送：
           ```json
           {
             "assets_ids": [...],
             "type": "market",
             "operation": "subscribe",
             "custom_feature_enabled": true
           }
           ```
   - `/markets/{condition_id}/start` 完成 Gamma & DB 更新后：
     - 调用 `await md_gateway.subscribe([yes_token_id, no_token_id])`，将这两个 token 加入 `subscribed_markets`，并在已连接时发起订阅消息到 WS。

2. **LocalOrderbook 维护与增量合并**

   文件：`app/market_data/gateway.py`  
   类：`LocalOrderbook`

   - 内部结构：
     ```python
     self.books: Dict[str, Dict[str, Dict[str, float]]] = {
         asset_id: {
             "bids": {price_str: size_float},
             "asks": {price_str: size_float},
         }
     }
     ```

   - 事件处理 `LocalOrderbook.apply_event(data)`：
     - `event_type == "book"`（全量簿）：
       - reset `books[asset_id] = {"bids": {}, "asks": {}}`
       - 遍历 data.bids / data.asks 填充 `{price: size}`。
     - `event_type == "price_change"`（增量）：
       - 对 `data.price_changes` 中每条：
         - 解析 `asset_id / side / price / size`。
         - 如果 `size == 0`：从对应 `bids/asks` dict 删除该档。
         - 否则写入/覆盖 `book[price] = size`。
   - Snapshot 输出 `LocalOrderbook.snapshot(asset_id, depth=5)`：
     - bids：按价格降序取 Top-5；
     - asks：按价格升序取 Top-5；
     - 如果任一侧为空，则返回 `None`（保证引擎总是看到双边完整簿）。

3. **初始快照（Initial Snapshot）**

   - 在 `/markets/{condition_id}/start` 中，启动 QuotingEngine 后 `await asyncio.sleep(0.5)`，使引擎先完成 Redis PubSub 订阅。
   - 然后调用：
     ```python
     await md_gateway.fetch_initial_snapshot(yes_token_id)
     await md_gateway.fetch_initial_snapshot(no_token_id)
     ```
   - `fetch_initial_snapshot(token_id)`：
     - 发起 `GET {PM_API_URL}/book?token_id=...`（CLOB REST）。
     - 若 200：
       - 使用返回的 `bids/asks` 调用 `LocalOrderbook.seed` 初始化本地簿。
       - 调用 `snapshot()` 获取 Top-5 快照 `snap`。
       - 写入 Redis：`set_state("ob:{token_id}", snap)`。
       - 发布第一条 tick：`publish("tick:{token_id}", snap)`，触发 QuotingEngine 进入首次网格报价。
     - 若 404：
       - 打印 warning（说明该 token 订单簿暂时无数据），引擎将依赖后续 WS 增量触发。

4. **Redis → QuotingEngine**

   - `QuotingEngine.run()`（`app/quoting/engine.py`）：
     ```python
     pubsub = redis_client.client.pubsub()
     await pubsub.subscribe(f"tick:{self.token_id}", f"control:{self.condition_id}")
     async for message in pubsub.listen():
         ...
     ```
   - Tick 流：
     - `channel == f"tick:{token_id}"` 且 `not self.suspended` → 进入 `on_tick(tick_data)` 进行定价与下单。
   - 控制流：
     - `channel == f"control:{condition_id}"` → 进入 `on_control_message(data)`，处理 `suspend` / `resume`，协调 Kill Switch 与 API 控制。

#### 1.4 做市与订单执行：QuotingEngine → OMS → Polymarket CLOB

1. **定价与网格指令生成**
   - `on_tick()` 逻辑见 2.2 节，输出一个 `orders_payload` 列表，每项包括：
     - `condition_id, token_id, side (BUY/SELL), price, size (BASE_ORDER_SIZE)`

2. **网格原子刷新**
   - `on_tick()` 内部在 `_trade_lock` 下执行：
     ```python
     await self.cancel_all_orders()
     await self.place_orders(orders_payload)
     ```
   - `cancel_all_orders()`：
     - 遍历 `self.active_orders` 中所有 `order_id` 调 `oms.cancel_order(order_id)`。
     - 对取消成功的，从 `active_orders` 中删除；失败则保留并打印 CRITICAL 日志，供下一次 Kill Switch 重试。

   - `place_orders()`：
     - 并发调用 `oms.create_order(...)`。
     - 收集返回的真实 `order_id`：
       ```python
       if isinstance(res, str):
           self.active_orders[res] = res
       ```

3. **OMS → Polymarket CLOB**

   文件：`app/oms/core.py`  
   类：`OrderManagementSystem`，全局实例 `oms`

   - 初始化 `ClobClient`：
     ```python
     self.client = ClobClient(
         host=settings.PM_API_URL,
         key=settings.PK,
         chain_id=settings.PM_CHAIN_ID,
         signature_type=2,
         funder=settings.FUNDER_ADDRESS,
     )
     self.client.create_or_derive_api_creds()
     self.client.set_api_creds(creds)
     ```

   - 下单生命周期 `create_order()`：
     1. **本地预写入 (PENDING)**：
        - 在一个 `AsyncSessionLocal` 内插入 `OrderJournal`：
          - `order_id = local_{token_id}_{side}_{timestamp}`（临时 ID）
          - `market_id = condition_id`
          - `side, price, size, status=PENDING`
          - `payload={"token_id": token_id}`
        - `session.commit()` 后结束事务。
     2. **真实执行（Dry-Run 或 Live）**
        - 若 `!self.client or !self.live_trading_enabled`：
          - 模拟网络延迟 `await asyncio.sleep(0.5)`。
          - 标记 `api_status = OPEN`，`api_payload = {"mock_response": "Success (Dry-Run)"}`。
        - 否则构造 `OrderArgs` 并通过 `CircuitBreaker.execute(_place_order)` 执行：
          ```python
          async def _place_order():
              return await asyncio.to_thread(self.client.create_and_post_order, order_args)
          ```
          - 这里用 `asyncio.to_thread` 包裹同步 HTTP，避免阻塞事件循环。
          - 如果返回 `{"success": true, "orderID": ...}`：
            - 标记 `api_status=OPEN`，`final_order_id=orderID`。
          - 否则将错误信息写入 `api_payload`，`api_status=FAILED`。
     3. **状态机收尾 (OPEN/FAILED)**：
        - 打开第二个 `AsyncSessionLocal`，`session.get(OrderJournal, order_id)`。
        - 如果 `final_order_id != order_id`（live 模式）：
          - 更新 DB 中的 `order_id = final_order_id`。
        - 更新 `order.status = api_status`，合并 `payload`。
        - `session.commit()` 并返回最终真实 `order_id`（仅在 OPEN 时返回）。

   - 撤单 `cancel_order(order_id)`：
     - Dry-Run：模拟设为 `CANCELED`。
     - Live：
       - 通过 `CircuitBreaker.execute(_cancel)` 调用 `client.cancel(order_id)`（同样用 `to_thread`）。
       - **关键修正**：对多种 cancel 返回格式做归一化：
         - `"Canceled"` 字符串；
         - dict 形式的：
           - `{"not_canceled": {}, "canceled": [order_id, ...]}`；
           - `{"not_canceled": {order_id: "order can't be found - already canceled or matched"}, "canceled": []}`；
           - legacy `{"success": true, ...}`。
       - 所有这几种都视为「订单在 CLOB 上不再活跃」，因此本地将 `OrderJournal.status = CANCELED`，并写入 `status_detail` 标记 `ALREADY_CLOSED_ON_CLOB`（如果是“already canceled or matched”）。
       - 若完全失败（API 异常或格式不识别），返回 False，由上层 Kill Switch 打出 CRITICAL 并重试。

   - **CircuitBreaker**
     - 状态字段：`state ∈ {CLOSED, HALF_OPEN, OPEN}`，`failures`，`last_failure_time`。
     - 每次执行失败 `record_failure()`，超过 `failure_threshold`（默认 5 次）后转为 `OPEN`：
       - 在 `OPEN` 期间直接拒绝新的下单/撤单请求，抛错 `"Circuit breaker is OPEN"`。
     - 超过 `recovery_timeout`（默认 60s）后重新尝试，将状态改为 `HALF_OPEN`，若下一次成功则 `reset()` 回 `CLOSED`。

#### 1.5 UserStream & 风控闭环：成交 / 撤单 / 对账

1. **User WebSocket → UserStreamGateway**

   - `user_stream.connect()`：
     - 等待 `oms.client` 和 `creds` 就绪。
     - 连接 `wss://ws-subscriptions-clob.polymarket.com/ws/user`。
     - 重新订阅已跟踪的 `condition_id` 集合，认证使用 `apiKey/secret/passphrase`。
   - `subscribe(condition_id)`：
     - 将 `condition_id` 放入 `subscribed_markets`，在连接时发送：
       ```json
       {
         "auth": {...api_creds...},
         "markets": [condition_id, ...],
         "type": "user"
       }
       ```

2. **成交处理 `handle_fill(order_id, filled_size, fill_price)`**

   - 为防止并发写冲突，使用：
     ```python
     stmt = select(OrderJournal).filter(OrderJournal.order_id == order_id).with_for_update()
     ```
     对订单记录上锁。
   - 累积部分成交：
     - 在 `order.payload["filled_size"]` 中累积 `current_filled + filled_size`，容忍 1e-6 dust。
     - 若 `new_total_filled >= original_size - 1e-6` → 订单置为 `FILLED`，否则保持 `OPEN`。
   - 更新 `InventoryLedger`：
     - 通过 `MarketMeta` 判断该 `token_id` 对应 YES/NO。
     - 对 `InventoryLedger` 使用 `SELECT ... WITH FOR UPDATE` 行锁。
     - BUY → 对对应 YES/NO exposure 加 `filled_size`；
     - SELL → 减 `filled_size`，并把 `fill_price * filled_size` 累计到 `realized_pnl`。

3. **撤单通知 `handle_cancellation(order_id)`**

   - 同样使用 `WITH FOR UPDATE` 锁定 `OrderJournal` 行。
   - 若 status 不是 `CANCELED/FILLED`：
     - 根据 payload 中 `filled_size` 判断是否“部分成交后撤单”，写入 `status_detail = "PARTIALLY_FILLED_AND_CANCELED"`。
     - 设置 `status = CANCELED` 并提交。

4. **RiskMonitor & Kill Switch**

   - `RiskMonitor.check_exposure()`：
     - 周期性从 DB 中读出所有 `InventoryLedger`，一旦发现某市场 Yes/No exposure 超过 `MAX_EXPOSURE_PER_MARKET`：
       - 打印 CRITICAL，并调用 `trigger_kill_switch(market_id)`。
   - `trigger_kill_switch(condition_id)`：
     - 将 `MarketMeta.status` 置为 `suspended`。
     - 通过 Redis 发布 `control:{condition_id}` → `{"action": "suspend"}`，使所有对应 `QuotingEngine`：
       - 置 `suspended=True`；
       - 在 `_trade_lock` 下同步执行 `cancel_all_orders()`。
     - 再调用 `oms.cancel_market_orders(condition_id)`，遍历所有 `PENDING/OPEN` 订单并尝试撤单。
   - **Reconciliation（对账）**：
     - 每 `reconciliation_interval=300s` 执行一次：
       - 调用 Data API `GET /positions?user=FUNDER_ADDRESS` 拿链上真实持仓。
       - 汇总为 `{conditionId: {yes, no}}`。
       - 在 DB 中使用：
         ```python
         stmt = select(InventoryLedger).with_for_update()
         ```
         对所有 `InventoryLedger` 行加锁。
       - 对任何与链上偏差 > `exposure_tolerance`（默认 1 USDC）的市场，直接用链上数覆盖本地 `yes_exposure/no_exposure`，保证长时间运行下账本不会漂移。

---

### 2. 核心模块技术实现深度拆解 (Core Modules Deep Dive)

#### 2.1 行情网关：MarketDataGateway & LocalOrderbook

**文件：** `app/market_data/gateway.py`  
**核心类：** `LocalOrderbook`, `MarketDataGateway`

- **LocalOrderbook 设计**
  - 采用 `price → size` 字典存储，避免数组重构。
  - 所有价格作为字符串键存储，使用 `float(price)` 排序，兼容 CLOB 返回的字符串价格。

- **Deltas 合并策略**
  - 通过 `event_type` 区分全量 / 增量：
    - `book`：
      - 清空并重建整本簿。
      - 适用于初始或长时间未更新后的同步。
    - `price_change`：
      - 对指定价位执行“增删改”，以 O(1) 更新本地状态。
  - 设计上保证无论接收多频繁的增量，都可以在任何时刻通过 `snapshot(asset_id)` 获得一致性的 Top-5 盘口。

- **Redis 分发**
  - 每次订单簿变动后，仅对**有变化的 asset_id** 输出快照并 publish。
  - Snapshots 在 Redis 中既存成 KV（`ob:{asset_id}`），又以 Pub/Sub 形式（`tick:{asset_id}`），满足：
    - 引擎订阅实时 tick；
    - 其他服务可直接从 `ob:` KV 读取最新簿。

#### 2.2 微观定价与做市引擎：QuotingEngine & AlphaModel

**文件：** `app/quoting/engine.py`  
**类：** `AlphaModel`, `QuotingEngine`

- **AlphaModel 数学模型**

  给定最佳一档：

  - \(P_b = \text{best\_bid\_price}, S_b = \text{best\_bid\_size}\)
  - \(P_a = \text{best\_ask\_price}, S_a = \text{best\_ask\_size}\)

  计算：

  - 中间价：
    \[
    mid = (P_b + P_a)/2
    \]
  - 订单簿失衡：
    \[
    obi = \frac{S_b - S_a}{S_b + S_a}
    \]
  - OBI Skew（最大幅度 0.015）：
    \[
    obi\_skew = obi \times 0.015
    \]
  - Inventory Skew：
    \[
    inv\_skew = - current\_exposure \times 0.0005
    \]
  - 最终 Fair Value：
    \[
    fair\_value = \text{clip}(mid + obi\_skew + inv\_skew,\ 0.01,\ 0.99)
    \]
  - Dynamic Spread：
    \[
    spread = 0.02 \times (1 + |obi|)
    \]

- **Grid 参数**
  - `GRID_LEVELS`（可配置，默认 1 或 2）：每一侧的价位层数。
  - `BASE_ORDER_SIZE`（可配置，需 ≥ 5 USDC 对应的最小下单量）。

- **网格价格计算**

  - anchor：
    \[
    a = spread / 2
    \]
  - 首档：
    \[
    bid_1 = round(fair\_value - a, 2), \quad ask_1 = round(fair\_value + a, 2)
    \]
  - 第 `i` 层（从 0 开始）：
    \[
    bid_i = round(bid\_1 - i\times tick\_size, 2)
    \]
    \[
    ask_i = round(ask\_1 + i\times tick\_size, 2)
    \]
    其中 `tick_size = 0.01`，边界限制在 [0.01, 0.99]。

- **Debounce 机制**
  - 引擎维护 `last_anchor_mid_price`。
  - 每次 tick 计算得到新的 `fair_value` 后：
    ```python
    price_diff = abs(fair_value - self.last_anchor_mid_price)
    if price_diff <= self.price_offset_threshold:  # 默认 0.005
        # 忽略这次 tick，避免频繁重置网格
        return
    ```
  - 只有当价格偏移超过阈值，才会执行完整的 `cancel + new grid` 流程。

- **Kill Switch 协同**
  - `on_control_message({"action": "suspend"})`：
    - 在 `_trade_lock` 内设置 `self.suspended = True`。
    - 同步等待 `cancel_all_orders()` 完成，确保没有遗留活跃挂单。
  - `{"action": "resume"}`：解除暂停，重新响应 tick。

#### 2.3 订单执行与路由：OMS & CircuitBreaker

- **ClobClient 免 Gas 签名**
  - 使用 `signature_type=2`，配合 `FUNDERS_ADDRESS` 代理签名，实现 Polymarket 通常使用的 proxy 钱包模式。
  - 通过 `create_or_derive_api_creds()` + `set_api_creds()` 为 WebSocket user stream 和 REST API 提供统一认证。

- **CircuitBreaker 状态机**
  - `failure_threshold` 默认 5，`recovery_timeout` 默认 60 秒。
  - 算法：
    - 每次包裹的 `_place_order` 或 `_cancel` 抛异常 → `record_failure()`。
    - 若失败次数 ≥ 阈值：
      - `state = OPEN`，LOG CRITICAL，后续所有请求直接被拒绝。
    - 在 `OPEN` 状态下，如果当前时间 - `last_failure_time` > `recovery_timeout`：
      - 切换为 `HALF_OPEN`，允许一次试探执行；
      - 如成功则 `reset()` 回 `CLOSED`。

- **同步调用 offload**
  - 所有 `py-clob-client` 调用通过 `asyncio.to_thread` 封装：
    - 避免同步 HTTP 阻塞事件循环（对高频 WS 监听与 Redis PubSub 至关重要）。
    - circuit breaker 外层仍保持 `await` 接口，不改变上层调用栈。

#### 2.4 风控与数据一致性：RiskMonitor & UserStream

- **FOR UPDATE 行锁使用点**
  - `UserStream.handle_fill()`：
    - `SELECT OrderJournal ... WITH FOR UPDATE` 锁订单行；
    - `SELECT InventoryLedger ... WITH FOR UPDATE` 锁库存行；
    - 在一个事务内完成订单状态更新 + 敞口更新，避免并发 fills 改写错 exposure。
  - `UserStream.handle_cancellation()`：
    - 对 `OrderJournal` 行加锁，确保不会与同步的 `cancel_order()` 产生状态竞争。
  - `RiskMonitor.reconcile_positions()`：
    - `SELECT InventoryLedger WITH FOR UPDATE` 锁住所有 ledger 行，在对账覆盖时避免与 `handle_fill()` 等并发写冲突。

- **Kill Switch 同步等待机制**
  - 风控触发 / API `/stop` / `/liquidate`：
    - 先通过 Redis 控制通道发 `suspend`，确保所有 QuotingEngine 持锁同步执行 `cancel_all_orders()`。
    - 再执行 `oms.cancel_market_orders` 做二次“扫尾”。
  - 在 `/liquidate` 中，强平下单不等待成交（由 UserStream 监听），但所有挂单取消是同步等待完成的（Kill Switch 完成后不会再有旧 grid 悬挂）。

---

### 3. 数据持久化层设计 (Data Persistence)

文件：`app/models/db_models.py`  
ORM：SQLAlchemy Declarative

#### 3.1 表结构与关系

- **`markets_meta` → `MarketMeta`**
  - `condition_id: PK`：Polymarket 市场唯一 ID。
  - `slug`：Gamma 的前端 slug，用于生成 Polymarket URL。
  - `end_date, status`：市场元信息与当前状态（active, closed, suspended 等）。
  - `yes_token_id, no_token_id`：对应 CLOB 的 YES/NO tokenId。
  - 关系：
    - `orders = relationship("OrderJournal", back_populates="market")`
    - `inventory = relationship("InventoryLedger", uselist=False)`

- **`orders_journal` → `OrderJournal`**
  - `order_id: PK`：本地订单 ID（最终会被替换为 CLOB orderID）。
  - `market_id: FK → markets_meta.condition_id`
  - `side: Enum(OrderSide)`：BUY / SELL。
  - `price: Numeric(10,4)`
  - `size: Numeric(20,4)`
  - `status: Enum(OrderStatus)`：PENDING / OPEN / FILLED / CANCELED / FAILED。
  - `payload: JSON`：存储 SDK 原始响应、filled_size 累积、status_detail、cancel_response 等。
  - `created_at: DateTime(timezone=True), server_default=func.now()`
  - `updated_at: DateTime(timezone=True), onupdate=func.now()`
  - 关系：
    - `market = relationship("MarketMeta", back_populates="orders")`

- **`inventory_ledger` → `InventoryLedger`**
  - `market_id: PK/FK → markets_meta.condition_id`
  - `yes_exposure: Numeric(20,4)`：当前 YES 仓位（以张数/份数计）。
  - `no_exposure: Numeric(20,4)`：当前 NO 仓位。
  - `realized_pnl: Numeric(20,4)`：已实现利润。
  - `updated_at: DateTime(timezone=True)`：最后更新时刻。

#### 3.2 典型查询模式

- 列出当前活动订单：
  - `SELECT * FROM orders_journal WHERE status IN ('PENDING', 'OPEN') ORDER BY created_at DESC`
- 查询某个市场风险：
  - `SELECT * FROM inventory_ledger WHERE market_id = :condition_id`
- Kill Switch:
  - `SELECT * FROM orders_journal WHERE market_id = :condition_id AND status IN ('PENDING','OPEN')`
- 对账：
  - `SELECT * FROM inventory_ledger FOR UPDATE` 全表锁定后逐行覆盖。

---

### 4. 驾驶舱控制端 (Streamlit Dashboard)

文件：`dashboard/app.py`

#### 4.1 控制面板 (Control Panel)

- 输入框：`Condition ID or Polymarket URL`
  - 支持三种输入：
    - 直接 `condition_id`（0x...）。
    - 纯 slug（`will-the-us-confirm-that-aliens-exist-before-2027`）。
    - 完整 URL（`https://polymarket.com/event/<slug>`）。
  - 辅助函数：`resolve_condition_id(market_input)`：
    - 若以 0x 开头且长度 ≥ 66 → 直接视为 condition_id。
    - 否则从 URL 提取 `/event/<slug>`，或者将整个输入当 slug。
    - 调用 `GET https://gamma-api.polymarket.com/markets?slug=<slug>` 拿到 `conditionId`。
- 安全确认：
  - 复选框：`I understand this may place real orders with current config`。
  - 未勾选则拒绝启动，防止误触发实盘下单。
- 提交后：
  - POST `/markets/{condition_id}/start` 并展示返回 JSON，完成后 `st.rerun()`。

#### 4.2 Emergency Controls

- 文本框：`Target Condition ID`
- `🛑 Stop`：
  - POST `/markets/{condition_id}/stop`，后台发布 `suspend` 控制信号并撤单。
- `☢️ Liquidate All`：
  - POST `/markets/{condition_id}/liquidate`，后台先撤单再在 0.01 价位平掉 Yes/No 仓位。

#### 4.3 Inventory & Risk

- 调用 `fetch_inventory()` 从 DB 读 `inventory_ledger`。
- 展示：
  - 顶部 KPI：Active Markets 数量、Total Realized PnL。
  - Bar Chart：按 `market_id` 展示 YES/NO exposure。
  - `Inventory Ledger` 表格：
    - 列：`market_id, yes_exposure, no_exposure, realized_pnl, updated_at, Gamma, Polymarket`。
    - Gamma 链接使用 `LinkColumn`，显示 condition_id。
    - Polymarket 链接通过 Gamma 解析 slug，显示 slug 文本。

#### 4.4 Active Orders

- `fetch_active_orders()` 读 `orders_journal` 中 `OPEN/PENDING` 订单。
- 将 `created_at` 统一转换到 `Asia/Shanghai` 时区，并生成 `created_at_local`。
- UI 表格列：
  - `order_id, market_id, side, price, size, status, created_at (Asia/Shanghai)`。
  - `price/size` 使用 `NumberColumn` 格式化，风格统一。

#### 4.5 Market Explorer (Gamma)

- `Load Top Markets` 按钮：
  - 调用 `GET https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=20`。
  - 将结果缓存到 `st.session_state["explorer_markets"]`。
- 对每个 market：
  - 使用 `st.expander(question)` 展示：
    - `conditionId, slug, volume24hr, liquidityNum`。
    - 直达前端的 Polymarket 链接。
  - 按钮「✅ Confirm & Start Quoting」：
    - 调 `POST /markets/{conditionId}/start`，一键启动该市场的双边做市。

#### 4.6 Danger Zone – 一键清除本地数据

- 表单 `WIPE`：
  - 用户必须在输入框里键入 `WIPE` 才能启用按钮 `🔥 Wipe All Data`。
  - 调用 FastAPI `/admin/wipe`：
    - 清空 `orders_journal / inventory_ledger / markets_meta`。
    - `Redis.flushdb()` 清空所有本地缓存。
  - 用于开发调试或出现严重数据污染后的「硬重置」。

---

### 5. V1.1 技术演进建议

基于当前 V1.0 的实现状态，建议下一版本重点在以下方向迭代：

1. **执行路径升级：引入 Builder Relayer 与合约级 Kill Switch**
   - 当前所有下单/撤单都走 CLOB HTTP API，缺少文档中描述的“双通道执行”：
     - V1.1 可以接入官方 `py-builder-relayer-client`，将日常 Maker 挂单统一走 Relayer，降低延迟与复杂度。
     - 保留 CLOB / 甚至合约直连路径专用于 Kill Switch / Liquidate。
   - 利用 `ALCHEMY_RPC_URL`：
     - 在极端情况下直接调用 CTF 合约进行应急撤单或仓位转移，真正实现“绕过第三方服务的链上级 Kill Switch”。

2. **智能参数与市场适配**
   - 目前 `BASE_ORDER_SIZE` 与 `GRID_LEVELS` 全局配置，未自动适配单个市场的 `orderMinSize`、流动性状况。
   - V1.1 可增加：
     - 从 `/book` 或 Gamma 的 `orderMinSize` 字段自动校正 `BASE_ORDER_SIZE`，避免反复 400：`size lower than minimum: 5`。
     - 根据市场 24h Volume / Liquidity 动态选择 Grid 密度与 spread 基准（例如高流动性市场使用更细致的 grid_levels）。
     - 将这些参数在 Dashboard 中以只读方式可见，便于调优。

3. **可观察性与报警体系**
   - 当前日志已经较为详尽，但仍缺少：
     - Prometheus / OpenTelemetry 级别的 metrics 与 tracing。
     - 自动告警通道（如当 CircuitBreaker 进入 OPEN 时推送到 PagerDuty / 钉钉 / Slack）。
   - V1.1 建议：
     - 引入 metrics 端点（如 `/metrics`），暴露：
       - 每市场发单/撤单成功率。
       - CircuitBreaker 状态与失败计数。
       - Kill Switch 触发次数与结果（成功/未完全成功）。
     - 在 RiskMonitor 中增加针对「KILL SWITCH INCOMPLETE」的主动重试与告警机制，而不是只打日志。