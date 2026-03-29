# 系统整体架构图

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b',
  'secondaryColor': '#0891b2',
  'tertiaryColor': '#f1f5f9'
}}%%
flowchart TB
    subgraph Client["客户端层"]
        Dashboard["Streamlit 驾驶舱<br/>监控 + 控制"]
        API["FastAPI 控制面<br/>启动/停止/风控"]
    end

    subgraph DataPlane["数据面"]
        subgraph MarketData["市场数据层"]
            MarketWS["Market WebSocket<br/>Polymarket 订单簿"]
            REST["REST API<br/>快照获取"]
            Gamma["Gamma API<br/>市场元数据"]
        end

        subgraph DataBus["Redis 消息总线"]
            tick["tick:{token}<br/>订单簿更新"]
            ob["ob:{token}<br/>完整快照"]
            control["control:{cid}<br/>控制指令"]
            order_status["order_status:<br/>订单状态"]
        end

        MarketWS --> tick
        MarketWS --> ob
    end

    subgraph Core["核心引擎层"]
        subgraph QuotingEngine["QuotingEngine × 2<br/>YES + NO (内部组件)"]
            AlphaModel["AlphaModel<br/>内部组件: FV 计算"]
            Grid["GridGenerator<br/>内部组件: 网格生成"]
            DiffQuote["sync_orders_diff()<br/>内部方法: 差分报价"]
        end

        subgraph RiskPlane["风控平面"]
            Watchdog["RiskMonitor<br/>Watchdog"]
            KillSwitch["Kill Switch<br/>硬熔断"]
            Reconciler["对账引擎<br/>Data API 全表对账<br/>间隔见 RECONCILIATION_INTERVAL_SEC"]
        end

        subgraph Inventory["库存管理层"]
            InvState["InventoryStateManager<br/>内存优先 + 异步持久化"]
        end
    end

    subgraph ExecutionPlane["执行平面"]
        OMS["OMS<br/>订单状态机"]
        CircuitBreaker["CircuitBreaker<br/>熔断器"]
        CLOB["Polymarket CLOB<br/>py-clob-client"]
        Builder["Builder API<br/>订单归因"]
    end

    subgraph DataLayer["数据层"]
        PostgreSQL["PostgreSQL<br/>InventoryLedger<br/>OrderJournal<br/>MarketMeta"]
        RedisKV["Redis KV<br/>状态缓存"]
    end

    subgraph AutoRouter["自动路由"]
        Router["PortfolioManager<br/>组合管理器"]
        Scorer["评分引擎<br/>ROI × 流动性 × 时间"]
        Rebalancer["重平衡器<br/>赛道隔离"]
    end

    %% 连接关系
    Dashboard --> API
    API -->|"生命周期管理"| Core
    API -->|"market_lifecycle"| AutoRouter

    tick --> QuotingEngine
    ob --> QuotingEngine
    Gamma -->|"rewards config"| QuotingEngine

    QuotingEngine -.->|"直接调用<br/>oms.create_order()"| OMS
    QuotingEngine -.->|"直接调用<br/>oms.cancel_order()"| OMS
    OMS -->|place/cancel| CLOB
    OMS -->|Builder签名| Builder

    Watchdog -->|check_exposure<br/>(每秒)| InvState
    Watchdog -.->|trigger_kill_switch| KillSwitch
    Watchdog -.->|reconcile_loop<br/>(周期)| Reconciler
    KillSwitch -.->|cancel_all| OMS
    Reconciler -->|Polymarket<br/>Data API| PostgreSQL

    InvState -->|async_persist| PostgreSQL

    AutoRouter -->|start_market_making| Core
    Router -->|Gamma查询| Gamma

    %% 样式定义 - 专业沉稳配色
    classDef highlight fill:#1e3a5f,stroke:#334155,stroke-width:2px,color:#fff
    classDef engine fill:#0891b2,stroke:#0e7490,stroke-width:2px,color:#fff
    classDef risk fill:#dc2626,stroke:#b91c1c,stroke-width:2px,color:#fff
    classDef data fill:#059669,stroke:#047857,stroke-width:2px,color:#fff
    classDef execution fill:#7c3aed,stroke:#6d28d9,stroke-width:2px,color:#fff
    classDef storage fill:#475569,stroke:#334155,stroke-width:2px,color:#fff
    classDef auto fill:#d97706,stroke:#b45309,stroke-width:2px,color:#fff
    classDef internalComp fill:#065f7f,stroke:#0e7490,stroke-width:1px,color:#fff

    class QuotingEngine engine
    class AlphaModel,Grid,DiffQuote internalComp
    class Watchdog,KillSwitch,Reconciler risk
    class InvState,PostgreSQL,RedisKV data
    class OMS,CircuitBreaker,CLOB,Builder execution
    class MarketWS,REST,Gamma,tick,ob,control,order_status storage
    class Router,Scorer,Rebalancer auto
```

## 架构说明

### 三层分离设计

| 层次 | 职责 | 关键技术 |
|------|------|----------|
| **数据面** | WebSocket 订阅、REST 快照、消息分发 | `redis.asyncio` Pub/Sub |
| **核心引擎层** | 定价、报价、风控、库存 | asyncio 热路径零 DB |
| **执行平面** | OMS 状态机、CLOB 交互、签名 | py-clob-client |

### 关键设计原则

1. **内存优先**: 热路径完全无 DB 读取
2. **消息解耦**: 所有模块通过 Redis Pub/Sub 通信
3. **状态分离**: 控制面(FastAPI) 与 数据面(Engine) 解耦
4. **异步持久化**: 成交 → 内存更新 → 异步队列 → DB

> **图注**：`apply_fill()` 由 User WebSocket 成交路径调用 `InventoryStateManager`，并非 InvState 自指；故图中不单独画「自环」边。

---

*设计亮点: 准机构级架构，热点路径完全内存化，零 DB 阻塞*
