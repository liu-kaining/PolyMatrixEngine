# 数据库实体关系图（与 `app/models/db_models.py` 一致）

当前 ORM **仅包含三张业务表**：`markets_meta`、`orders_journal`、`inventory_ledger`。激励字段挂在 **`MarketMeta`** 上，**无**独立的 `rewards_config` 表；**无** `funding_address` 表（资金地址来自配置 `FUNDER_ADDRESS`）。

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
erDiagram
    MARKET_META ||--o| INVENTORY_LEDGER : "one ledger row per market"
    MARKET_META ||--o{ ORDER_JOURNAL : "journal entries"

    MARKET_META {
        string condition_id PK
        string slug UK
        datetime end_date
        string status
        string yes_token_id
        string no_token_id
        numeric rewards_min_size
        numeric rewards_max_spread
        numeric reward_rate_per_day
    }

    INVENTORY_LEDGER {
        string market_id PK, FK
        numeric yes_exposure
        numeric no_exposure
        numeric yes_capital_used
        numeric no_capital_used
        numeric realized_pnl
        datetime updated_at
    }

    ORDER_JOURNAL {
        string order_id PK
        string market_id FK
        string side
        numeric price
        numeric size
        string status
        string payload
        datetime created_at
        datetime updated_at
    }
```

## 表关系说明（flowchart）

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph MM["markets_meta"]
        M1["condition_id PK"]
        M2["yes_token_id / no_token_id"]
        M3["rewards_* 激励字段"]
    end

    subgraph IL["inventory_ledger"]
        I1["market_id PK = condition_id"]
        I2["yes/no_exposure capital_used"]
    end

    subgraph OJ["orders_journal"]
        O1["order_id PK"]
        O2["market_id FK"]
        O3["payload 含 token_id 等"]
    end

    M1 -->|"1:1"| I1
    M1 -->|"1:N"| O2

    style MM fill:#0891b2,stroke:#0e7490,color:#fff
    style IL fill:#7c3aed,stroke:#6d28d9,color:#fff
    style OJ fill:#dc2626,stroke:#b91c1c,color:#fff
```

## 说明

- **`InventoryLedger` 主键**仅为 `market_id`（对应 `condition_id`），不按钱包地址分表。
- **成交与库存**：User WS `handle_fill` 更新 `OrderJournal` 与内存 `inventory_state`，再异步刷写 `inventory_ledger`；与下图示意的 SQL 仅为概念参考，**以 ORM 字段为准**。

## 库存计算口径（概念 SQL，非必须与实际列名一一对应）

```sql
-- 示例：按市场 join 元数据做 MTM（定价来自引擎/Redis，非本表持久化）
SELECT
    il.market_id,
    il.yes_exposure,
    il.no_exposure,
    il.yes_capital_used + il.no_capital_used AS total_capital_used
FROM inventory_ledger il
JOIN markets_meta mm ON il.market_id = mm.condition_id;
```

## 索引设计（建议）

```sql
CREATE INDEX IF NOT EXISTS idx_orders_journal_market_id ON orders_journal(market_id);
CREATE INDEX IF NOT EXISTS idx_orders_journal_status ON orders_journal(status);
CREATE INDEX IF NOT EXISTS idx_orders_journal_created_at ON orders_journal(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_markets_meta_status ON markets_meta(status);
```

## 异步持久化队列（InventoryStateManager）

内存单例 + **有界队列**异步写入 `inventory_ledger`，与 `app/core/inventory_state.py` 一致；热路径 `on_tick` **不读**该表。

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Step1["1. OrderJournal"]
        A["User WS handle_fill<br/>with_for_update 订单行"]
    end

    subgraph Step2["2. 内存"]
        B["inventory_state.apply_fill"]
    end

    subgraph Step3["3. 异步队列"]
        C["有界队列 maxsize=1000"]
    end

    subgraph Step4["4. 批量落库"]
        D["inventory_ledger 行更新"]
    end

    A --> B --> C --> D

    style A fill:#0891b2,stroke:#0e7490,color:#fff
    style B fill:#475569,stroke:#334155,color:#fff
    style C fill:#7c3aed,stroke:#6d28d9,color:#fff
    style D fill:#059669,stroke:#047857,color:#fff
```

---

*与实现文件对齐：`app/models/db_models.py`、`app/core/inventory_state.py`。*
