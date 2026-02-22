# PolyMatrix Engine 阶段性架构与实现状态报告

## 1. 资产与状态流转路径 (Data Flow)

本阶段已实现从外部 API 注册到策略生成、OMS 模拟落库的完整异步数据流闭环。流转路径如下：

1.  **市场注册与资产映射**：
    *   外部调用 `app/main.py` 的 `POST /markets/{condition_id}/start` 接口。
    *   通过 `app/market_data/gamma_client.py` 中的 `GammaAPIClient.get_market_tokens_by_condition_id` 访问 Polymarket Gamma API，解析 `clobTokenIds` 提取出 `yes_token_id` 和 `no_token_id`。
    *   通过 `AsyncSession` 将映射关系持久化至 `markets_meta` 表中。
    *   分别拉起两个以 `token_id` 为核心的 `QuotingEngine` (YES/NO 独立引擎) 协程，加入 `background_tasks`。
2.  **行情订阅与分发**：
    *   接口调用 `md_gateway.subscribe([yes_token_id, no_token_id])`。
    *   `MarketDataGateway` (`app/market_data/gateway.py`) 向 Polymarket WebSocket 下发订阅。
    *   接收到 `book` 或 `price_change` 后，`OrderbookParser` 进行 JSON 解析，生成买卖前 5 档快照。
    *   利用 Redis 进行双写：使用 `set_state` (缓存最新盘口) 并通过 `redis_client.publish(f"tick:{asset_id}", update)` 向订阅该 `token_id` 的策略引擎广播。
3.  **策略决策 (Quoting Engine)**：
    *   `QuotingEngine.run` (`app/quoting/engine.py`) 监听 Redis Pub/Sub 的 `tick:{token_id}` 频道。
    *   触发 `on_tick()` 计算中间价 (Mid-Price)。经历防抖拦截后，生成包含 `side`, `price`, `size` 及准确 `token_id` 的 Payload。
    *   并行调用 `oms.cancel_order`（清理旧网格）和 `oms.create_order`（铺设新网格）。
4.  **状态机与模拟落库 (OMS Core)**：
    *   `OrderManagementSystem.create_order` (`app/oms/core.py`) 开启新的 `AsyncSessionLocal`。
    *   首先向 PostgreSQL `orders_journal` 插入 `status=PENDING` 的记录。
    *   非阻塞 `await asyncio.sleep(0.5)` 模拟网络请求。
    *   90% 概率将状态原子级更新为 `OPEN`，10% 更新为 `FAILED`，最后由 `session.commit()` 落盘。

---

## 2. 核心机制实现核验 (Feature Verification)

### 2.1 Token ID 映射
*   **获取节点**：在 FastAPI 的 `/markets/{condition_id}/start` 路由中，如果数据库中未查到完整的 Token 映射，则会调用 `gamma_client.get_market_tokens_by_condition_id`。
*   **缓存与持久化**：不依赖 Redis 缓存，而是直接作为 `yes_token_id` 和 `no_token_id` 两个字段写入并持久化至 PostgreSQL 的 `markets_meta` 表中。
*   **OMS Payload 验证**：在 `app/quoting/engine.py` 的 `on_tick` 方法中构建的 `orders_payload`，明确包含了实例化时传入的 `self.token_id`。随后传递给 `oms.create_order` 并以 `{"token_id": token_id}` 的形式完整存入 `orders_journal` 表的 JSONB 字段 `payload` 中，映射**精准无误**。

### 2.2 防抖与阈值控制 (Debounce & Threshold)
*   **阈值设定**：目前在 `QuotingEngine.__init__` 中设定 `self.price_offset_threshold = 0.005`。
*   **防抖逻辑**：在 `on_tick` 中，通过 `abs(mid_price - self.last_anchor_mid_price)` 与阈值比对。如果未突破阈值，引擎记录 `logger.debug` 后直接执行 `return`。
*   **事件循环安全性**：由于整个 `on_tick` 是由 `async for message in pubsub.listen():` 触发的非阻塞协程，`return` 会立即让出控制权 (Yield Control) 给 FastAPI/Event Loop，不会造成计算堆积或内存泄漏，保证了高频 Tick 下的处理能力。

### 2.3 状态机持久化 (State Machine)
*   **异步高并发安全**：OMS 中每次调用 `create_order` 均使用 `async with AsyncSessionLocal() as session:`，这保证了每次下单都是从 SQLAlchemy 连接池中获取独立的连接并在 `with` 块结束时自动归还释放。
*   **流转逻辑校验**：
    *   插入阶段：`order_id`、`market_id`、`status=OrderStatus.PENDING` 成功建立并 `await session.commit()`。
    *   更新阶段：使用 `await session.get(OrderJournal, order_id)` 重新获取当前事务中的实体（保证长 await sleep 后拿到的不是陈旧对象），修改为 `OPEN` 或 `FAILED` 后再次 `commit()`。模拟的延迟与 10% 随机失败机制运作正常。

---

## 3. 系统隐患与技术债 (Technical Debt & Bottlenecks) [已在 Phase 1.5 修复]

站在高可用交易系统的维度，前期代码库曾存在以下隐患（**已在最新架构重构中彻底解决**）：

1.  **✅ Redis 连接池/生命周期管理问题 (Fixed)**：
    *   *之前*：在 `QuotingEngine.run` 中未在引擎关闭时明确调用 `.close()` 释放订阅。
    *   *修复*：增加了 `try...finally` 和 `asyncio.CancelledError` 捕获，在守护协程关闭时强制执行 `await pubsub.unsubscribe()` 与 `await pubsub.close()`，彻底杜绝了长期运行可能导致的连接耗尽 (Connection Leak)。
2.  **✅ OMS 状态竞争与 Session 隔离隐患 (Fixed)**：
    *   *之前*：`await asyncio.sleep(0.5)` 等耗时网络 I/O 处于 DB `AsyncSession` 上下文锁内。
    *   *修复*：采用“两段式”操作。Session 1 极速写入 `PENDING` 并释放连接；在无数据库锁的状态下发起 API I/O 阻塞调用；回执后使用 Session 2 重取订单并更新为 `OPEN/FAILED`。这使得 Postgres 能够承载极高并发而无需担心连接池枯竭。
3.  **✅ QuotingEngine 并发取消与竞态条件 (Fixed)**：
    *   *之前*：高频 Tick 打断可能导致新老网格计算混合，造成丢失跟踪的“孤儿订单 (Orphan Orders)”。
    *   *修复*：引入了核心的业务级异步锁 `self._trade_lock = asyncio.Lock()`。在判定价格阈值突破后的所有动作——撤单、计算、重新铺网格，被收敛于绝对原子的互斥闭环内，高频跳动的 Tick 只能等待或被排队后的防抖逻辑平滑丢弃。
4.  **异常捕获不够优雅**：
    *   `app/market_data/gateway.py` 里的 JSON 解析或 WS 读取一旦抛出未预期异常，虽然有 `logger.error` 捕获，但缺乏重试补偿机制，可能会导致 Parser 内部状态机（如 Top 5 缓存）陷入脏数据。

## 4. 下一步演进建议 (Phase 2 Focus)

1.  **实盘 OMS 执行闭环**：
    填充真实的 API 密钥与 `Funder_Address`，去掉 `sleep`，测试 Polymarket 的 Rate Limiting 并观察 Circuit Breaker 的真实阻断能力。
2.  **行情网关状态自愈**：
    为 `OrderbookParser` 增加异常情况下的全量快照重抓取逻辑（Fallback to sync full orderbook API），以防 WebSocket 数据断层导致的本地薄记错位。
3.  **用户私有频道订单回执监听**：
    打通 `ws/user` WebSocket，实现真正基于 Fill 报文更新 `inventory_ledger` (持仓/PnL) 的流转。