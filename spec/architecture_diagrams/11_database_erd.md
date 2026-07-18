# 数据库实体关系图（迁移 009）

```mermaid
erDiagram
    MARKET_META ||--o| INVENTORY_LEDGER : owns
    MARKET_META ||--o{ ORDER_JOURNAL : contains
    MARKET_META ||--o{ FILL_EVENT : maps
    MARKET_META ||--o{ RISK_RESERVATION : limits
    ORDER_JOURNAL ||--o{ FILL_EVENT : receives
    ORDER_JOURNAL ||--o| RISK_RESERVATION : reserves
    FILL_EVENT ||--|| FILL_CASH_LEDGER : cash_fact
    ORDER_RECONCILIATION_RUN ||--o{ EXCHANGE_ORDER_SNAPSHOT : captures

    MARKET_META {
        string condition_id PK
        string yes_token_id
        string no_token_id
        string status
        numeric rewards_min_size
        numeric rewards_max_spread
    }
    ORDER_JOURNAL {
        string order_id PK
        string exchange_order_id UK
        string reservation_id UK
        string market_id FK
        enum side
        numeric price
        numeric size
        enum status
        json payload
    }
    FILL_EVENT {
        string event_id PK
        string exchange_order_id
        string local_order_id FK
        string market_id FK
        string token_id
        string side
        numeric price
        numeric size
        string status
        int accounting_state_version
        json payload
    }
    FILL_CASH_LEDGER {
        string event_id PK_FK
        string market_id FK
        string side
        numeric gross_cash_delta
        numeric fee_amount
        numeric net_cash_delta
        string fee_status
    }
    INVENTORY_LEDGER {
        string market_id PK_FK
        numeric yes_exposure
        numeric no_exposure
        numeric yes_capital_used
        numeric no_capital_used
        numeric realized_pnl
        string accounting_version
        int state_version
    }
    RISK_RESERVATION {
        string reservation_id PK
        string client_order_id UK
        string exchange_order_id
        string market_id FK
        string token_id
        string side
        numeric limit_price
        numeric original_size
        numeric remaining_size
        numeric reserved_notional
        string status
    }
    PORTFOLIO_RISK_STATE {
        string wallet_id PK
        numeric reserved_buy_notional
        int state_version
    }
    ORDER_RECONCILIATION_RUN {
        string run_id PK
        string status
        int blocker_count
        json summary
    }
    EXCHANGE_ORDER_SNAPSHOT {
        string run_id PK_FK
        string exchange_order_id PK
        string source
        string status
        json payload
    }
    ACCOUNTING_AUDIT_RUN {
        string run_id PK
        string status
        int inventory_count
        int fill_count
        int blocker_count
        json summary
    }
```

## 事务边界

- 下单前：`portfolio_risk_state` 行锁串行化组合准入，随后插入 `risk_reservations` 与 `orders_journal`。
- 成交：锁定 fill/order/reservation/inventory；同事务写 `fill_cash_ledger`、库存成本/PnL、订单累计成交和 fill 终态。
- 订单对账：保存 run/snapshot 后，只在 exchange terminal、local fills 与 reservation 一致时释放剩余额度。
- 会计审计：按 `fill_events.accounting_state_version` 重放，核对 `inventory_ledger`；结果写 `accounting_audit_runs`。

`inventory_state` 只是在报价热路径提供已提交版本的内存快照，不是 durable 事实源。
