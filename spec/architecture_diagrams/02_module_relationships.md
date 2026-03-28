# 核心模块关系图

```mermaid
graph LR
    subgraph Entry["入口层"]
        FastAPI["FastAPI<br/>main.py"]
        Streamlit["Streamlit<br/>dashboard.py"]
    end

    subgraph MarketData["market_data/ 市场数据"]
        Gateway["MarketDataGateway<br/>WS + LocalOrderbook"]
        UserStream["UserStreamGateway<br/>WS + 成交处理"]
        Gamma["GammaAPIClient<br/>HTTP + 元数据"]
    end

    subgraph Core["core/ 核心"]
        InvState["InventoryStateManager<br/>单例 + 有界队列"]
        Lifecycle["MarketLifecycle<br/>Engine Supervisor"]
        AutoRouter["AutoRouter<br/>Portfolio Manager"]
        Config["Config<br/>Pydantic Settings"]
    end

    subgraph Quoting["quoting/ 做市引擎"]
        Engine["QuotingEngine<br/>~1150行 核心逻辑"]
        Alpha["AlphaModel<br/>FV 计算"]
        Grid["GridGenerator<br/>网格档位"]
        Diff["DiffQuoter<br/>差分报价"]
    end

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
    Gateway -->|"subscribe"| UserStream

    UserStream -->|"apply_fill"| InvState
    UserStream -->|"order_status"| OMSCore

    Gamma -->|"rewards config"| Engine
    Gamma -->|"market_info"| AutoRouter

    %% 引擎内部
    Engine -->|"FV 计算"| Alpha
    Alpha -->|"锚点"| Grid
    Grid -->|"网格"| Diff
    Diff -->|"place/cancel"| OMSCore

    Engine -->|"capital_used"| Watchdog
    Engine -->|"exposure"| InvState

    %% OMS 执行
    OMSCore -->|"熔断检查"| Circuit
    Circuit -->|"通过"| ClobClient
    OMSCore -->|"HMAC签名"| ClobClient

    %% 风控
    Watchdog -->|"监控"| InvState
    Watchdog -->|"kill_switch"| KillSwitch
    Watchdog -->|"reconcile"| Reconciler
    KillSwitch -->|"cancel_all"| OMSCore
    Reconciler -->|"覆盖状态"| InvState
    Reconciler -->|"Polymarket<br/>Data API"| Models

    %% AutoRouter
    AutoRouter -->|"start/stop"| Lifecycle
    Lifecycle -->|"engine_tasks"| Engine

    %% 持久化
    InvState -->|"async_persist"| Session
    Session --> Models

    %% 样式
    classDef entry fill:#667eea,stroke:#333,color:#fff
    classDef data fill:#4ecdc4,stroke:#333
    classDef core fill:#ffe66d,stroke:#333
    classDef quoting fill:#ff6b6b,stroke:#333,color:#fff
    classDef oms fill:#95e1d3,stroke:#333
    classDef risk fill:#f093fb,stroke:#333
    classDef db fill:#a8edea,stroke:#333

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
