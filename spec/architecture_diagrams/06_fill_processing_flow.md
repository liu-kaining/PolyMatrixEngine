# 当前成交处理流程

```mermaid
sequenceDiagram
    participant WS as User WebSocket
    participant Own as Local order ownership filter
    participant Inbox as fill_events
    participant DB as Postgres transaction
    participant Cash as fill_cash_ledger
    participant Inv as InventoryLedger
    participant Mem as InventoryStateManager

    WS->>Own: trade candidate (maker/taker rows)
    Own->>Own: local/exchange order id lookup + bounded race retry
    alt 非本地订单
        Own-->>WS: ignore counterparty row
    else 本地订单
        Own->>Inbox: deterministic event_id, insert RECEIVED
        Inbox->>DB: lock fill/order/reservation/inventory
        DB->>DB: validate token, side, price, cumulative size
        DB->>DB: decrement BUY/SELL reservation
        DB->>Cash: signed gross cash + explicit fee or UNKNOWN
        DB->>Inv: fee-aware average-cost accounting
        DB->>DB: increment state_version; mark fill PROCESSED
        DB-->>Inbox: atomic commit
        Inbox->>Mem: apply exact committed state_version snapshot
    end
```

## 不变量

- `event_id` 对同一 exchange trade/order/role 是确定性的；重复消息不会重复记账。
- User WS payload 中的对手方 maker/taker row 必须先证明属于本地 OrderJournal。
- fill、reservation、cash、inventory、order 与 inbox 状态在同一事务内提交。
- BUY fill 不得高于订单 limit；累计 fill 不得超过原始订单 size；SELL 不得超过已知持仓。
- 明确 BUY fee 进入成本，明确 SELL fee 从收入扣除；不明确的 fee 保留 `UNKNOWN`，不按 0。
- 每个成功 fill 记录其 `accounting_state_version`，启动/管理端可从零重放并核对最终账本。
- 数据库提交成功后才更新内存；内存拒绝旧 `state_version` 覆盖新状态。
- `UNMAPPED`、`FAILED`、缺 cash、版本断档或 replay mismatch 都会阻止会计 readiness。

## 断线边界

User WS 重连只触发 Data API 仓位比较，并继续遵守 recent-fill delay guard。仓位 API 不能恢复成交顺序和手续费，因此差异会使会计降级并 Halt；完整恢复仍依赖后续认证 trade-history backfill。
