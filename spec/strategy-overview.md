# PolyMatrix Engine 策略总览

> 本文档整理系统内涉及**订单**（下单、撤单、风控）与**选市场**的全部策略逻辑，便于与资深交易/量化人员沟通优化思路。
>
> **当前与代码一致**：选市场仅用多因子机会分数排序 + 手动点选，无 AI/自动选；排序公式见 4.4。

---

## 一、订单策略（Order Strategy）

### 1.1 触发时机（When to Act）

| 触发源 | 条件 | 动作 |
|--------|------|------|
| **Tick 推送** | 收到 Redis `tick:{token_id}` 的 orderbook 更新 | 进入 `on_tick()`，计算 Fair Value 与网格 |
| **Debounce** | `\|fair_value - last_anchor_mid_price\| <= QUOTE_PRICE_OFFSET_THRESHOLD` | **跳过**本次网格重置，减少无效撤单/下单 |
| **控制信号** | Redis `control:{condition_id}` 收到 `suspend` | 立即 `cancel_all_orders()`，停止响应 tick |
| **控制信号** | 收到 `resume` | 恢复响应 tick |

**Debounce 逻辑**：只有当 Fair Value 相对上次锚点移动超过 `QUOTE_PRICE_OFFSET_THRESHOLD`（默认 0.01）时，才执行「撤全部 → 下新单」。否则忽略本次 tick，避免频繁刷新。

---

### 1.2 定价逻辑（Pricing）

#### 1.2.1 Fair Value 计算（AlphaModel）

```
mid_price = (best_bid + best_ask) / 2

OBI = (best_bid_size - best_ask_size) / (best_bid_size + best_ask_size)   # 订单簿失衡 [-1, +1]
obi_skew = OBI * 0.015   # 买盘强则 fair_value 上移

inv_skew = -current_exposure * 0.0005   # 持有多头则 fair_value 下移（鼓励卖出）

fair_value = clamp(mid_price + obi_skew + inv_skew, 0.01, 0.99)
```

#### 1.2.2 Dynamic Spread

```
dynamic_spread = QUOTE_BASE_SPREAD * (1 + |OBI|)
```

- 失衡越大，spread 越宽，防御单向流动
- 默认 `QUOTE_BASE_SPREAD = 0.02`

#### 1.2.3 网格档位

```
anchor_distance = dynamic_spread / 2
bid_1 = fair_value - anchor_distance
ask_1 = fair_value + anchor_distance

# 各档：bid_1, bid_1 - 0.01, bid_1 - 0.02, ...（共 GRID_LEVELS 档）
```

---

### 1.3 下单模式（Two Modes）

#### Mode A：NEUTRAL_ACCUMULATE（轻仓/空仓，exposure < 5）

- **只挂 BUY**，不挂 SELL（避免资金锁在卖单上）
- **策略**：少而精，只在 fair_value 下方挂买单，等别人卖给我们
- **第一档特殊逻辑**（`QUOTE_BID_ONE_TICK_BELOW_TOUCH=True`）：
  - 第一档 bid 至少为 `best_bid - 0.01`（最多比 touch 低 1 tick）
  - 目的：更容易被卖单打到，仍保留约 1¢ edge
- **档位**：`bid_1`, `bid_1 - 0.01`, `bid_1 - 0.02`, ... 共 `GRID_LEVELS` 档
- **单笔 size**：`BASE_ORDER_SIZE`（默认 10，最低 5）

#### Mode B：LIQUIDATE_LONG（多头过重，exposure >= 5）

- **只挂 SELL**，不再挂 BUY
- **定价**：`ask_price = min(fair_value + 0.01, best_ask - 0.01)`，尽量贴近 best ask 以快速成交
- **size**：`min(current_exposure, max(BASE_ORDER_SIZE, 5))`，不超卖

---

### 1.4 原子更新（Atomic Grid Refresh）

每次 tick 触发网格更新时：

1. **先** `cancel_all_orders()`：撤掉当前 `active_orders` 中全部订单
2. **再** `place_orders(orders_payload)`：并发下新单

保证同一时刻不会有「旧单 + 新单」并存，避免孤儿单。

---

### 1.5 撤单策略（Cancel Strategy）

| 场景 | 动作 |
|------|------|
| **网格刷新** | 撤掉本 token 全部 `active_orders` |
| **Kill Switch** | `cancel_market_orders(condition_id)`：撤掉该 market 下所有 OPEN/PENDING 订单 |
| **撤单后** | 调用 `client.get_order(order_id)` 检查 `size_matched`，记录部分成交情况到 payload |

---

## 二、风控与 Kill Switch

### 2.1 敞口监控（Watchdog）

- **周期**：每 1 秒检查一次
- **条件**：`|yes_exposure| > MAX_EXPOSURE_PER_MARKET` 或 `|no_exposure| > MAX_EXPOSURE_PER_MARKET`
- **动作**：`trigger_kill_switch(condition_id)`：
  1. 将 `MarketMeta.status` 设为 `suspended`
  2. 发布 Redis `control:{condition_id}` → `{"action": "suspend"}`
  3. 调用 `oms.cancel_market_orders(condition_id)`

### 2.2 敞口来源

- **InventoryLedger**：由 UserStream 的 `handle_fill` 实时更新
- **Reconciliation**：每 5 分钟从 Polymarket Data API 拉取真实持仓，与 DB 对账，若偏差 > 1 USDC 则覆盖 DB

### 2.3 用户层 Kill Switch

- **Stop**：suspend + cancel，不主动平仓
- **Liquidate**：suspend + cancel + 在 0.01 价位挂 SELL 单清空 Yes/No 敞口（市价平仓）

---

## 三、成交与库存更新

### 3.1 成交来源

- **UserStream**：订阅 Polymarket User WS，接收 `trade` 事件（MATCHED）
- **Maker 成交**：遍历 `maker_orders`，对每个 `order_id` 调用 `handle_fill(order_id, matched_amount, price)`
- **Taker 成交**：若有 `taker_order_id`，同样调用 `handle_fill`

### 3.2 handle_fill 逻辑

1. 更新 `OrderJournal.payload["filled_size"]`，累计部分成交
2. 若 `filled_size >= original_size`，状态改为 FILLED
3. 根据 `MarketMeta` 判断 YES/NO，更新 `InventoryLedger.yes_exposure` 或 `no_exposure`
4. SELL 时累加 `realized_pnl`

---

## 四、选市场策略（Market Selection）

### 4.1 数据源与拉取

- **Gamma API**：`https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=1000&offset={offset}`
- **拉取方式**：异步并发（`httpx.AsyncClient` + `asyncio.Semaphore(5)`），按页 1000 分页，单次失败仅跳过该 offset 并合并已拉取数据
- **上限**：`MAX_MARKETS`（默认 50000，可配 `GAMMA_MAX_MARKETS`）；某页返回 < 1000 条即停止
- **缓存**：`@st.cache_data(ttl=300)` 缓存 5 分钟，避免频繁刷新轰炸 API
- **过滤**：仅保留二元市场（outcomes = {yes, no}），排除体育、博彩类

### 4.2 筛选模式（Screener Mode）

| 模式 | min_dte | min_vol | min_liq | YES 价格区间 |
|------|---------|---------|---------|--------------|
| Conservative | 7 天 | 50k | 10k | [0.25, 0.75] |
| Normal | 3 天 | 10k | 3k | [0.20, 0.80] |
| Aggressive | 1 天 | 1k | 500 | [0.10, 0.90] |
| Ultra | 1 天 | 0 | 0 | [0, 1] |

### 4.3 黑名单（Question Blacklist）

- 体育：sports, nfl, nba, soccer, football, ...
- 博彩：win the match, halftime, in-play, live betting, ...
- 事件驱动：up or down, strikes by, one day after launch, one week after

### 4.4 多因子机会分数（Opportunity Score）与排序

**审计现状（原逻辑）**：过滤后按 `(recommendation_score, volume24hr)` 降序。原 recommendation_score = fill_score + risk_score + opp_score（线性 vol/liq 上限、DTE、价格区间、赛道加分、相对 vol×liq）。

**当前逻辑**：`calculate_opportunity_score(market)` 多因子打分，再按该分数降序：

```
liq_vol = log10(volume+1)*0.4 + log10(liquidity+1)*0.4   # 对数防单一天量扭曲
liq_vol_scaled = liq_vol * 15
price_penalty = |yes_price - 0.5| * 20                  # 越贴近 0.5 越好
category_bonus = 5 if ⭐ Politics/Culture else 0   # 小幅加分，避免 Politics 霸榜
score = liq_vol_scaled - price_penalty + category_bonus
if yes_price < 0.15 or yes_price > 0.85: score -= 50   # 极值死刑，沉底
recommendation_score = score
stars = 1 + int(score/20),  capped [1,5]
```

- **排序**：按 `(recommendation_score, volume24hr)` 降序
- **默认展示**：4 星及以上（可关闭过滤看全部）

### 4.5 选市场

- 列表按 `(recommendation_score, volume24hr)` 降序，默认选中第一行
- 用户可点击表格行末 ✓ 手动切换选中市场

---

## 五、可配置参数（.env）

| 参数 | 默认 | 说明 |
|------|------|------|
| MAX_EXPOSURE_PER_MARKET | 50 | 单市场敞口上限，超则 Kill Switch |
| BASE_ORDER_SIZE | 10 | 单笔订单 size（USDC） |
| GRID_LEVELS | 2 | 每侧网格档数 |
| QUOTE_BASE_SPREAD | 0.02 | 基础 spread，影响 bid/ask 距 fair_value 的距离 |
| QUOTE_PRICE_OFFSET_THRESHOLD | 0.01 | Fair Value 移动超过此值才刷新网格 |
| QUOTE_BID_ONE_TICK_BELOW_TOUCH | true | 第一档 bid 是否允许最多比 best_bid 低 1 tick |

---

## 六、可优化方向（供讨论）

1. **Fair Value**：当前仅用 orderbook mid + OBI + inventory，可引入外部 alpha（AI、体育盘口、新闻等）
2. **Spread**：动态 spread 公式可调，或按波动率/流动性自适应
3. **档位与 size**：GRID_LEVELS、BASE_ORDER_SIZE 与流动性、资金规模的关系
4. **Mode 切换阈值**：当前 exposure >= 5 即切 LIQUIDATE，可改为可配置或更细粒度
5. **选市场**：评分公式权重、黑名单、DTE 下限、事件盘识别
6. **Kill Switch 后**：是否自动 resume、何时可重新开仓
7. **Liquidate 定价**：当前 0.01 砸盘，可考虑更优的 taker 策略
