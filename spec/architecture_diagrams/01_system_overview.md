# 系统整体架构图

以下为 **兼容性优先** 的 Mermaid：无 `%%{init}`、无嵌套 subgraph、无 `class`/`style`、无边标签、无 `<br/>`，避免 Cursor / VS Code / GitHub 内置解析器报错。原「彩色分层 + 嵌套分组」若需导出 PPT，可用 [mermaid.live](https://mermaid.live) 在可渲染版本上再加样式。

```mermaid
flowchart TB
    subgraph S1["客户端层"]
        N1["Streamlit 驾驶舱"]
        N2["FastAPI 控制面"]
    end
    subgraph S2["数据面"]
        N3["Market WebSocket"]
        N26["User WebSocket"]
        N4["REST 快照"]
        N5["Gamma API"]
        N6["Redis PubSub tick"]
        N7["Redis PubSub ob"]
        N8["Redis PubSub control"]
        N9["Redis PubSub order_status"]
    end
    subgraph S3["核心引擎层"]
        N10["AlphaModel FV"]
        N11["GridGenerator"]
        N12["sync_orders_diff"]
        N13["Watchdog"]
        N14["KillSwitch"]
        N15["Reconciler"]
        N16["InventoryStateManager"]
    end
    subgraph S4["执行平面"]
        N17["OMS"]
        N18["CircuitBreaker"]
        N19["CLOB"]
        N20["Builder API"]
    end
    subgraph S5["数据层"]
        N21["PostgreSQL"]
        N22["Redis KV"]
    end
    subgraph S6["自动路由"]
        N23["PortfolioManager"]
        N24["radar_scan 评分"]
        N25["赛道重平衡"]
    end
    N1 --> N2
    N2 --> N10
    N2 --> N23
    N3 --> N6
    N3 --> N7
    N26 --> N21
    N6 --> N10
    N7 --> N10
    N5 --> N10
    N10 --> N11
    N11 --> N12
    N12 -.-> N17
    N17 --> N19
    N17 --> N20
    N17 --> N21
    N13 --> N16
    N13 -.-> N14
    N13 -.-> N15
    N14 -.-> N17
    N15 --> N21
    N21 --> N16
    N23 --> N24
    N24 --> N25
    N23 --> N10
    N23 --> N5
```

## 架构说明

### 此前常见渲染失败原因（已在本图规避）

1. **`style QuotingEngine`**：`QuotingEngine` 是 **subgraph**，不是节点；对 subgraph 做 `style` 在多数版本会直接语法错误。  
2. **子图套子图**：三层嵌套在部分预览引擎上不稳定。  
3. **`%%{init: ...}`**：`themeVariables` 大括号或引号稍有不匹配，整段图失败。  
4. **`classDef` 里的 `stroke-width`**：少数解析器对带连字符的样式串不兼容。  
5. **边标签**里的 `/`、`()`、`<br/>`：个别渲染器会误解析。

### 三层分离设计

| 层次 | 职责 | 关键技术 |
|------|------|----------|
| **数据面** | WebSocket 订阅、REST 快照、消息分发 | `redis.asyncio` Pub/Sub |
| **核心引擎层** | 定价、报价、风控、库存 | asyncio 热路径零 DB |
| **执行平面** | OMS 状态机、CLOB 交互、签名 | py-clob-client |

### 关键设计原则

1. **已提交快照**: 报价 tick 无 DB 读取；成交与订单先 durable commit，再同步内存
2. **消息解耦**: 所有模块通过 Redis Pub/Sub 通信
3. **状态分离**: 控制面(FastAPI) 与 数据面(Engine) 解耦
4. **原子事实层**: 成交 → inbox/reservation/cash/fee/inventory/order 同事务 → versioned memory snapshot

> **图注**：`InventoryStateManager` 是报价热路径缓存，不是 durable 事实源；User WS fill 先提交 PostgreSQL。

---

*安全边界：报价 tick 读取内存；任何资金、订单或成交事实必须先持久化。*
