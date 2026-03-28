# Tick 处理流程

```mermaid
flowchart TB
    subgraph Trigger["触发阶段 ⚡"]
        A["Redis PubSub<br/>tick:{token_id}"]
    end

    subgraph LoadConfig["配置加载"]
        B["_load_rewards_config()<br/>从 Redis 加载激励参数"]
        C{"rewards<br/>存在?"}
        C -->|Yes| D["解析<br/>min_size, spread, rates"]
        C -->|No| E["使用默认参数"]
    end

    subgraph FairValue["统一定价 Oracle 💎"]
        F["_get_unified_fair_value()<br/>计算 YES 锚点 FV"]
        G["FV_yes = clamp<br/>(mid + OBI × 0.015<br/>, 0.01, 0.99)"]
        H["发布到 Redis<br/>fv_anchor:{cid}"]
        I["NO 侧派生<br/>FV_no = 1 - FV_yes"]
    end

    subgraph BudgetCheck["严格 MTM 预算检查 💰"]
        J["获取 InventorySnapshot<br/>yes/no_exposure<br/>pending_buy_notional"]
        K["held_value =<br/>yes_exp × FV_yes +<br/>no_exp × FV_no"]
        L["strict_used =<br/>held_value +<br/>pending_yes_n + pending_no_n"]
        M{"strict_used <<br/>GLOBAL_MAX_BUDGET?"}
    end

    subgraph GridGen["网格生成 📊"]
        N{"持仓 > 5.0?"}
        N -->|Yes| O["Maker Unwind<br/>SELL 侧卖单"]
        N -->|No| P["网格档位生成"]
        P --> Q["SELL 侧: 持仓 × FV<br/>BUY 侧: 预算限制网格"]
        O --> R["极端行情?<br/>调整策略"]
        R -->|Yes| S["Extreme Taker<br/>更激进报价"]
        R -->|No| T["Normal Maker"]
    end

    subgraph BalancePrecheck["余额预检 💎"]
        U["_apply_balance_precheck()<br/>余额是否足够?"]
        V{"余额不足?"}
        V -->|Yes| W["缩档<br/>砍掉最低档"]
        V -->|No| X["保留全部档位"]
        W --> X
    end

    subgraph DiffQuote["差分报价 🔄"]
        Y["sync_orders_diff()<br/>精确匹配保留"]
        Z["非精确订单检查:<br/>1. Lifetime > 8s?<br/>2. Price offset < threshold?<br/>3. Within rewards band?"]
        AA["保留 / 撤单 决策"]
        AB["to_cancel<br/>待撤订单"]
        AC["to_create<br/>待发订单"]
    end

    subgraph Execution["执行层面 📦"]
        AE["并发撤单<br/>asyncio.gather"]
        AF["并发发单<br/>place_orders"]
        AG["更新<br/>active_orders"]
    end

    %% 主流程连接
    A --> B
    B --> C
    C --> D
    C --> E
    D --> F
    E --> F
    F --> G
    G --> H
    H --> I
    I --> J
    J --> K
    K --> L
    L --> M
    M -->|Yes| N
    M -->|No| ZO["跳过报价<br/>日志警告"]
    N --> P
    P --> Q
    Q --> U
    U --> V
    V --> W
    V --> X
    X --> Y
    Y --> Z
    Z --> AA
    AA --> AB
    AA --> AC
    AB --> AE
    AC --> AF
    AF --> AG

    %% 样式
    classDef trigger fill:#ff6b6b,color:#fff
    classDef config fill:#4ecdc4,stroke:#333
    classDef fv fill:#ffe66d,stroke:#333
    classDef risk fill:#f093fb,stroke:#333
    classDef grid fill:#95e1d3,stroke:#333
    classDef exec fill:#a8edea,stroke:#333

    class A trigger
    class B,C,D,E config
    class F,G,H,I fv
    class J,K,L,M risk
    class N,O,P,Q,R,S,T grid
    class U,V,W,X exec
    class Y,Z,AA,AB,AC exec
```

## 热路径关键指标

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Tick 处理性能目标                            │
├─────────────────────────────────────────────────────────────────────┤
│  🏃 端到端延迟:     < 10ms (无 DB 读取)                             │
│  📊 内存读取:       InventoryStateManager (热路径零 DB)            │
│  🔄 并发发单:       asyncio.gather (所有档位并发)                   │
│  📝 异步持久化:     有界队列 maxsize=1000, 关闭时排空               │
└─────────────────────────────────────────────────────────────────────┘
```

## 差分报价核心逻辑

```python
def sync_orders_diff(self, desired_buckets):
    """
    精确匹配保留，非精确按规则决策
    """
    # 1. 签名当前活跃订单
    active_by_sig = {
        (side, round(price,4), round(size,4)): [orders]
    }

    # 2. 遍历活跃订单
    for order_id, meta in self.active_orders.items():
        sig = _order_signature(meta)

        if sig in desired_buckets:
            # 精确匹配 → 保留
            desired_buckets[sig].pop()
        else:
            # 非精确 → 三重保护检查
            if meta.age_sec < RECONCILIATION_BUFFER:
                continue  # Lifetime 保护
            if price_diff <= PRICE_OFFSET_THRESHOLD:
                continue  # 价格偏移保护
            if within_rewards_band(meta.price):
                continue  # Rewards Band 保护
            to_cancel.append(order_id)

    # 3. 保留的 desired 生成 to_create
    to_create = [o for bucket in desired_buckets for o in bucket]
```

## 三重抗干扰保护

| 保护机制 | 触发条件 | 保护效果 |
|----------|----------|----------|
| **Lifetime 保护** | 订单年龄 < 8s | 成交后短期内不因价格变动而撤单 |
| **价格偏移保护** | 价格变动 < 0.005 | 轻微价格波动不撤单 |
| **Rewards Band 保护** | 订单仍在奖励带内 | 保持激励收益 |

---

*设计亮点: 热路径零 DB，统一定价 Oracle，差分报价最小化交易摩擦*
