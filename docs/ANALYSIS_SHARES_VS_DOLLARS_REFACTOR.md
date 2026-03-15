# 份额 (Shares) 与 美金 (Notional/Dollars) 度量衡修复 — 分析报告

**目标**：在修改代码前，明确状态拆解、逻辑纠正与配置语义，保证风控与资金计算逻辑自洽。

---

## 一、业务约定（Polymarket）

| 概念 | 含义 | 单位 | 示例 |
|------|------|------|------|
| **size** | 订单/持仓的份额数 | Shares | 100 |
| **price** | 单价 | $ [0.01, 0.99] | 0.52 |
| **notional** | 占用的美金 | Dollars | size × price = 52 |
| **exposure** | 当前持仓份额（yes_exposure / no_exposure） | Shares | 来自成交累计 |
| **GLOBAL_MAX_BUDGET** | 全系统允许占用的美金上限 | Dollars | 1000 |

**核心结论**：  
- 持仓与挂单的“占用本金”必须用 **Dollars** 与 `GLOBAL_MAX_BUDGET` / 单市场预算比较。  
- 当前代码把 **Shares** 和 **Dollars** 直接相加，导致与“美金预算”比对时出现严重度量衡错误。

---

## 二、状态拆解：如何干净地追踪 Shares 与 Used Dollars

### 2.1 当前状态

- **DB / 内存**：`yes_exposure`、`no_exposure` 为 **Shares**（由 `apply_fill` 用 `filled_size` 累加）。  
- **内存**：`pending_yes_buy_notional`、`pending_no_buy_notional` 为 **Dollars**（挂单 price×size 之和）。  
- **缺失**：没有“当前持仓对应的占用美金（已花费本金）”的权威来源。

### 2.2 方案对比

| 方案 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A. 用 Mid 估算** | `used_dollars ≈ shares × mid_price`，在需要时由调用方传入 mid 或从 Redis 取 | 不改 DB，不改 apply_fill | 需在 watchdog/engine 等处传 mid；watchdog 需能拿到各市场 mid；估算值非真实成本 |
| **B. 在状态中维护“占用美金”** | 在 inventory 中增加持仓成本（例如 cost basis），在 apply_fill 中按成交更新 | 与 GLOBAL_MAX_BUDGET / 单市场预算可比、语义一致；watchdog 无需 mid | 需改 inventory_state + DB schema（如新增字段）+ 迁移 |

### 2.3 推荐：方案 B（在状态中维护“占用美金”）

- **语义**：风控和预算比较的应是“已经占用的美金”，即 **成本口径**，而不是“按市价估的市值”。  
- **实现建议**：  
  - 在 **内存 snapshot** 与 **DB（InventoryLedger）** 中增加：  
    - `yes_capital_used`：当前 YES 持仓对应的占用美金（成本基准确认前可用“买入侧已花费”近似）。  
    - `no_capital_used`：当前 NO 持仓对应的占用美金。  
  - 在 **apply_fill** 中：  
    - **BUY**：`yes/no_capital_used += fill_price * filled_size`，`yes/no_exposure += filled_size`。  
    - **SELL**：按成本减少占用美金（见下），并减少 exposure。  

**成本减少方式（二选一，推荐简单版）**：  
- **简单版（按平均成本）**：  
  - 维护 `yes_capital_used` / `no_capital_used` 与 `yes_exposure` / `no_exposure`。  
  - SELL 时：`capital_used -= (capital_used / exposure) * filled_size`，再 `exposure -= filled_size`（注意 exposure→0 时置 0）。  
- **精确版**：按 FIFO 成本结转；实现复杂，收益有限，建议先用简单版。

这样：  
- **持仓份额**：仍由 `yes_exposure` / `no_exposure`（Shares）唯一表示。  
- **占用美金**：由 `yes_capital_used` + `no_capital_used` + `pending_yes_buy_notional` + `pending_no_buy_notional` 表示，全部为 **Dollars**，可与 `GLOBAL_MAX_BUDGET` 和单市场美金上限直接比较。

若暂不落库（避免迁移），可先在 **inventory_state 内存** 中增加 `yes_capital_used` / `no_capital_used`，在 **apply_fill** 和 **ensure_loaded**（从 DB 加载时若没有历史成本则用 0，或一次性地用当前 exposure 与某默认价估算初始化）中维护；**get_global_exposure** 只做“美金之和”。DB 与迁移可放在第二步。

---

## 三、逻辑纠正：正确公式推导

### 3.1 全局“已占用美金”（应与 GLOBAL_MAX_BUDGET 比较）

**目标**：一个数，单位 **Dollars**，表示“所有市场当前占用的美金”，用于和 `GLOBAL_MAX_BUDGET` 比较。

**正确公式**：

```text
global_used_dollars =
  Σ over all markets (
    yes_capital_used(m) + no_capital_used(m)
    + pending_yes_buy_notional(m) + pending_no_buy_notional(m)
  )
```

- 若尚未引入 `yes_capital_used` / `no_capital_used`，**临时**可用“份额×当前 mid”估算该市场占用美金，但必须保证 **不入账份额**（即不能把 shares 和 dollars 加在一起）。

**修正后的 `get_global_exposure` / `get_global_exposure_excluding`**：  
- 语义改为 **get_global_used_dollars**（及 excluding 版本）。  
- 返回值 = 上述 `global_used_dollars`（或排除某 market 后的和），**单位统一为 Dollars**。  
- 调用方（watchdog、engine）一律按“美金”使用，与 `GLOBAL_MAX_BUDGET` 比较。

### 3.2 单市场“已占用美金”（应与 MAX_EXPOSURE_PER_MARKET 比较，见第四节）

对单个市场 `m`：

```text
local_used_dollars(m) =
  yes_capital_used(m) + no_capital_used(m)
  + pending_yes_buy_notional(m) + pending_no_buy_notional(m)
```

单位：**Dollars**。

### 3.3 `_apply_balance_precheck` 的正确逻辑（全 Dollars）

**输入（建议）**：  
- `orders_payload`：本批订单。  
- `local_used_dollars`：**本市场**当前已占用美金（上面公式，来自 snapshot + pending）。  
- `global_other_markets_dollars`：**除本市场外**的全局已占用美金（来自 `get_global_exposure_excluding`，且该函数已改为返回 Dollars）。

**约束**：  
- 本市场：`local_used_dollars + 本批 BUY 总 notional ≤ MAX_EXPOSURE_PER_MARKET`（若配置为美金，见下）。  
- 全局：`global_other_markets_dollars + local_used_dollars + 本批 BUY 总 notional ≤ GLOBAL_MAX_BUDGET`。

**可用预算（Dollars）**：

```text
local_available  = max(0, MAX_EXPOSURE_PER_MARKET - local_used_dollars)
global_available = max(0, GLOBAL_MAX_BUDGET - global_other_markets_dollars - local_used_dollars)
available        = min(local_available, global_available)
```

- 仅对 **BUY** 订单：若 `total_buy_notional > available`，则按现有策略缩量或砍档，使 `sum(price×size)` ≤ available。  
- **SELL** 订单不参与预算扣减，始终放行。

**当前 BUG**：  
- `local_used = current_exposure + opposite_exposure + other_side_pending` 中，前两项为 **Shares**，第三项为 **Dollars**，相加无意义。  
- 修正：不再使用“exposure 份额”直接加；改为传入或计算 **local_used_dollars**（以及 **global_other_markets_dollars**），全部为美金。

---

## 四、配置澄清：MAX_EXPOSURE_PER_MARKET 的语义与落地

### 4.1 建议：统一为“最大允许占用美金”

- **定义**：`MAX_EXPOSURE_PER_MARKET` = 单市场允许占用的 **美金上限**（与 `GLOBAL_MAX_BUDGET` 同量纲）。  
- **比较对象**：单市场 `local_used_dollars`（yes_capital_used + no_capital_used + pending notional）。  
- **效果**：  
  - 风控、预算预检、日志中的“敞口/占用”全部用 **Dollars**，不再出现“份额与美金混比”的情况。  
  - 配置含义清晰：例如 200 表示“单市场最多占用 200 美金”。

### 4.2 代码落地建议

| 位置 | 当前问题 | 修正方式 |
|------|----------|----------|
| **watchdog** | 用 `yes_exp` / `no_exp`（Shares）与 `max_exposure`（若为美金）比较 | 改为：取该市场 `local_used_dollars`（或 yes_capital_used + no_capital_used），与 `MAX_EXPOSURE_PER_MARKET`（美金）比较；可选保留“单边份额过大”的辅助告警（需另定义阈值，例如份额上限）。 |
| **engine：extreme_threshold** | `extreme_threshold = MAX_EXPOSURE_PER_MARKET * 0.9`，再与 `current_exposure_for_logic`（Shares）比较 | 改为：`extreme_threshold_dollars = MAX_EXPOSURE_PER_MARKET * 0.9`，与 **本市场当前占用美金** 比较（例如本市场 `local_used_dollars`），而不是与份额比较。 |
| **engine：liquidate_threshold** | 当前为 `base_size * 2.0`（份额），用于“何时开始挂卖单/双向报价” | 可保留为 **份额** 阈值（表示“持仓达到多少 shares 就参与卖侧”），与 MAX_EXPOSURE_PER_MARKET（美金）分开：一个管“行为切换”（份额），一个管“风险熔断”（美金）。 |
| **.env 文档** | 未明确单位 | 在 README / .env.example 中写明：`MAX_EXPOSURE_PER_MARKET` = 单市场最大占用 **美金**；`GLOBAL_MAX_BUDGET` = 全局最大占用 **美金**。 |

---

## 五、修复项与依赖关系小结

1. **inventory_state**  
   - 增加 `yes_capital_used` / `no_capital_used`（Dollars），在 **apply_fill** 中按买卖更新（SELL 用平均成本减少）。  
   - **ensure_loaded** / 从 DB 加载：若 DB 暂无这两列，可先填 0；后续可通过迁移增加 DB 列并回填。  
   - **get_global_exposure** → 改为 **get_global_used_dollars**：只加 (yes_capital_used + no_capital_used + pending_yes_buy_notional + pending_no_buy_notional) 每市场，返回 **Dollars**。  
   - **get_global_exposure_excluding(market_id)**：同上，但排除指定 market，返回 **Dollars**。

2. **engine**  
   - 调用 **get_global_exposure_excluding** 得到 `global_other_markets_dollars`。  
   - 从 snapshot 取本市场 `yes_capital_used`、`no_capital_used`，加上 pending，得到 **local_used_dollars**。  
   - ** _apply_balance_precheck** 签名改为接收 `local_used_dollars` 与 `global_other_markets_dollars`（均为 Dollars），内部全部按美金计算 available 并只对 BUY 做裁剪。  
   - **extreme_threshold**：与 **本市场 local_used_dollars** 比较（或与 `extreme_threshold_dollars = MAX_EXPOSURE_PER_MARKET * 0.9` 比较），不再与 shares 比较。

3. **watchdog**  
   - 每市场用 **local_used_dollars**（或 yes_capital_used + no_capital_used + pending）与 `MAX_EXPOSURE_PER_MARKET` 比较；全局用 **get_global_used_dollars()** 与 `GLOBAL_MAX_BUDGET` 比较。

4. **配置与文档**  
   - 明确：`MAX_EXPOSURE_PER_MARKET` = 单市场最大 **美金**；`GLOBAL_MAX_BUDGET` = 全局最大 **美金**；在代码注释与 README/.env.example 中写明。

---

## 六、结论与下一步

- **状态**：持仓仍以 **Shares**（yes_exposure / no_exposure）为主；**占用美金** 由新增的 capital_used（及现有 pending notional）表示，全部 **Dollars**。  
- **逻辑**：全局与单市场预算、熔断、预检一律使用 **Dollars**；`get_global_exposure` 与 `_apply_balance_precheck` 按上述公式修正，不再混合份额与美金。  
- **配置**：`MAX_EXPOSURE_PER_MARKET` 与 `GLOBAL_MAX_BUDGET` 统一为 **美金**，并在文档与代码中固定语义。

确认该方案后，可按上述顺序实现代码修改（先 inventory_state + engine 预检 + extreme 逻辑，再 watchdog，最后配置与文档）。
