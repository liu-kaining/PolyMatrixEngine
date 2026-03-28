# 成交处理流程

```mermaid
flowchart TB
    subgraph WS["WebSocket 事件"]
        A["User WebSocket<br/>polymarket.com/ws"]
        B["消息类型:<br/>▸ fill<br/>▸ cancel<br/>▸ cancel_all"]
    end

    subgraph Parse["消息解析"]
        C["process_message()<br/>JSON 解析"]
        D{"消息类型?"}
        E["handle_fill()<br/>成交处理"]
        F["handle_cancel()<br/>撤单处理"]
        G["handle_cancel_all()<br/>全撤处理"]
    end

    subgraph FillHandler["成交处理器 💎"]
        H["OMS update_order()<br/>更新 OrderJournal"]
        I["filled_size += amount<br/>status → FILLED"]
        J["apply_fill()<br/>InventoryStateManager"]
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
        T["QuotingEngine<br/>清理 active_orders"]
    end

    subgraph Close["连接管理"]
        U{"连接正常?"}
        V["假死探测<br/>30s 无消息"]
        W["自愈重连<br/>重新认证"]
    end

    %% 连接
    A --> B
    B --> C
    C --> D
    D -->|fill| E
    D -->|cancel| F
    D -->|cancel_all| G

    E --> H
    F --> H
    G --> H

    H --> I
    I --> J

    J --> K
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

    %% 样式
    classDef ws fill:#667eea,color:#fff
    classDef handler fill:#4ecdc4,stroke:#333
    classDef memory fill:#ffe66d,stroke:#333
    classDef persist fill:#95e1d3,stroke:#333
    classDef notify fill:#ff6b6b,color:#fff

    class A,B ws
    class E,F,G,H,I handler
    class J,K,L,M,N memory
    class O,P,Q,R persist
    class S,T notify
```

## 成交处理核心代码

```python
async def handle_fill(
    self,
    order_id: str,
    filled_size: float,
    fill_price: float,
    condition_id: str,
    token_id: str
):
    """
    成交处理核心逻辑
    1. DB 持久化 (同步)
    2. 内存更新 (同步)
    3. 异步队列持久化
    """
    # 1. DB 更新
    async with get_db_session() as session:
        order = await session.get(OrderJournal, order_id)
        order.filled_size += filled_size
        order.status = OrderStatus.FILLED if is_full_fill else OrderStatus.PARTIALLY_FILLED
        await session.commit()

    # 2. 内存优先更新
    await self.inventory_state.apply_fill(
        condition_id=condition_id,
        token_id=token_id,
        side="BUY" if is_buy else "SELL",
        size=filled_size,
        price=fill_price
    )

    # 3. 通知引擎清理
    await self.redis.publish(
        f"order_status:{condition_id}:{token_id}",
        {"order_id": order_id, "status": "filled"}
    )
```

## 内存优先设计

```
┌─────────────────────────────────────────────────────────────────────┐
│                         内存优先 vs 传统方案                          │
├─────────────────────────────────────────────────────────────────────┤
│  传统方案:                                                           │
│  DB 写入 ──→ 返回成功 ──→ 更新内存                                   │
│  ❌ DB 延迟影响成交处理                                               │
│  ❌ DB 故障导致成交丢失                                               │
│  ❌ 热路径 DB 瓶颈                                                    │
├─────────────────────────────────────────────────────────────────────┤
│  内存优先方案 (PolyMatrix):                                           │
│  内存更新 ──→ 返回成功 ──→ 异步队列 ──→ DB                             │
│  ✅ 成交处理零延迟                                                    │
│  ✅ 内存状态始终最新                                                  │
│  ✅ 有界队列 + 关闭排空保证持久化                                     │
└─────────────────────────────────────────────────────────────────────┘
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
