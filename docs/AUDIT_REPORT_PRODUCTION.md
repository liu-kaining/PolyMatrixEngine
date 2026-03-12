# 实盘安全审计报告 (Production Security Audit)

**审计日期**: 2025-03  
**审计范围**: app/core/, app/quoting/, app/market_data/, app/oms/, app/risk/  
**资金规模**: $1000 实盘做市

---

## 执行摘要

| 级别 | 数量 | 状态 |
|------|------|------|
| 🔴 红灯 (致命) | 1 | ✅ 已修复 (user_stream fire-and-forget) |
| 🟡 黄灯 (隐患) | 6 | 3 项已加固，3 项需监控 |
| 🟢 绿灯 (安全) | 8+ | 通过防灾测试 |

**核心结论**:
- `inventory_state` 有 DB 兜底，重启后 `ensure_loaded` 从 DB 恢复；Reconciliation 定期纠正漂移。
- `auto_router.market_start_times` 已通过存量盘口强制注册修复，重启后首轮补齐。
- `user_stream` 的 handle_fill/handle_cancellation 原为 fire-and-forget，异常会静默丢失；已添加 `_safe_create_task` 兜底。
- 其余模块（gateway、watchdog、engine、oms）在异常捕获与日志方面已具备基本防灾能力。

---

## 维度 1：内存状态与重启灾难 (State Persistence & Restart Safety)

### 🔴 红灯 (致命)

#### 1.1 user_stream.handle_fill / handle_cancellation — 静默异常丢失
**文件**: `app/market_data/user_stream.py` L179, L185, L194  
**问题**: `asyncio.create_task(self.handle_fill(...))` 和 `handle_cancellation` 为 fire-and-forget，异常不会被主循环捕获，导致成交/取消事件处理失败时静默死亡，库存与 OrderJournal 脱节。  
**修复**: 为 create_task 添加 done_callback，在异常时打 CRITICAL 日志并可选告警。

#### 1.2 inventory_state 持久化队列满时状态漂移
**文件**: `app/core/inventory_state.py` L201-204  
**问题**: `put_nowait` 在队列满时 `QueueFull`，内存已更新但 DB 未持久化。重启后 `ensure_loaded` 从 DB 加载，得到旧数据，导致库存漂移。  
**现状**: 已有 ERROR 日志；Reconciliation 会最终纠正，但存在时间窗口。  
**建议**: 黄灯，监控队列使用率；若长期满，需扩容或降频。

### 🟡 黄灯 (隐患)

#### 1.3 gateway.subscribed_markets / user_stream.subscribed_markets
**文件**: `app/market_data/gateway.py` L96, `user_stream.py` L21  
**问题**: 纯内存，重启后清空。当前流程依赖 `start_market_making_impl` 每次启动时调用 subscribe，故正常路径下可恢复。若未来有“从 DB 恢复存量市场”的启动路径，需显式恢复订阅。  
**结论**: 当前通过防灾测试，需在新增恢复逻辑时补全。

#### 1.4 market_lifecycle.engine_tasks
**文件**: `app/core/market_lifecycle.py` L28  
**问题**: 纯内存，重启后为空。Auto-Router 会重新扫描并启动，不会自动恢复旧任务。  
**结论**: 设计如此，非 Bug。重启后由 Auto-Router 按 Top N 重新选市。

#### 1.5 auto_router.market_start_times
**文件**: `app/core/auto_router.py` L21  
**问题**: 已通过存量盘口强制注册修复，重启后首轮 _rebalance 会补齐缺失时间戳。  
**结论**: ✅ 该模块通过防灾测试。

---

## 维度 2：异步并发与静默死亡 (Async Concurrency & Silent Failures)

### 🔴 红灯 (致命)

#### 2.1 user_stream fire-and-forget 任务无异常兜底
**文件**: `app/market_data/user_stream.py` L179, L185, L194  
**问题**: 与 1.1 相同。`handle_fill` / `handle_cancellation` 若抛异常，主 _listen 循环不受影响，但成交/取消逻辑中断，库存与链上不一致。  
**修复**: 见下方极简修复代码。

### 🟡 黄灯 (隐患)

#### 2.2 watchdog.reconciliation_loop 独立任务
**文件**: `app/risk/watchdog.py` L30  
**问题**: `asyncio.create_task(self.reconciliation_loop())` 无人 await，若循环内未捕获异常会静默退出。  
**现状**: 内部有 `except Exception`，仅记录日志，不会崩溃。  
**结论**: 通过防灾测试。

#### 2.3 market_lifecycle._mark_market_exited
**文件**: `app/core/market_lifecycle.py` L141  
**问题**: `asyncio.create_task(_mark_market_exited(cid))` 无人 await。  
**影响**: 若更新 DB 失败，MarketMeta.status 可能未置为 exited，影响展示，不影响资金。  
**结论**: 黄灯，建议在 _mark_market_exited 内加强异常日志。

#### 2.4 user_stream / gateway _listen 循环
**文件**: `user_stream.py` L116-151, `gateway.py` L233-278  
**问题**: 均有 `try/except`，TimeoutError / ConnectionClosed / Exception 会 raise 触发重连。  
**结论**: ✅ 通过防灾测试。

---

## 维度 3：浮点数精度与订单幽灵 (Float Precision & Ghost Orders)

### 🟢 绿灯 (安全)

#### 3.1 user_stream 部分成交判断
**文件**: `app/market_data/user_stream.py` L250  
**代码**: `if new_total_filled >= original_size - 1e-6`  
**结论**: ✅ 正确使用 epsilon 避免浮点误差。

#### 3.2 engine 订单尺寸与阈值
**文件**: `app/quoting/engine.py` L534, L537, L618  
**结论**: 使用 `>=` / `<=` 与阈值比较，无直接 `==` 浮点相等，可接受。

### 🟡 黄灯 (低风险)

#### 3.3 gateway LocalOrderbook size==0
**文件**: `app/market_data/gateway.py` L65  
**代码**: `if size == 0:` 用于 price_change 删除档位。  
**风险**: 若 API 返回极小数 (如 1e-10) 而非 0，可能误保留档位。  
**结论**: 低优先级，Polymarket 通常返回 0 或正数。

---

## 维度 4：做市奖励防断签 (Rewards Eligibility Continuity)

### 🟡 黄灯 (隐患)

#### 4.1 sync_orders_diff Cancel → Create 空窗期
**文件**: `app/quoting/engine.py` L856-872  
**问题**: 先 `cancel_order` 再 `place_orders`，中间存在网络 RTT 空窗，可能短暂无挂单。  
**现状**: 单次 diff 通常只 cancel 少量 stale，create 补足，空窗约 100–300ms。  
**建议**: 若交易所对连续挂单要求极严，可考虑 Cancel-Replace 原子化（若 API 支持）。

#### 4.2 rewards 尺寸与 spread 校验
**文件**: `app/quoting/engine.py` L639-650  
**结论**: 已校验 `rewards_min_size` 与 `rewards_max_spread`，并发布 `rewards_eligible`。✅

---

## 极简修复代码 (仅红灯项)

### 修复 1: user_stream fire-and-forget 异常兜底

**文件**: `app/market_data/user_stream.py`

在 `_process_single_event` 中，将 `asyncio.create_task(...)` 包装为带异常回调的调用：

```python
def _safe_create_task(coro):
    task = asyncio.create_task(coro)
    def _done(t):
        try:
            t.result()
        except Exception as e:
            logger.exception("User WS fire-and-forget task failed: %s", e)
    task.add_done_callback(_done)
    return task
```

然后将 L179, L185, L194 的 `asyncio.create_task(...)` 替换为 `_safe_create_task(...)`。

**状态**: ✅ 已实施

---

### 修复 2: gateway LocalOrderbook 浮点档位删除 (黄灯)

**文件**: `app/market_data/gateway.py` L65  
**修改**: `if size == 0:` → `if abs(size) < 1e-9:`  
**状态**: ✅ 已实施

---

### 修复 3: market_lifecycle._mark_market_exited 异常日志增强 (黄灯)

**文件**: `app/core/market_lifecycle.py` L56  
**修改**: `logger.error` → `logger.exception` 以输出堆栈  
**状态**: ✅ 已实施
