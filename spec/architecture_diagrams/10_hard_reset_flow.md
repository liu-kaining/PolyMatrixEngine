# 硬重置流程 (V6.4)

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Schedule["硬重置调度"]
        A["每 5 分钟<br/>hard_reset_cycle"]
        B{"当前状态?<br/>ACTIVE / GRACEFUL_EXIT"}
        A --> B
        B -->|ACTIVE| C["继续重置流程"]
        B -->|其他| D["跳过本次"]
    end

    subgraph CancelAll["全钱包撤单"]
        C["OMS<br/>physical_clob_cancel_all()"]
        E["CLOB HTTP POST<br/>/cancel_all"]
        F["超时: 45s"]
        G{"成功?"}
        H["等待 3s<br/>USDC 释放"]
        I["读取余额日志<br/>验证"]
        C --> E --> F --> G
        G -->|Yes| H --> I
        G -->|No| J["重试 1 次"]
        J --> E
    end

    subgraph LocalCancel["本地状态清理"]
        K["cancel_all_orders()<br/>force_evict=True"]
        L["清空 active_orders"]
        M["清空 pending_buy_notional"]
        N["重置引擎状态 → SUSPENDED"]
    end

    subgraph ForceReconcile["强制对账"]
        O["reconcile_single_market<br/>(force=True)"]
        P["绕过时间保护<br/>强制覆盖"]
        Q["同步 InventoryLedger<br/>到内存"]
    end

    subgraph Decision["对账决策"]
        R{"对账<br/>成功?"}
        S["POST_RESET_<br/>RECONCILE_FREEZE<br/>冻结 BUY"]
        T["引擎状态 → QUOTING<br/>恢复报价"]
    end

    subgraph Recovery["恢复阶段"]
        U["订阅 Market WS<br/>tick: ob:"]
        V["订阅 User WS<br/>fills, cancels"]
        W["恢复 QuotingEngine<br/>engine_tasks"]
    end

    %% 连接
    C --> E
    I --> K
    K --> L --> M --> N
    N --> O
    O --> P --> Q
    Q --> R
    R -->|Yes| T
    R -->|No| S
    S --> U
    T --> U
    U --> V --> W

    classDef schedule fill:#0891b2,stroke:#0e7490,color:#fff
    classDef cancel fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef reconcile fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef decision fill:#d97706,stroke:#b45309,color:#fff
    classDef recovery fill:#059669,stroke:#047857,color:#fff

    class A,B,D schedule
    class C,E,F,G,H,I,J cancel
    class K,L,M,N,O,P,Q reconcile
    class R,S,T decision
    class U,V,W recovery
```

## 硬重置核心代码

```python
async def physical_clob_cancel_all_for_hard_reset(self):
    """
    V6.4 硬重置:
    1. 全钱包 CLOB cancel_all (超时 45s)
    2. 等待 USDC 释放 (默认 3s)
    3. 读取余额日志
    4. 清理本地状态
    """
    # 1. CLOB 全钱包撤单
    try:
        result = await self.clob_client.cancel_all(
            timeout=45,
            retries=1
        )
        logger.info(f"Cancel all result: {result}")
    except asyncio.TimeoutError:
        logger.error("CLOB cancel_all timeout")
        raise

    # 2. 等待 USDC 释放
    await asyncio.sleep(HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC)

    # 3. 验证余额变化
    balance_log = await self._read_balance_log()
    logger.info(f"Balance after cancel_all: {balance_log}")

    # 4. 清理本地状态
    await self.cancel_all_orders(force_evict=True)

    # 5. 强制对账
    for cid in get_active_router_markets():
        success = await reconcile_single_market(cid, force=True)
        if not success:
            logger.warning(f"Reconcile failed for {cid}, will freeze")
            set_engine_state(cid, EngineState.POST_RESET_RECONCILE_FREEZE)


async def cancel_all_orders(self, force_evict: bool = False):
    """
    强制清理所有本地订单状态
    force_evict: True 时强制从 active_orders 移除
    """
    async with self._orders_lock:
        # 并发撤单
        cancel_tasks = [
            self.cancel_order(oid)
            for oid in list(self.active_orders.keys())
        ]
        await asyncio.gather(*cancel_tasks, return_exceptions=True)

        # 强制驱逐
        if force_evict:
            self.active_orders.clear()
            self._pending_buys.clear()

        # 重置状态
        self._state = EngineState.SUSPENDED
```

## Ghost Order 防护

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart LR
    subgraph Problem["Ghost Order 问题"]
        P1["引擎重启后本地无订单状态"]
        P2["CLOB 上仍有挂单"]
        P3["报价时以为没单<br/>实际有单 → 双边持仓失控"]
    end

    subgraph Solution["V6.4 解决方案"]
        S1["CLOB cancel_all"]
        S2["本地状态清理"]
        S3["强制对账"]
    end

    subgraph Result["效果"]
        R1["CLOB 无残留订单"]
        R2["本地状态干净"]
        R3["对账确认持仓一致"]
    end

    P1 --> S1
    P2 --> S2
    P3 --> S3

    S1 --> R1
    S2 --> R2
    S3 --> R3

    classDef problem fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef solution fill:#0891b2,stroke:#0e7490,color:#fff
    classDef result fill:#059669,stroke:#047857,color:#fff

    class P1,P2,P3 problem
    class S1,S2,S3 solution
    class R1,R2,R3 result
```

## 重置时间线

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
timeline
    title 硬重置完整流程

    section T+0s
        CLOB cancel_all
        : 超时 45s

    section T+3s
        睡眠等待 USDC 释放

    section T+4s
        本地 cancel_all_orders(force_evict=True)
        : 清空 active_orders
        : 清空 pending_buys
        : 引擎状态 → SUSPENDED

    section T+5s
        reconcile_single_market(force=True)
        : 绕过时间保护
        : 强制覆盖 DB
        : 同步内存状态

    section T+6s
        决策
        : 成功 → 引擎状态 → QUOTING
        : 失败 → 引擎状态 → POST_RESET_RECONCILE_FREEZE

    section T+7s
        恢复
        : 订阅 WS, 恢复 QuotingEngine
```

## 对账失败冻结

```python
# 对账失败后的状态
if not reconcile_success:
    engine_state = EngineState.POST_RESET_RECONCILE_FREEZE

    # 效果:
    # - 禁止新 BUY 订单
    # - 保持现有持仓
    # - 每分钟重试对账
    # - 下次对账成功 → 恢复正常
```

---

*设计亮点: V6.4 硬重置彻底解决 Ghost Order 问题，5 分钟周期保证系统状态最终一致*
