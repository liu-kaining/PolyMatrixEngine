# 成交处理流程

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph WS["WebSocket 事件"]
        A["User WebSocket<br/>wss://ws-subscriptions-clob.polymarket.com/ws/user"]
        B["消息类型:<br/>trade / order"]
    end

    subgraph Parse["消息解析"]
        C["process_message()<br/>JSON 解析"]
        D{"event_type?"}
        E["handle_fill()<br/>成交处理"]
        F["handle_cancellation()<br/>撤单处理"]
    end

    subgraph FillHandler["UserStreamGateway 成交处理"]
        H["DB 更新 OrderJournal<br/>filled_size, status"]
        I["直接调用<br/>inventory_state.apply_fill()"]
        J["异步队列持久化"]
    end

    subgraph InvUpdate["库存状态更新"]
        K["内存优先更新"]
        L["yes_exposure<br/>no_exposure"]
        M["capital_used<br/>realized_pnl"]
        N["pending_buy_notional<br/>释放"]
    end

    subgraph AsyncPersist["异步持久化"]
        O["有界队列<br/>maxsize=1000"]
        P{"队列满?"}
        Q["尾部丢弃<br/>(极少发生)"]
        R["异步写入<br/>InventoryLedger"]
    end

    subgraph Notify["状态通知"]
        S["Redis PubSub<br/>order_status:{cid}:{token}"]
        T["QuotingEngine 订阅<br/>清理 active_orders"]
    end

    subgraph Close["连接管理"]
        U{"连接正常?"}
        V["假死探测<br/>45s 无消息"]
        W["自愈重连<br/>重新认证"]
    end

    %% 连接
    A --> B
    B --> C
    C --> D
    D -->|trade| E
    D -->|order| F

    E --> H
    F --> H

    H --> I
    I --> K
    K --> L
    K --> M
    K --> N

    N --> O
    O --> P
    P -->|Yes| Q
    P -->|No| R
    Q --> R

    R --> S
    S --> T

    A --> U
    U -->|No| V
    V --> W
    W --> A

    %% 样式 - 专业沉稳配色
    classDef ws fill:#0891b2,stroke:#0e7490,color:#fff
    classDef handler fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef memory fill:#475569,stroke:#334155,color:#fff
    classDef persist fill:#059669,stroke:#047857,color:#fff
    classDef notify fill:#dc2626,stroke:#b91c1c,color:#fff

    class A,B ws
    class E,F,H handler
    class I,J,K,L,M,N memory
    class O,P,Q,R persist
    class S,T notify
```

## 成交处理核心代码

```python
async def handle_fill(self, order_id: str, filled_size: float, fill_price: float):
    """
    UserStreamGateway 成交处理核心逻辑
    1. DB 持久化 (同步) - 更新 OrderJournal
    2. 内存更新 (同步) - 直接调用 inventory_state.apply_fill()
    3. 异步队列持久化 - 通过 inventory_state 内部队列
    4. Redis PubSub 通知 QuotingEngine
    """
    # 1. DB 更新
    async with AsyncSessionLocal() as session:
        order = await session.get(OrderJournal, order_id)
        # ... 更新 filled_size, status
        await session.commit()
        market_id = order.market_id
        side = order.side.value
        token_id = payload.get("token_id")

    # 2. 内存优先更新 (直接调用，不是通过 OMS)
    tokens = await self._resolve_market_tokens(session, market_id)
    if tokens and token_id:
        is_yes = token_id == tokens["yes_token_id"]
        updated = await inventory_state.apply_fill(
            market_id=market_id,
            is_yes=is_yes,
            side=side,
            filled_size=filled_size,
            fill_price=fill_price,
        )

    # 3. Redis PubSub 通知 QuotingEngine 清理 active_orders
    await self._publish_order_status_event(market_id, token_id, order_id, "FILLED")
```

## 内存优先设计

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart LR
    subgraph Traditional["传统方案"]
        A1["DB 写入"]
        A2["返回成功"]
        A3["更新内存"]
        A1 --> A2 --> A3
    end

    subgraph PolyMatrix["内存优先方案"]
        B1["内存更新"]
        B2["返回成功"]
        B3["异步队列"]
        B4["DB 持久化"]
        B1 --> B2 --> B3 --> B4
    end

    classDef traditional fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef good fill:#059669,stroke:#047857,color:#fff

    class A1,A2,A3 traditional
    class B1,B2,B3,B4 good
```

## 异步持久化队列

```python
class InventoryStateManager:
    def __init__(self):
        self._persist_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def apply_fill(self, ...):
        """同步: 内存更新"""
        # 更新内存状态
        self.yes_exposure += size  # BUY YES
        self.capital_used[token_id] += size * price

        # 异步持久化入队
        try:
            self._persist_queue.put_nowait({
                "action": "fill",
                "condition_id": condition_id,
                "token_id": token_id,
                "side": side,
                "size": size,
                "price": price,
                "timestamp": now()
            })
        except asyncio.QueueFull:
            logger.warning("Persist queue full, dropping tail")

    async def _persist_drain_loop(self):
        """后台持久化循环"""
        while not self._shutdown:
            try:
                batch = []
                # 批量获取 (最多 100 条或超时 1s)
                for _ in range(100):
                    try:
                        item = self._persist_queue.get_nowait()
                        batch.append(item)
                    except asyncio.QueueEmpty:
                        break

                if batch:
                    await self._batch_persist(batch)

                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Persist error: {e}")
```

## 自愈重连机制

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
sequenceDiagram
    participant WS as UserStreamGateway
    participant Auth as HMAC Auth
    participant Inv as InventoryState

    Note over WS: 连接建立 (30s ping)

    loop 假死探测
        WS->>WS: 30s 无消息?
        WS->>WS: 触发重连
    end

    WS->>Auth: 重新生成 HMAC 签名
    Auth-->>WS: 签名票据

    WS->>WS: 重连 WebSocket

    alt 需要恢复状态
        WS->>Inv: 查询当前持仓
        Inv-->>WS: InventorySnapshot
        WS->>WS: 同步 active_orders
    end

    Note over WS: 恢复正常处理
```

---

*设计亮点: 内存优先保证热路径零延迟，有界队列保证内存安全，关闭排空保证不丢数据*
