# 数据库实体关系图

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
erDiagram
    MARKET_META ||--o{ INVENTORY_LEDGER : "1:N"
    MARKET_META ||--o{ ORDER_JOURNAL : "1:N"
    MARKET_META ||--o{ REWARDS_CONFIG : "1:N"
    INVENTORY_LEDGER }o--|| FUNDING_ADDRESS : "N:1"

    MARKET_META {
        string condition_id PK "条件ID (Polymarket)"
        string token_id_yes "YES 代币 ID"
        string token_id_no "NO 代币 ID"
        string market_question "市场问题"
        string status "状态: pending/active/suspended/completed"
        timestamp start_time "开始时间"
        timestamp end_date "结算时间"
        float liquidity "流动性"
        jsonb tags "标签数组"
        timestamp created_at "创建时间"
        timestamp updated_at "更新时间"
    }

    INVENTORY_LEDGER {
        string condition_id PK,FK "条件ID"
        string funding_address PK "钱包地址"
        float yes_exposure "YES 持仓份额"
        float no_exposure "NO 持仓份额"
        decimal yes_capital_used "YES 占用资金(USD)"
        decimal no_capital_used "NO 占用资金(USD)"
        decimal realized_pnl "已实现盈亏"
        timestamp last_reconcile_at "上次对账时间"
        timestamp updated_at "更新时间"
    }

    ORDER_JOURNAL {
        string order_id PK "订单ID (Polymarket)"
        string condition_id FK "条件ID"
        string token_id "代币ID"
        string side "方向: BUY/SELL"
        decimal price "价格"
        decimal size "数量"
        decimal filled_size "成交数量"
        string status "状态: pending/open/filled/partial/cancelled"
        string order_type "类型: GTC/IOC/FOK"
        string signature "订单签名"
        timestamp created_at "创建时间"
        timestamp updated_at "更新时间"
    }

    REWARDS_CONFIG {
        string condition_id PK,FK "条件ID"
        decimal annual_roi "年化收益"
        decimal rate "利率"
        decimal min_size "最小订单 size"
        decimal spread "价差"
        boolean active "是否激活"
        timestamp fetched_at "获取时间"
    }

    FUNDING_ADDRESS {
        string address PK "钱包地址"
        string api_key "API Key"
        string api_secret "API Secret"
        boolean is_builder "是否使用 Builder API"
        timestamp created_at "创建时间"
    }
```

## 表关系说明

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph MARKET_META["MARKET_META (市场元数据)"]
        M1["condition_id (PK)"]
        M2["token_id_yes / token_id_no"]
        M3["status / end_date"]
    end

    subgraph INVENTORY_LEDGER["INVENTORY_LEDGER (库存台账)"]
        I1["condition_id (PK,FK)"]
        I2["funding_address (PK)"]
        I3["yes/no_exposure"]
        I4["yes/no_capital_used"]
    end

    subgraph ORDER_JOURNAL["ORDER_JOURNAL (订单日志)"]
        O1["order_id (PK)"]
        O2["condition_id (FK)"]
        O3["side / price / size"]
        O4["filled_size / status"]
    end

    subgraph REWARDS_CONFIG["REWARDS_CONFIG (激励配置)"]
        R1["condition_id (PK,FK)"]
        R2["annual_roi / rate"]
    end

    subgraph FUNDING_ADDRESS["FUNDING_ADDRESS (钱包)"]
        F1["address (PK)"]
        F2["api_key / api_secret"]
    end

    MARKET_META -->|"1:N"| INVENTORY_LEDGER
    MARKET_META -->|"1:N"| ORDER_JOURNAL
    MARKET_META -->|"1:N"| REWARDS_CONFIG
    INVENTORY_LEDGER -->|"N:1"| FUNDING_ADDRESS

    classDef market fill:#0891b2,stroke:#0e7490,color:#fff
    classDef ledger fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef order fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef rewards fill:#d97706,stroke:#b45309,color:#fff
    classDef funding fill:#059669,stroke:#047857,color:#fff

    class MARKET_META,market
    class INVENTORY_LEDGER,ledger
    class ORDER_JOURNAL,order
    class REWARDS_CONFIG,rewards
    class FUNDING_ADDRESS,funding
```

## 库存计算口径

```sql
-- 实时库存计算
SELECT
    il.condition_id,
    il.funding_address,
    il.yes_exposure,
    il.no_exposure,
    il.yes_capital_used + il.no_capital_used AS total_capital_used,
    -- MTM 盯市价值
    il.yes_exposure * mm.current_fv_yes +
    il.no_exposure * mm.current_fv_no AS mtm_value,
    -- 未实现盈亏
    il.yes_exposure * (mm.current_fv_yes - mm.entry_price_yes) +
    il.no_exposure * (mm.current_fv_no - mm.entry_price_no) AS unrealized_pnl
FROM inventory_ledger il
JOIN market_meta mm ON il.condition_id = mm.condition_id
WHERE il.funding_address = :address;
```

## 索引设计

```sql
-- 核心查询索引
CREATE INDEX idx_order_journal_condition_id ON order_journal(condition_id);
CREATE INDEX idx_order_journal_status ON order_journal(status);
CREATE INDEX idx_order_journal_created_at ON order_journal(created_at DESC);
CREATE INDEX idx_inventory_ledger_address ON inventory_ledger(funding_address);
CREATE INDEX idx_market_meta_status ON market_meta(status);
CREATE INDEX idx_market_meta_end_date ON market_meta(end_date);
```

## 异步持久化队列

```python
# InventoryStateManager 有界队列
class InventoryStateManager:
    _persist_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def apply_fill(self, ...):
        # 1. 内存更新 (同步, 零延迟)
        self.yes_exposure += size
        self.capital_used += size * price

        # 2. 异步持久化 (不阻塞热路径)
        try:
            self._persist_queue.put_nowait({
                "action": "fill",
                "condition_id": condition_id,
                "size": size,
                "price": price,
                "timestamp": now()
            })
        except asyncio.QueueFull:
            logger.warning("Persist queue full")

    async def _persist_drain_loop(self):
        """后台持久化循环 (批次写入)"""
        while not self._shutdown:
            batch = []
            for _ in range(100):
                try:
                    item = self._persist_queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break

            if batch:
                await self._batch_persist(batch)

            await asyncio.sleep(1)
```

## 状态同步流程

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Step1["1. DB 同步写入"]
        A["OrderJournal<br/>同步, 保证持久化"]
    end

    subgraph Step2["2. 内存更新"]
        B["InventoryStateManager<br/>同步, 热路径"]
    end

    subgraph Step3["3. 异步队列"]
        C["有界队列<br/>maxsize=1000"]
    end

    subgraph Step4["4. 批量 DB 写入"]
        D["InventoryLedger<br/>异步, 批量"]
    end

    A --> B --> C --> D

    classDef step fill:#0891b2,stroke:#0e7490,color:#fff
    class A,B,C,D step
```

---

*设计亮点: 内存优先 + 异步批量持久化，热路径零 DB，保证高性能同时不丢数据*
