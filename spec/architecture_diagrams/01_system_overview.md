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
        subgraph QuotingEngine["QuotingEngine × 2<br/>YES + NO"]
            AlphaModel["AlphaModel<br/>统一定价 Oracle"]
            Grid["网格生成器<br/>动态 Spread"]
            DiffQuote["差分报价器<br/>抗干扰"]
        end

        subgraph RiskPlane["风控平面"]
            Watchdog["RiskMonitor<br/>Watchdog"]
            KillSwitch["Kill Switch<br/>硬熔断"]
            Reconciler["对账引擎<br/>REST 校验"]
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

    QuotingEngine --> OMS
    OMS -->|place/cancel| CLOB
    OMS -->|Builder签名| Builder

    Watchdog -->|check_exposure| InvState
    Watchdog -->|kill_switch| KillSwitch
    Watchdog -->|reconcile| Reconciler
    Reconciler -->|Polymarket<br/>Data API| PostgreSQL

    InvState -->|apply_fill| Inventory
    InvState -->|async_persist| PostgreSQL

    AutoRouter -->|start/stop<br/>market| Core
    Router -->|filter| Gamma

    %% 样式定义 - 专业沉稳配色
    classDef highlight fill:#1e3a5f,stroke:#334155,stroke-width:2px,color:#fff
    classDef engine fill:#0891b2,stroke:#0e7490,stroke-width:2px,color:#fff
    classDef risk fill:#dc2626,stroke:#b91c1c,stroke-width:2px,color:#fff
    classDef data fill:#059669,stroke:#047857,stroke-width:2px,color:#fff
    classDef execution fill:#7c3aed,stroke:#6d28d9,stroke-width:2px,color:#fff
    classDef storage fill:#475569,stroke:#334155,stroke-width:2px,color:#fff
    classDef auto fill:#d97706,stroke:#b45309,stroke-width:2px,color:#fff

    class QuotingEngine,AlphaModel,Grid,DiffQuote engine
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

---

*设计亮点: 准机构级架构，热点路径完全内存化，零 DB 阻塞*
