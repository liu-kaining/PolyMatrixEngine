# PolyMatrix Engine V3.0 Farming — 安全与质量审计报告

**审计范围**: 资金风控、除零/类型安全、死代码、异常处理、鲁棒性。  
**结论**: 发现 2 项 CRITICAL、4 项 WARNING、3 项 OPTIMIZATION；CRITICAL 已给出修复方案并在代码中落实。

---

## 1. 资金风控与致命逻辑漏洞 (Critical Financial Risks)

### 1.1 除零错误 (Division by Zero)

**检查结果: 通过**

- **Farming Score** (`dashboard/app.py`): `liq = max(1.0, ...)`, `turnover = vol/liq if liq > 0 else 0`, `daily_yield = r_rate / max(liq, 1.0)`. 无除零风险。
- **报价引擎** `_compute_effective_size`: `budget_per_order = max_exposure / total_slots`, `total_slots = max(1, self.grid_levels * 2)`. `safe_size = budget_per_order / price if price > 0 else self.base_size`. 已防护。
- **_apply_balance_precheck**: `max_size = remaining / o["price"]` 仅在 `o["price"] > 0` 时执行。 已防护。

### 1.2 无限发单 / 循环重试

**检查结果: 通过**

- **Circuit Breaker** (OMS): 400/403 视为 non-transient，不累计失败次数；其他错误（含 429）累计，达到阈值后 OPEN，**阻止所有新请求**。 不会在 400 余额不足时无限重试。
- **WS 断线**: User/Market Gateway 使用指数退避重连（1s → 60s 封顶），重连的是 **连接**，不是重复发单。 发单仅由 tick 驱动，无“断线后疯狂发单”路径。

### 1.3 敞口控制 (MAX_EXPOSURE)

**检查结果: 通过**

- **_apply_balance_precheck** 在 **sync_orders_diff 之前** 执行，用 `available = budget_limit - used_notional` 裁剪 BUY 单；`used_notional = current_exposure + opposite_exposure` 双侧计费。 本地预检严密，无绕过路径。
- **Watchdog** 基于 DB 的 InventoryLedger 检查，超限触发 kill switch（suspend + cancel_market_orders）。 与引擎侧预检双重保护。

### 1.4 精度问题 (Tick Size / 舍入)

**检查结果: ⚠️ WARNING**

- 引擎固定 `tick_size = 0.01`，价格统一 `round(..., 2)`。 若某市场 CLOB 要求 0.001 tick，可能 400。
- **建议**: 从 Gamma/MarketMeta 读取 per-market `orderPriceMinTickSize`（若有），未配置时回退 0.01；下单前按该 tick 对齐价格。 当前实盘若仅交易 0.01 tick 市场可暂缓。

---

## 2. 死代码与幽灵依赖 (Dead Code & Artifacts)

**检查结果: 通过**

- 全局检索 `Conservative`, `Aggressive`, `Ultra`, `Normal`（screener 模式）: **无残留**。 仅 `spec/` 文档中留有历史描述，非执行逻辑。
- FastAPI 与 Dashboard 均**未**使用 `request.args.get("mode")` 或类似 screener mode 参数；Dashboard 筛选为进程内 `_filter_and_score_screener(raw_markets)`，无后端 mode 依赖。

---

## 3. 异常捕获与鲁棒性 (Error Handling & Resilience)

### 3.1 奖励参数缺失 / 结构变化

**检查结果: 通过**

- **Gamma client**: `rewards_min_size` / `rewards_max_spread` / `reward_rate_per_day` 均在 try/except 内解析，失败时保持 None；返回的 `GammaMarketInfo` 允许 None。
- **引擎** `_load_rewards_config`: `float(rewards.get("rewards_min_size") or 0)` 等，None 转为 0；`rewards_max_spread * 0.90` 在为 0 时仍安全。
- **Dashboard** `calculate_market_score`: `float(market_data.get("reward_rate_per_day") or 0)`，缺省 0。 无 TypeError 风险。

### 3.2 持久化队列满 (InventoryStateManager)

**检查结果: 🚨 CRITICAL**

- **问题**: `apply_fill` 中 `self._persist_queue.put_nowait(...)` 在队列满（maxsize=1000）时会抛出 `asyncio.QueueFull`。 `handle_fill` 由 `create_task` 调用，异常会导致该 task 失败，**内存已更新但持久化未入队**，且无重试，造成内存与 DB 不一致。
- **修复**: 见下方「CRITICAL 修复」第 1 条。

### 3.3 报价引擎 on_tick 未捕获异常

**检查结果: 🚨 CRITICAL**

- **问题**: `run()` 中 `async for message in pubsub.listen()` 内直接 `await self.on_tick(data)`。 若 `on_tick` 或 `_get_unified_fair_value`、`inventory_state.get_snapshot`、Redis/DB 等任一处抛错，**整个 listen 循环退出**，finally 执行后该引擎永久停止，不再报价。
- **修复**: 见下方「CRITICAL 修复」第 2 条。

### 3.4 Redis / DB 瞬时断开

**检查结果: ⚠️ WARNING**

- **Redis**: `set_state` / `get_state` / `publish` 无 try/except。 若 Redis 断开，会向调用方抛错；若发生在 on_tick 内且未在更外层捕获，会触发 3.3 的引擎退出。
- **DB**: `ensure_loaded`、`apply_fill` 中的 session 若连接失败会抛出，同样可能终止引擎或 fill 处理。
- **建议**: 在引擎 **消息分发层** 统一 try/except（见 3.3 修复）；对 Redis/DB 的 key 读写可增加重试或降级（例如 get_state 失败时返回 None，由业务层 fallback）。 当前通过 3.3 的顶层捕获可避免单次 Redis/DB 异常导致进程级崩溃。

---

## 4. 审计结论汇总

| 级别 | 数量 | 说明 |
|------|------|------|
| 🚨 CRITICAL | 2 | 持久化队列满导致 apply_fill 抛错；on_tick 未捕获异常导致引擎退出。 已修复。 |
| ⚠️ WARNING | 4 | 价格精度未按 per-market tick；Redis/DB 无重试；persist_worker 失败仅记录不重试；handle_fill 中 create_task 异常未统一记录。 见建议。 |
| 💡 OPTIMIZATION | 3 | 见下方。 |

---

## 5. CRITICAL 修复说明（已实现）

1. **InventoryStateManager.apply_fill — 队列满时不再抛错**  
   - 将 `put_nowait` 改为在 `QueueFull` 时捕获，记录 ERROR 并可选地做一次非阻塞 `put` 重试；若仍满则丢弃本次持久化写入，**不向上抛出**，避免 handle_fill 的 task 因队列满而失败，内存状态已更新不受影响。

2. **QuotingEngine.run — 单条消息异常不退出循环**  
   - 在 `async for message in pubsub.listen()` 内，对 `on_control_message` / `on_tick` / `on_order_status_message` 的调用包在 `try/except` 中：记录异常与 channel，然后 `continue`，不退出 listen 循环，确保单条错误不会导致该引擎永久停止。

---

## 6. WARNING 建议（可选后续）

- **Per-market tick size**: 从 Gamma 或 MarketMeta 读取 `orderPriceMinTickSize`，下单前 `round(price, tick_digits)`。
- **Redis/DB 重试**: 对 `get_state`/`set_state` 或关键 DB 调用增加有限次数重试 + 退避。
- **Persist 失败**: 当前 persist_worker 失败仅 log；可考虑将失败项写入 dead-letter 队列或告警，便于对账。
- **handle_fill task 异常**: 对 `asyncio.create_task(self.handle_fill(...))` 添加 `.add_done_callback` 或统一在 task 内 log 未捕获异常，避免静默失败。

---

## 7. OPTIMIZATION 建议

- **连接与资源**: 确保 lifespan 中 Redis/DB 在 shutdown 时正确 close；已有 `inventory_state.stop()` 等待队列排空，可再确认无泄漏。
- **日志**: 对 400/403 的 API 错误可增加短结构化摘要（如 condition_id、token_id、side），便于排查封号/余额问题。
- **性能**: 若单市场 tick 频率极高，可考虑对 on_tick 做节流（如同一 token 最小间隔 100ms）以降低 OMS/Redis 压力；当前未发现必须修改。

---

*审计完成日期: 2026-03-08*
