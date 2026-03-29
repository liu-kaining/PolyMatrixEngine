# 核心模块关系图

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Entry["入口层"]
        FastAPI["FastAPI<br/>main.py"]
        Streamlit["Streamlit<br/>dashboard.py"]
    end

    subgraph MarketData["market_data/ 市场数据"]
        Gateway["MarketDataGateway<br/>WS + LocalOrderbook"]
        UserStream["UserStreamGateway<br/>WS + 成交处理 + Redis PubSub"]
        Gamma["GammaAPIClient<br/>HTTP + 元数据"]
    end

    subgraph Core["core/ 核心"]
        InvState["InventoryStateManager<br/>单例 + 有界队列"]
        Lifecycle["MarketLifecycle<br/>Engine Supervisor"]
        AutoRouter["AutoRouter<br/>Portfolio Manager"]
        Config["Config<br/>Pydantic Settings"]
    end

    subgraph Quoting["quoting/ 做市引擎"]
        Engine["QuotingEngine<br/>核心逻辑"]
        Alpha["AlphaModel<br/>内部组件: FV 计算"]
        Grid["GridGenerator<br/>内部组件: 网格档位"]
        Diff["DiffQuoter<br/>内部方法: 差分报价"]
    end

    Engine --> Alpha
    Engine --> Grid
    Engine --> Diff

    subgraph OMS["oms/ 订单管理"]
        OMSCore["OMS Core<br/>状态机"]
        Circuit["CircuitBreaker<br/>熔断器"]
        ClobClient["CLOB Client<br/>py-clob-client"]
    end

    subgraph Risk["risk/ 风控"]
        Watchdog["Watchdog<br/>RiskMonitor"]
        KillSwitch["KillSwitch<br/>硬熔断"]
        Reconciler["Reconciler<br/>对账引擎"]
    end

    subgraph Persistence["db/ 数据持久化"]
        Session["AsyncSession<br/>asyncpg"]
        Models["DB Models<br/>SQLAlchemy"]
    end

    %% FastAPI 控制
    FastAPI -->|"生命周期管理"| Lifecycle
    FastAPI -->|"风控指令"| Watchdog
    FastAPI -->|"状态查询"| InvState

    %% Dashboard 监控
    Streamlit -->|"实时监控"| InvState
    Streamlit -->|"Gamma查询"| Gamma

    %% MarketData 分发
    Gateway -->|"tick:{token}"| Engine
    Gateway -->|"ob:{token}"| Engine

    %% UserStream 通过 Redis PubSub 发布成交事件
    UserStream -.->|"Redis PubSub<br/>order_status:{cid}:{token}"| Engine
    UserStream -.->|"apply_fill() 直接调用"| InvState

    Gamma -->|"rewards config"| Engine
    Gamma -->|"market_info"| AutoRouter

    %% 引擎内部 (Alpha/Grid/Diff 是内部组件)
    Engine -->|"直接调用<br/>oms.create_order()"| OMSCore
    Engine -->|"直接调用<br/>oms.cancel_order()"| OMSCore

    Engine -->|"capital_used"| Watchdog
    Engine -->|"exposure"| InvState

    %% OMS 执行
    OMSCore -->|"熔断检查"| Circuit
    OMSCore -->|"CLOB HTTP"| ClobClient
    OMSCore -->|"HMAC签名"| ClobClient

    %% 风控
    Watchdog -->|"监控<br/>每秒检查"| InvState
    Watchdog -.->|"trigger_kill_switch<br/>触发时"| KillSwitch
    Watchdog -.->|"reconcile_loop<br/>周期对账"| Reconciler
    KillSwitch -.->|"cancel_all"| OMSCore
    KillSwitch -.->|"suspend market"| Models
    Reconciler -->|"覆盖状态"| InvState
    Reconciler -->|"Polymarket<br/>Data API"| Models

    %% AutoRouter
    AutoRouter -->|"start/stop"| Lifecycle
    Lifecycle -->|"engine_tasks"| Engine

    %% 持久化
    InvState -->|"async_persist"| Session
    Session --> Models

    %% 样式 - 专业沉稳配色
    classDef entry fill:#1e3a5f,stroke:#334155,color:#fff
    classDef data fill:#0891b2,stroke:#0e7490,color:#fff
    classDef core fill:#475569,stroke:#334155,color:#fff
    classDef quoting fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef oms fill:#059669,stroke:#047857,color:#fff
    classDef risk fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef db fill:#64748b,stroke:#475569,color:#fff

    class FastAPI,Streamlit entry
    class Gateway,UserStream,Gamma data
    class InvState,Lifecycle,AutoRouter,Config core
    class Engine,Alpha,Grid,Diff quoting
    class OMSCore,Circuit,ClobClient oms
    class Watchdog,KillSwitch,Reconciler risk
    class Session,Models db
```

## 模块依赖矩阵

```
                    Gateway  UserStream  Gamma  InvState  Engine  OMS  Watchdog  AutoRouter
MarketDataGateway     -        ↑          -       -         ↑      -       -          -
UserStreamGateway     -        -          -       ↑         -      ↑      -          -
GammaAPIClient        -        -          -       -         ↑      -       -          ↑
InventoryState        ↑        -          -       -         ↑      -       ↑          -
QuotingEngine         -        -          ↑       ↑         -      ↑      ↑          -
OMS                   -        -          -       -         -      -       -          -
Watchdog              -        -          -       ↑         -      ↑      -          -
AutoRouter            -        -          ↑       -         ↑      -       -          -
```

> **↑** = 依赖方向

## 关键接口契约

| 接口 | 路径 | 用途 |
|------|------|------|
| `tick:{token}` | Redis PubSub | 订单簿增量更新触发 |
| `ob:{token}` | Redis PubSub | 完整快照推送 |
| `apply_fill()` | 内存方法 | 成交后内存更新 |
| `check_exposure()` | Watchdog | 每秒风控检查 |
| `sync_orders_diff()` | Engine | 差分报价同步 |
