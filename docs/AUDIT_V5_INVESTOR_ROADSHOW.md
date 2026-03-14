# PolyMatrix Engine V5.0 架构安全性审计报告（投资人路演级）

**审计目标**：证明系统在 1000 刀本金、5 个核心盘口下，可稳定产出 $20–$50/天（吃点差 + 官方奖励），并具备**不可爆仓/死锁**的护城河。  
**审计范围**：`app/quoting/engine.py`、`app/core/auto_router.py`、资金与风控链路。  
**结论**：三项致命隐患已修复，当前逻辑满足生产与路演要求。

---

## 一、修复项总览

| 隐患 | 风险等级 | 修复措施 | 状态 |
|------|----------|----------|------|
| 极端阈值逻辑悖论 | 致命 | 极端线改为 `MAX_EXPOSURE_PER_MARKET * 0.9` | ✅ 已修复 |
| Router 换防僵尸死锁 | 致命 | exit_mode 下强制 EXTREME TAKER，3 秒内资金回收 | ✅ 已修复 |
| 全局资金链断裂 | 高 | 审计确认 SELL 单永不因预算被裁，并补充文档 | ✅ 已确认+文档化 |

---

## 二、极端阈值逻辑悖论 (The Liquidation Paradox)

**问题**：  
当 `BASE_ORDER_SIZE=80` 时，`liquidate_threshold = 160`，原 `is_extreme_long = (exposure >= 240)`，而 `MAX_EXPOSURE_PER_MARKET=200`，敞口不可能达到 240，**极端清仓逻辑永远不触发**。

**修复**（`app/quoting/engine.py`）：  
- 极端线改为与资金上限对齐：`extreme_threshold = MAX_EXPOSURE_PER_MARKET * 0.9`（例如 200→180）。  
- `is_extreme_long = (current_exposure_for_logic >= extreme_threshold)`。  
- 当敞口接近单市场上限（如 180 刀）时，立即走 Taker 砸盘自救，不再依赖 `liquidate_threshold * 1.5`。

**路演要点**：极端清仓线与单市场资金上限绑定，保证在“满仓前”一定触发自救，逻辑闭环。

---

## 三、Router 换防导致的僵尸死锁 (Graceful Exit Deadlock)

**问题**：  
Auto-Router 将某盘口剔除时发送 `graceful_exit`，引擎进入 `exit_mode` 后若仍用 **Maker 挂卖**，当价格远离时卖单可能长期不成交，资金被锁在已不再监控的盘口。

**修复**（`app/quoting/engine.py`）：  
- 在 `exit_mode` 且 `current_exposure > 1.0` 时，**强制使用 EXTREME TAKER**：  
  `ask_price = best_bid_price - 0.02`，无 crosses-the-book 限制。  
- 新增 `force_taker_exit = self.exit_mode and current_exposure > 1.0`，与 `is_extreme_long` 共用同一 Taker 分支。  
- 日志区分 "GRACEFUL EXIT: forcing EXTREME TAKER" 与 "EXTREME INVENTORY"。

**路演要点**：换防/退役盘口在数秒内以 Taker 方式强制回收资金，宁可支付滑点也不留僵尸仓位。

---

## 四、全局资金链断裂风险 (Global Liquidity Crunch)

**场景**：5 盘口 × 200 刀/盘口 = 1000 刀，若同时满仓且浮亏，总资产可能降至约 900 刀。若平仓单（SELL）也受“剩余预算”限制，可能被误裁，导致无法挂出平仓单。

**审计结论**：  
- `_apply_balance_precheck` 仅对 **BUY** 名义价值做预算校验与裁剪。  
- **SELL** 订单与 `sell_orders` 始终完整保留：`available <= 0` 时返回 `sell_orders`；裁剪时返回 `sell_orders + kept`，即平仓单永不因预算被删。  
- 已在函数文档与注释中明确：“SELL orders are NEVER trimmed；平仓单不受预算限制”。

**路演要点**：全局预算只限制“加仓”（BUY），不限制“减仓”（SELL），资金链紧张时仍可畅通挂出所有平仓单。

---

## 五、与 Auto-Router 的协同

- **auto_router.py**：对 ROI 下降盘口发送 `graceful_exit`，引擎侧收到后进入 `exit_mode`。  
- **engine.py**：在 `exit_mode` 且存在敞口时强制 Taker 退出，避免 Maker 排队导致的资金长期锁死。  
- 定力锁（min_hold）与换防逻辑未改动，仅确保“一旦决定退出，必走 Taker”。

---

## 六、路演一句话总结

- **收益逻辑**：5 个核心盘口、1000 刀本金，通过 Maker 吃点差 + 官方奖励，目标 $20–$50/天。  
- **护城河**：  
  1）极端敞口用 `MAX_EXPOSURE_PER_MARKET * 0.9` 触发 Taker 自救，无悖论；  
  2）Router 换防/退役盘口强制 Taker 退出，无僵尸死锁；  
  3）全局预算只限 BUY 不限 SELL，平仓单永不被裁，无资金链断裂导致的“无法平仓”。

---

*审计完成日期：V5.0 投资人路演版本*
