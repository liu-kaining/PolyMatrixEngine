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

    subgraph MainLoop["主监控循环 (每秒)"]
        D["await asyncio.sleep(1)<br/>每秒心跳"]
        E["check_exposure()<br/>暴露检查 (内存)"]
        F{"capital_used 是否超过<br/>MAX_EXPOSURE?"}
        G["trigger_kill_switch()<br/>硬熔断"]
    end

    subgraph Reconcile["对账循环 reconciliation_loop（独立 task）"]
        H["sleep<br/>RECONCILIATION_INTERVAL_SEC<br/>config 默认 3600s"]
        I["reconcile_positions()<br/>全量持仓对账"]
        L["GET Data API<br/>/positions 一次"]
        M["按 conditionId 聚合 API 仓位"]
        N["SELECT InventoryLedger<br/>with_for_update 全表行"]
        O["for each 行:<br/>比对 API、尊重 buffer、<br/>必要时覆盖 DB 并<br/>apply_reconciliation_snapshot"]
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
    I --> L
    L --> M
    M --> N
    N --> O
    O --> H

    classDef init fill:#0891b2,stroke:#0e7490,color:#fff
    classDef main fill:#dc2626,stroke:#b91c1c,color:#fff
    classDef reconcile fill:#7c3aed,stroke:#6d28d9,color:#fff

    class A,B init
    class D,E,F,G main
    class H,I,L,M,N,O reconcile
```

> **图注**：硬重置不在 Watchdog 中；由 `QuotingEngine.on_tick()` 约每 300s 触发，详见 `10_hard_reset_flow.md`。**单市场** `reconcile_single_market(force=True)` 由引擎在硬重置后调用，**不在**此周期图的主链上（与 `reconcile_positions` 不同）。

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

    section 每秒主循环
        心跳间隔 : asyncio.sleep 约 1s
        暴露检查 : check_exposure 遍历活跃市场
        熔断条件 : capital_used 超阈值则 kill_switch

    section 周期对账
        间隔可配 : RECONCILIATION_INTERVAL_SEC 见 .env
        批量任务 : reconcile_positions Data API
        覆盖规则 : 超容差且过保护窗则写库并同步内存

    section 硬重置
        职责划分 : 由 QuotingEngine 触发非 Watchdog
        周期参考 : 约每 300s 见引擎逻辑
```

## 熔断器状态 (在 OMS 中)

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
        CircuitBreaker (在 OMS 中)
        CLOSED: 正常允许请求
        OPEN: 拒绝请求 (熔断)
        HALF_OPEN: 测试请求
    end note
```

---

*设计亮点: 多时间尺度的风控检查，秒级熔断 + 周期对账，QuotingEngine 内部硬重置解决 Ghost Order*
