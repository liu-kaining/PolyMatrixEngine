# Funded Deployment Runbook

> This is an operator checklist, not a profitability claim. The engineering review and automated acceptance suite are strictly offline: they do not connect to Polymarket, derive credentials, submit/cancel orders, or validate a funded account. Keep `TRADING_MODE=disabled` until every gate below is satisfied.

## 1. Freeze and identify the release

Use an immutable 40-character Git commit and build only from the hash-locked dependency file:

```bash
git rev-parse HEAD
python -m pip install --require-hashes -r requirements.lock
```

Set `APP_CODE_COMMIT` to that exact commit. Any later change to critical trading code invalidates the Alpha evidence and requires a new report.

Back up the production database and verify that the backup can be restored before applying migrations. Render the migration SQL for review, then apply it once from a dedicated migration job while the API is stopped:

```bash
alembic upgrade base:head --sql
alembic upgrade head
```

Do not run multiple API replicas during migration. Live execution is intentionally single-writer and protected by the Postgres wallet lease.

## 2. Deploy locked

Copy `.env.example` to a secret-managed `.env`. At minimum:

- replace `POSTGRES_PASSWORD`, `PK` and `ADMIN_API_TOKEN`; use a URL-safe random database password and an admin token of at least 32 characters;
- keep `TRADING_MODE=disabled`, `LIVE_TRADING_ENABLED=False`, `OFFLINE_VALIDATED_ALPHA_ENABLED=False` and `ENABLE_ADMIN_WIPE=False`;
- keep `AUTO_ROUTER_ENABLED=False` for live—the reward-ranked router is paper-only;
- expose API/Dashboard only through an authenticated TLS reverse proxy. Compose binds them to loopback by default;
- set conservative per-market and global budgets that the operator can lose in full without operational harm.

Start the locked control plane and inspect `GET /health` and `GET /ready`. In disabled mode, no Polymarket market/user stream or order client is started.

Run the local accounting audit while disabled and with no engines active:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/admin/accounting/audits
```

Do not proceed if any inventory ledger is not verified `v2`, any fill/cash fact is missing, any order is unresolved, or a sticky halt is present. Legacy ledgers require complete historical fills and fees; there is no unsafe “assume flat” bypass.

## 3. Produce strategy evidence

Follow [`OFFLINE_ALPHA_EVALUATION.md`](./OFFLINE_ALPHA_EVALUATION.md) with a real out-of-sample dataset. The report must be generated against the exact release commit and exact runtime parameters, then configure:

```dotenv
OFFLINE_VALIDATED_ALPHA_ENABLED=True
ALPHA_VALIDATION_REPORT_PATH=/absolute/or/container/path/alpha-evidence.json
ALPHA_VALIDATION_REPORT_SHA256=<exact-file-sha256>
ALPHA_STRATEGY_ID=<report-strategy-id>
APP_CODE_COMMIT=<exact-release-commit>
```

The example evidence file is deliberately invalid. Do not lower thresholds merely to make a losing strategy pass. Rewards are excluded from admission PnL.

## 4. Operator-side account preflight

Using Polymarket's current official wallet/funding workflow, the operator must confirm in the intended deployment jurisdiction and network:

- `FUNDER_ADDRESS` is the wallet authenticated by `PK`;
- the wallet owns sufficient current V2 collateral (pUSD) for the configured budget plus operational margin;
- collateral and conditional-token allowances required by the venue are present;
- the deployment IP is geographically eligible;
- DNS, clock synchronization, TLS validation and outbound access to the pinned CLOB, stream and geoblock endpoints work;
- there are no unmanaged open orders or external positions in the same wallet.

These are external checks and were intentionally not performed by the offline review.

## 5. Create a short-lived arm

Set the intended wallet allow-list and loss ceiling. `GLOBAL_MAX_BUDGET` must not exceed `LIVE_BUDGET_CAP_USD`.

```dotenv
LIVE_ALLOWED_FUNDER_ADDRESSES=0x...
GLOBAL_MAX_BUDGET=...
LIVE_BUDGET_CAP_USD=...
```

Generate an arm expiring no more than 24 hours ahead:

```bash
python -m app.core.trading_safety \
  --funder 0xYOUR_FUNDER \
  --expires-at YYYY-MM-DDTHH:MM:SS+00:00 \
  --budget 20
```

Copy the exact expiry and generated digest to `LIVE_ARM_EXPIRES_AT` and `LIVE_ARM_TOKEN`, then set both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=True`. Restart one API instance. Merely selecting live does not submit an order.

Before starting any market, require `GET /ready` to return HTTP 200 with `live_order_ready=true`, every one of the 14 readiness components true, and no sticky halt. The startup sequence checks the wallet lease, geography, SDK credentials, open orders/trades, positions, reservations, accounting and Alpha evidence. A failed or uncertain check remains closed.

## 6. Controlled first market

Only after the previous section passes, choose one explicitly reviewed binary market with complete metadata, sufficient depth, a safe event horizon and no external wallet exposure. Keep the global/per-market budget and grid at their minimum operational values. Starting a market is the step that authorizes real order submission:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/markets/CONDITION_ID/start
```

Continuously monitor `/ready`, `/health`, `/orders/active`, the exchange UI, positions and logs. Stop immediately on unknown order state, queue drops, reconnect reconciliation, accounting degradation, unexpected taker fills, position mismatch, budget drift or negative markout beyond the evidence assumptions:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://127.0.0.1:8000/admin/halt
```

A halt blocks new risk and attempts cancellation, but it is not a guaranteed liquidation mechanism. Confirm outstanding orders and positions independently. The removed `/liquidate` endpoint must not be recreated as a market dump.

## 7. Incident recovery

- Preserve the database, logs, reconciliation audit and exchange facts. Never wipe evidence during an incident.
- An ambiguous submit without an exchange ID keeps its full reservation and requires manual comparison against the exchange account. Do not mark it canceled or release capital based only on absence from a local list.
- A provisional trade (`MATCHED`, `MINED` or retry state) freezes readiness until it becomes terminal; accounting changes only on `CONFIRMED`.
- To clear a sticky halt, first return to `TRADING_MODE=disabled`, stop every engine, confirm no unresolved orders/nonzero positions, run order/accounting reconciliation as applicable, then call the guarded halt-acknowledgement endpoint. Restart and repeat all preflights with a new short-lived arm.
- Roll back application code only with a database-compatible release. Never downgrade schema while retaining newer ledger facts.

## Non-negotiable release decision

Offline tests establish implementation invariants, not profitable or correct live behavior. If historical evidence, deployment checks, independent review or any runtime readiness item is missing, the release decision remains **locked**.
