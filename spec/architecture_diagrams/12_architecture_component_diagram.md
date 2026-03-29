# 组件关系总览（Mermaid）

> 对应原 PlantUML 架构总览图，**单一工具链**：与本目录其他图一致，用 Mermaid 在 GitHub / VS Code 即可渲染。  
> **做市行为与标志位**见 [`03_quoting_state_machine.md`](./03_quoting_state_machine.md)；**四层风控细节**见 [`07_risk_control_layers.md`](./07_risk_control_layers.md)。

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Client["客户端层"]
        API["FastAPI 控制面"]
        Dashboard["Streamlit 驾驶舱"]
    end

    subgraph DataPlane["数据面 Data Plane"]
        MarketWS["Market WebSocket"]
        UserWS["User WebSocket"]
        GammaAPI["Gamma / 元数据<br/>+ CLOB rewards 等"]
        TickQ["PubSub tick:"]
        OBQ["PubSub ob:"]
    end

    subgraph Core["核心引擎层"]
        subgraph QE["QuotingEngine × 2"]
            Alpha["AlphaModel"]
            Grid["Grid Generator"]
            DiffQuote["Diff Quoter"]
        end
        subgraph Risk["风控平面"]
            Watchdog["Watchdog"]
            KillSwitch["Kill Switch"]
            Reconcile["Reconciler<br/>reconcile_positions"]
        end
        InvState["InventoryStateManager"]
        subgraph AutoRt["自动路由"]
            PortfolioMgr["Portfolio / auto_router"]
            Scorer["评分 _radar_scan"]
            Lifecycle["MarketLifecycle"]
        end
    end

    subgraph Exec["执行平面 Execution"]
        OMS["OMS Core"]
        CB["CircuitBreaker"]
        CLOB["CLOB Client"]
        Builder["Builder API"]
    end

    subgraph Store["数据层"]
        PG[("PostgreSQL")]
        RedisKV[("Redis KV")]
    end

    Dashboard --> API
    API --> Watchdog
    API --> PortfolioMgr

    MarketWS --> TickQ
    MarketWS --> OBQ

    TickQ --> Alpha
    OBQ --> Alpha

    GammaAPI --> PortfolioMgr
    GammaAPI --> Alpha

    Alpha --> Grid
    Grid --> DiffQuote
    DiffQuote --> OMS

    Watchdog --> InvState
    Watchdog --> KillSwitch
    KillSwitch --> OMS
    Reconcile --> PG

    InvState --> OMS
    InvState --> PG

    PortfolioMgr --> Lifecycle
    Lifecycle --> Alpha
    Scorer -.-> PortfolioMgr

    OMS --> CLOB
    OMS --> CB
    CB --> CLOB
    OMS --> Builder

    CLOB --> MarketWS
    CLOB --> UserWS

    classDef client fill:#1e3a5f,stroke:#334155,color:#fff
    classDef dataPlane fill:#0891b2,stroke:#0e7490,color:#fff
    classDef core fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef riskC fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef execC fill:#059669,stroke:#047857,color:#fff
    classDef storeC fill:#475569,stroke:#334155,color:#fff

    class API,Dashboard client
    class MarketWS,UserWS,GammaAPI,TickQ,OBQ dataPlane
    class Alpha,Grid,DiffQuote,PortfolioMgr,Scorer,Lifecycle core
    class Watchdog,KillSwitch,Reconcile riskC
    class OMS,CB,CLOB,Builder execC
    class PG,RedisKV,InvState storeC
```

## 图注

- **Redis**：`tick:` / `ob:` / `control:` / `order_status:` 等同实例上的 Pub/Sub 主题，图中只画与引擎订阅主链相关的 tick/ob。
- **Scorer**：逻辑在 `auto_router._radar_scan` 内，与 `PortfolioMgr` 为同一模块内的步骤，虚线表示「从属」而非跨进程调用。
- **Reconciler**：周期由 `RECONCILIATION_INTERVAL_SEC` 控制（默认 3600s），与硬重置后的 `reconcile_single_market(force=True)` 不同，详见 [`08_watchdog_mechanism.md`](./08_watchdog_mechanism.md)。

---

*原 `12/13/14_plantuml_*.puml` 已移除，统一维护 Mermaid 源。*
