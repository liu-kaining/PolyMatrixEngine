# Watchdog 监控机制

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
flowchart TB
    subgraph Init["Watchdog 启动"]
        A["RiskMonitor.run()"]
        B["asyncio.create_task<br/>reconciliation_loop()"]
        C["进入主循环<br/>while True"]
    end

    subgraph MainLoop["主监控循环"]
        D["await asyncio.sleep(1)<br/>每秒心跳"]
        E["check_exposure()<br/>暴露检查"]
        F{"暴露 ><br/>MAX_EXPOSURE?"}
        G["trigger_kill_switch()<br/>硬熔断"]
    end

    subgraph Reconcile["对账循环"]
        H["await asyncio.sleep(60)<br/>60s 间隔"]
        I["reconcile_positions()<br/>批量对账"]
        J["获取活跃市场列表<br/>get_active_router_markets()"]
        K["for each market:<br/>reconcile_single_market()"]
    end

    subgraph SingleReconcile["单市场对账"]
        L["GET Data API<br/>/positions?user={address}"]
        M["解析 positions<br/>{condition_id: {yes, no}}"]
        N["行锁 InventoryLedger"]
        O{"DB vs API<br/>差异检查"}
        P{"差异 > tolerance<br/>且 非保护窗口?"}
        Q["覆盖 DB<br/>yes/no_exposure"]
        R["修正 capital_used<br/>按比例"]
        S["apply_reconciliation_snapshot()<br/>同步内存"]
    end

    subgraph HardReset["硬重置强制对账"]
        T["每 5 分钟<br/>hard_reset_cycle"]
        U["physical_clob_cancel_all()<br/>全钱包撤单"]
        V["睡眠 3s"]
        W["本地 cancel_all<br/>force_evict=True"]
        X["reconcile_single_market<br/>(force=True)"]
        Y{"对账<br/>成功?"}
        Z["POST_RESET_<br/>RECONCILE_FREEZE"]
    end

    Init --> C
    C --> D
    D --> E
    E --> F
    F -->|Yes| G
    F -->|No| D
    G --> D

    C --> H
    H --> I
    I --> J
    J --> K
    K --> L
    L --> M
    M --> N
    N --> O
    O --> P
    P -->|Yes| Q
    P -->|No| S
    Q --> R
    R --> S
    S --> K
    K -->|"遍历完成"| C

    T --> U --> V --> W --> X --> Y
    Y -->|Yes| C
    Y -->|No| Z
    Z --> C

    classDef init fill:#0891b2,stroke:#0e7490,color:#fff
    classDef main fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef reconcile fill:#7c3aed,stroke:#6d28d9,color:#fff
    classDef reset fill:#d97706,stroke:#b45309,color:#fff

    class A,B init
    class D,E,F,G main
    class H,I,J,K,L,M,N,O,P,Q,R,S reconcile
    class T,U,V,W,X,Y,Z reset
```

## check_exposure 核心逻辑

```python
async def check_exposure(self):
    """
    每秒检查所有活跃市场的暴露
    触发 kill_switch 条件:
    - 单市场 capital_used > MAX_EXPOSURE_PER_MARKET
    """
    active_cids = get_active_router_markets()

    for cid in active_cids:
        # 获取内存快照 (热路径零 DB)
        snap = await inventory_state.get_snapshot(cid)

        # 计算实际使用资金
        actual_used = snap.yes_capital_used + snap.no_capital_used

        if actual_used > MAX_EXPOSURE_PER_MARKET:
            # DB 验证状态
            async with get_db_session() as session:
                market = await session.get(MarketMeta, cid)
                if market.status != MarketStatus.SUSPENDED:
                    await self.trigger_kill_switch(cid, session)
```

## 对账核心逻辑

```python
async def reconcile_single_market(self, condition_id: str, force: bool = False):
    """
    单市场对账:
    1. 从 Polymarket Data API 获取真实持仓
    2. 对比本地 DB 记录
    3. 差异超过容差 → 覆盖更新
    """
    # 1. 获取 API 持仓
    api_positions = await self._fetch_api_positions(address)

    # 2. 获取本地 DB 持仓
    async with get_db_session() as session:
        async with session.begin():
            ledger = await session.get(
                InventoryLedger,
                (condition_id, funding_address),
                with_for_update=True  # 行锁
            )

            # 3. 对比差异
            diff_yes = abs(api_positions.yes - ledger.yes_exposure)
            diff_no = abs(api_positions.no - ledger.no_exposure)

            if max(diff_yes, diff_no) > EXPOSURE_TOLERANCE:
                # 4. 时间保护
                if not force and self._within_reconciliation_buffer(ledger):
                    return True  # 跳过

                # 5. 覆盖更新
                ledger.yes_exposure = api_positions.yes
                ledger.no_exposure = api_positions.no

                # 6. 修正 capital_used
                ratio = api_positions.yes / max(ledger.yes_exposure, 0.001)
                ledger.yes_capital_used *= ratio

    # 6. 同步内存
    await inventory_state.apply_reconciliation_snapshot(condition_id, ledger)
    return True
```

## 监控时间线

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
timeline
    title Watchdog 时间线

    section 1秒检查
        check_exposure()
        : 遍历所有活跃市场
        : 比较 capital_used vs 阈值
        : 超限 → trigger_kill_switch

    section 60秒对账
        reconciliation_loop()
        : 遍历所有活跃市场
        : 调用 Polymarket Data API
        : 差异 > tolerance → 覆盖更新

    section 5分钟硬重置
        hard_reset_cycle()
        : CLOB cancel_all
        : 本地状态清理
        : 强制对账
```

## 熔断器状态

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
  'primaryColor': '#1e3a5f',
  'primaryTextColor': '#ffffff',
  'primaryBorderColor': '#334155',
  'lineColor': '#64748b'
}}%%
stateDiagram-v2
    [*] --> CLOSED: 初始化
    CLOSED --> OPEN: 5 次连续失败
    OPEN --> HALF_OPEN: 60s 后尝试
    HALF_OPEN --> CLOSED: 请求成功
    HALF_OPEN --> OPEN: 请求失败

    note right of CLOSED
        CircuitBreaker
        CLOSED: 正常允许请求
        OPEN: 拒绝请求 (熔断)
        HALF_OPEN: 测试请求
    end note
```

---

*设计亮点: 多时间尺度的风控检查，从秒级熔断到分钟级对账，全方位保障系统安全*
