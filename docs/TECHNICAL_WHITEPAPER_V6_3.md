# PolyMatrixEngine V6.3 Technical Whitepaper

## Executive summary

PolyMatrixEngine is a maker-first Polymarket execution system optimized for:
- **Capital velocity**: prioritize active, deep-liquidity markets where maker spreads and liquidity rewards are realizable.
- **Risk containment**: strict per-market and portfolio-level constraints; explicit avoidance of binary-resolution toxic flow.
- **Operational robustness**: memory-first hot path, reconciliation safeguards, and hard resets for WebSocket-induced state drift.

V6.3 introduces (and/or hardens) three pillars:
- **AutoRouter V6.3**: liquidity hard filters + time-to-resolution decay to avoid long-dated stagnant markets.
- **Rewards-eligibility discipline**: spread clamped *within* reward brackets with safety margin.
- **State correctness**: eliminate phantom budgets from ghost orders and reconciliation capital leaks; remove unit mismatches.

---

## System overview

### Core components
- **Market Data Gateway** (`app/market_data/gateway.py`): consumes Market WS + REST snapshot, publishes `tick:{token}` and `ob:{token}` to Redis.
- **User Stream** (`app/market_data/user_stream.py`): consumes user WS events, updates order journal and inventory state, publishes `order_status:*`.
- **Inventory State Manager** (`app/core/inventory_state.py`): in-memory source of truth for exposures and capital usage; async DB persist queue.
- **Quoting Engine** (`app/quoting/engine.py`): maker-only grid quoting with diff-quote reconciliation and budget precheck.
- **Risk Watchdog** (`app/risk/watchdog.py`): hard kill switch on per-market limit breach and periodic reconciliation via Polymarket Data API.
- **AutoRouter** (`app/core/auto_router.py`): portfolio scheduler selecting which markets to run.

### Data flow (control plane + hot path)
1. Gateway publishes ticks → engines consume ticks.
2. Engines compute fair value and desired grid → OMS place/cancel (diff quoting).
3. Fills update inventory in memory immediately → DB persist occurs asynchronously.
4. Watchdog monitors risk and performs periodic reconciliation with a time buffer.
5. AutoRouter rotates active markets via Redis control messages (`graceful_exit`, `suspend`, etc.).

---

## Risk and accounting primitives (units matter)

### Key state variables
- **Exposure** (`yes_exposure`, `no_exposure`): **shares**.
- **Capital used** (`yes_capital_used`, `no_capital_used`): **USD notional spent** (approx. \(\sum price \times size\) on BUY fills, reduced on SELL by average cost logic).
- **Pending buy notional** (`pending_yes_buy_notional`, `pending_no_buy_notional`): **USD notional locked** by resting BUY orders.

### Budgeting goals
1. **Per-market cap**: `MAX_EXPOSURE_PER_MARKET` (USD) is the hard risk line.
2. **Global cap**: `GLOBAL_MAX_BUDGET` (USD) across all active markets.
3. **Portfolio concentration controls**:
   - `MAX_EXPOSURE_PER_SECTOR` (USD) per tag/category.
   - `MAX_SLOTS_PER_SECTOR` number of markets per tag/category.

**Principle**: comparisons must be unit-consistent. Shares-based exposure cannot be directly compared to USD thresholds.

---

## AutoRouter V6.3 (selection + eviction)

### Goals
- Avoid over-concentration in correlated events.
- Avoid binary resolution windows and expired markets (toxic flow).
- Prefer deep-liquidity mid-term markets (7–90 days) for realizable APY.

### Event-horizon halts
AutoRouter treats markets as disallowed if:
- end date is within `EVENT_HORIZON_HOURS` **or**
- end date has already passed.

This prevents the system from being trapped in resolution-adjacent toxic flow and reduces tail risk.

### Sector/tag concentration limits
AutoRouter tracks sector exposure/slots and skips candidates that would breach:
- `MAX_SLOTS_PER_SECTOR`
- `MAX_EXPOSURE_PER_SECTOR`

Markets missing tags are treated as independent sectors using a per-market fallback key to avoid tag-collision.

### Scoring model (V6.3)
AutoRouter ranks markets by:
- **Liquidity hard filter**: `liquidity < 20000.0` → skip.
- **Base score**:

\[
score_{base} = daily\_roi \times \log_{10}(liquidity)
\]

- **Time decay** computed from days-to-resolution (missing end date treated as long-dated):
  - \(days\_left > 180\): multiply by `0.01`
  - \(90 < days\_left \le 180\): multiply by `0.5`
  - \(days\_left \le 90\): multiply by `1.0`

This forces selection toward deep and mid-term markets, reducing “theoretical ROI but dead volume” bias.

---

## QuotingEngine: rewards eligibility + maker-only safety

### Rewards bracket compliance (Auto-Tune spread)
When rewards are available (`rewards_min_size`, `rewards_max_spread`), V6.3 clamps spread to stay inside the bracket:
- `target_spread = rewards_max_spread * 0.95` (safety margin)
- `dynamic_spread = min(max(dynamic_spread, base_spread), target_spread)`

This prevents quoting outside reward constraints and forfeiting rewards.

### Maker-only safeguards at extremes
Two edge-case traps are explicitly handled:
- **Buy 1-cent trap**: if `best_ask` is at floor and maker-safe `best_ask - tick` drops below 0.01, BUY order is skipped.
- **Sell 99-cent trap**: if maker-safe `best_bid + tick` requires price > 0.99, maker sell is blocked and the order is skipped.

These avoid accidentally crossing the book and becoming taker at boundary prices.

---

## Ghost orders and budget hallucinations

### Problem
WebSocket drops or OMS cancel failures can cause `active_orders` to retain phantom orders, inflating pending notional and producing “budget exhausted” hallucinations.

### Controls
- **Periodic hard reset (TTL)**: every 5 minutes, engines cancel all orders and skip the tick to rebuild cleanly next tick.
- **Force eviction on hard reset**: cancel failures are force-evicted from the local `active_orders` cache to free pending-notional budget.

This is a pragmatic operational trade-off: the source of truth is the exchange; local caches must not permanently lock budget.

---

## Watchdog: kill-switch semantics and reconciliation

### Kill switch trigger (hair-trigger fix)
Hard kill switch decisions are based on **actual spent capital only**:
- `actual_used_dollars = yes_capital_used + no_capital_used`

Pending notional is excluded from the suspend decision to avoid routine quoting suspending markets.

### Reconciliation correctness (capital leak fixes)
Reconciliation updates:
- exposures (`yes_exposure`, `no_exposure`)
- capital used (`yes_capital_used`, `no_capital_used`), including:
  - **zero-out** when exposure is effectively closed
  - **proportional scaling** when exposure is partially reduced

In-memory state is explicitly updated with reconciled capital-used values to prevent restart-only correction.

---

## Configuration reference (V6.3)

Key parameters:
- **AutoRouter**
  - `AUTO_ROUTER_ENABLED`
  - `AUTO_ROUTER_MAX_MARKETS`
  - `AUTO_ROUTER_SCAN_INTERVAL_SEC`
  - `AUTO_ROUTER_MIN_HOLD_HOURS`
  - `EVENT_HORIZON_HOURS`
  - `MAX_EXPOSURE_PER_SECTOR`
  - `MAX_SLOTS_PER_SECTOR`
- **Risk**
  - `MAX_EXPOSURE_PER_MARKET`
  - `GLOBAL_MAX_BUDGET`
  - `EXPOSURE_TOLERANCE`
  - `RECONCILIATION_BUFFER_SECONDS`
- **Quoting**
  - `BASE_ORDER_SIZE`
  - `GRID_LEVELS`
  - `QUOTE_BASE_SPREAD`
  - `QUOTE_PRICE_OFFSET_THRESHOLD`
  - `AUTO_TUNE_FOR_REWARDS`

---

## Known limitations and future work
- The scoring model is intentionally simple and transparent; adding orderbook-based toxicity metrics (spread, skew, depth) can further reduce adverse selection.
- Sector exposure is approximated from `MAX_EXPOSURE_PER_MARKET` when admitting new markets; a tighter estimator can incorporate current open orders and recent fill rates.
- No global kill switch is triggered on `GLOBAL_MAX_BUDGET` breach (currently logged); consider an explicit portfolio-wide reduce-only mode under severe stress.

