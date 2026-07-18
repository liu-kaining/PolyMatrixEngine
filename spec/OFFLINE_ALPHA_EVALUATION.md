# Offline Alpha Evaluation Runbook

> This workflow is strictly local/offline. It does not start the API, connect to Polymarket, derive exchange credentials, submit orders, or perform a live probe. A generated report is evidence input, not permission to trade.

## Current decision

The repository contains the evaluator and the live evidence gate, but it does not contain a qualifying historical dataset. Therefore the current live decision remains **NO-GO**. `spec/alpha_evidence.example.json` is deliberately invalid and must never be armed.

## 1. Export the exact runtime strategy configuration

Set the intended non-secret strategy/risk environment values, then export their canonical representation:

```bash
python -m app.core.strategy_fingerprint \
  --output data/alpha/runtime-strategy-config.json
```

The command prints the canonical config SHA-256 and the critical-source bundle SHA-256. The source bundle covers the quote engine, OMS, fills, accounting, reconciliation, risk, lifecycle, market/user streams and live safety gate. Any change to those files invalidates an older report.

## 2. Prepare `alpha-dataset-v1`

The dataset root has exactly these fields; unknown fields are rejected:

```json
{
  "schema_version": "alpha-dataset-v1",
  "strategy_id": "maker-alpha-v2",
  "training_end_at": "2026-01-01T00:00:00+00:00",
  "strategy_capital_usd": 1000.0,
  "fills": [],
  "terminal_marks": [],
  "equity_curve": []
}
```

Each `fills` item must contain:

- unique `event_id`, `market_id`, `token_id`;
- timezone-aware `executed_at` strictly after `training_end_at`;
- `side` (`BUY`/`SELL`), probability `price`, positive share `size`;
- explicit absolute `fee_amount` (zero is allowed only when it is an observed fact);
- `mark_30s_at` between 30 and 35 seconds after execution and `mark_30s_mid` in `[0,1]`.

The schema rejects reward fields, missing fees, non-finite values, duplicate events, lookahead timestamps and SELL quantities above evaluated inventory.

Each nonzero ending token position needs one `terminal_marks` row with `market_id`, `token_id`, `marked_at` and `mid`. The mark cannot predate the last fill for that token.

`equity_curve` must contain strictly increasing `{timestamp, strategy_equity_usd}` points covering the full fill interval. The first value must equal `strategy_capital_usd` within one cent, preventing unrelated account capital from mechanically shrinking the reported drawdown.

## 3. Generate evidence

Use a full 40–64 character build commit:

```bash
python -m app.core.alpha_evaluator \
  --dataset data/alpha/out-of-sample.json \
  --strategy-config data/alpha/runtime-strategy-config.json \
  --code-commit FULL_BUILD_COMMIT \
  --bootstrap-iterations 10000 \
  --bootstrap-seed 20260718 \
  --output data/alpha/alpha-evidence.json
```

The evaluator computes, rather than accepts as declared results:

- fee-aware trading PnL excluding rewards: signed fill cash plus ending inventory at terminal marks;
- 30-second signed markout net of explicit fees;
- deterministic 95% lower bounds using market-cluster bootstrap;
- maximum peak-to-trough strategy-equity drawdown divided by strategy capital.

It prints the exact evidence file SHA-256.

## 4. Admission thresholds

The live gate can only tighten these built-in floors:

- at least 1,000 fills, 20 markets and 30 days of out-of-sample fills;
- fee completeness exactly 1.0;
- positive reward-excluded PnL;
- positive 95% PnL lower bound;
- non-negative 30-second markout lower bound;
- maximum drawdown fraction no greater than 25%;
- report age no greater than 30 days;
- exact match for evidence file hash, strategy ID, build commit, canonical runtime config hash and current critical-source hash.

## 5. What this still does not prove

Even a passing report does not validate live exchange contracts. Before any future GO decision, the separate blockers in `LIVE_TRADING_REMEDIATION_PLAN.md` must be resolved: User WS authentication acknowledgement, fee field/unit/payer semantics, order status and cancel response contracts, authenticated missing-fill backfill, market sequence/timestamp/checksum behavior, historical ledger recovery and failure-injection replay.
