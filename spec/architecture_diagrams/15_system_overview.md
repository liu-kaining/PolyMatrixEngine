# PolyMatrix Engine V6.4 系统概览

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Client["客户端层"]
        Dashboard["Streamlit 驾驶舱<br/>监控 + 控制"]
        API["FastAPI 控制面<br/>/start /stop /liquidate"]
    end

    subgraph Infra["基础设施"]
        Redis["Redis<br/>消息总线 + KV"]
        Postgres["PostgreSQL<br/>持久化"]
    end

    subgraph DataPlane["数据面"]
        MarketWS["Market WebSocket<br/>订单簿订阅"]
        UserWS["User WebSocket<br/>成交/撤单"]
        GammaAPI["Gamma API<br/>市场元数据"]
    end

    subgraph Core["核心引擎"]
        subgraph Engine["QuotingEngine × 2"]
            Alpha["AlphaModel<br/>统一定价 Oracle"]
            Grid["网格生成器<br/>动态 Spread"]
            Diff["差分报价器<br/>三重保护"]
        end

        subgraph Risk["风控平面"]
            Watchdog["Watchdog<br/>每秒检查"]
            KillSwitch["Kill Switch<br/>硬熔断"]
            Reconciler["对账引擎<br/>reconcile_positions<br/>间隔默认 3600s"]
        end

        InvState["InventoryStateManager<br/>内存优先 + 异步持久化"]
    end

    subgraph Execution["执行平面"]
        OMS["OMS 状态机"]
        Circuit["CircuitBreaker<br/>熔断器"]
        CLOB["Polymarket CLOB"]
        Builder["Builder API<br/>订单归因"]
    end

    subgraph Router["自动路由"]
        PortfolioMgr["PortfolioManager<br/>组合管理"]
        Scorer["评分引擎<br/>ROI × 流动性 × 时间"]
    end

    %% 连接关系
    Dashboard --> API
    API -->|"生命周期管理"| Engine
    API -->|"风控指令"| Watchdog

    MarketWS -->|"tick:| ob:"| Redis
    UserWS -->|"order_status"| Redis
    GammaAPI -->|"rewards"| Redis

    Redis -->|"tick:| ob:"| Engine
    Redis -->|"order_status"| OMS

    Engine --> OMS
    OMS --> CLOB
    OMS --> Builder

    Watchdog --> InvState
    Watchdog --> KillSwitch
    Reconciler --> Postgres

    InvState --> Postgres
    InvState --> Engine

    PortfolioMgr --> Scorer
    Scorer -->|"start/stop"| Engine

    %% 样式定义
    classDef client fill:#0891b2,stroke:#0e7490,color:#fff
    classDef infra fill:#64748b,stroke:#475569,color:#fff
    classDef data fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef engine fill:#1e3a5f,stroke:#334155,color:#fff
    classDef risk fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef exec fill:#059669,stroke:#047857,color:#fff
    classDef router fill:#d97706,stroke:#b45309,color:#fff

    class Dashboard,API client
    class Redis,Postgres infra
    class MarketWS,UserWS,GammaAPI data
    class Alpha,Grid,Diff engine
    class Watchdog,KillSwitch,Reconciler risk
    class OMS,Circuit,CLOB,Builder exec
    class PortfolioMgr,Scorer router
```

## 核心设计亮点

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart LR
    subgraph Highlights["技术亮点"]
        H1["热路径零 DB 读取<br/>Tick 处理只读内存"]
        H2["统一定价 Oracle<br/>YES FV → NO 派生"]
        H3["差分报价<br/>只撤不一致订单"]
        H4["四层风控<br/>报价前 → 硬熔断 → 对账 → 硬重置"]
        H5["智能自动路由<br/>评分 + 赛道隔离"]
        H6["Ghost Order 防护<br/>5 分钟硬重置周期"]
    end

    classDef highlight fill:#0891b2,stroke:#0e7490,color:#fff

    class H1,H2,H3,H4,H5,H6 highlight
```

## 核心参数一览

| 参数 | 值 | 说明 |
|------|-----|------|
| MAX_EXPOSURE_PER_MARKET | $50 | 单市场敞口上限 |
| GLOBAL_MAX_BUDGET | $1000 | 全局资金上限 |
| EXPOSURE_TOLERANCE | 1% | 对账容差 |
| RECONCILIATION_BUFFER | 8s | 时间保护窗口 |
| RECONCILIATION_INTERVAL_SEC | 3600s（默认） | Watchdog 全量对账；`.env` 可改 |
| HARD_RESET_INTERVAL | 5min | 硬重置间隔 |
| EVENT_HORIZON | 24h | 事件地平线 |
| CIRCUIT_BREAKER_FAILURES | 5次 | 熔断阈值 |

## 快速导航

| 图表 | 文件 | 内容 |
|------|------|------|
| 系统架构 | `01_system_overview.md` | 整体架构图 |
| 模块关系 | `02_module_relationships.md` | 核心模块关系 |
| 状态机 | `03_quoting_state_machine.md` | QuotingEngine 状态机 |
| Tick 处理 | `04_tick_processing_flow.md` | Tick 处理流程 |
| 差分报价 | `05_differential_quoting.md` | 差分报价详解 |
| 成交处理 | `06_fill_processing_flow.md` | 成交处理流程 |
| 风控体系 | `07_risk_control_layers.md` | 多层风控体系 |
| Watchdog | `08_watchdog_mechanism.md` | Watchdog 监控机制 |
| 自动路由 | `09_auto_router.md` | 自动路由与组合管理 |
| 硬重置 | `10_hard_reset_flow.md` | 硬重置流程 |
| 数据库 | `11_database_erd.md` | 数据库 ER 图 |

---

*PolyMatrix Engine V6.4 - 面向 Polymarket 的准机构级自动化做市与流动性引擎*
