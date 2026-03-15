# Shares vs Dollars 重构 — 部署与迁移指南

## 变更摘要

- **DB**：`inventory_ledger` 新增 `yes_capital_used`、`no_capital_used`（Numeric，默认 0）
- **inventory_state**：维护 capital_used，`get_global_exposure` → `get_global_used_dollars`（纯美金）
- **engine**：`_apply_balance_precheck` 全美金；`extreme_threshold` 与 `local_used_dollars` 比较
- **watchdog**：用 `local_used_dollars` 与 `MAX_EXPOSURE_PER_MARKET`（美金）比较

## 实盘停机、迁移、重启流程

```bash
# 1. 停机（停止 API 和 Dashboard）
docker compose stop api dashboard

# 2. 执行数据库迁移（在宿主机或任意能连 DB 的容器内）
# 方式 A：宿主机执行（需安装 alembic、psycopg2、python-dotenv）
cd /path/to/PolyMatrixEngine
alembic upgrade head

# 方式 B：使用临时容器执行迁移
docker compose run --rm api alembic upgrade head

# 3. 确认迁移成功
# 检查 inventory_ledger 表是否有 yes_capital_used、no_capital_used 列
docker compose exec postgres psql -U postgres -d polymatrix -c "\d inventory_ledger"

# 4. 重启服务
docker compose up -d api dashboard
```

## 迁移脚本生成（如需重新生成）

```bash
# 生成迁移（已提供 005_add_capital_used.py，通常无需重新生成）
alembic revision --autogenerate -m "add capital used"

# 执行迁移
alembic upgrade head

# 回滚（仅用于测试）
alembic downgrade -1
```

## 配置说明

- **MAX_EXPOSURE_PER_MARKET**：单市场最大占用 **美金**（如 200 = 200 USDC）
- **GLOBAL_MAX_BUDGET**：全局最大占用 **美金**（如 1000 = 1000 USDC）

## 历史数据

迁移会为 `inventory_ledger` 中已有行添加 `yes_capital_used`、`no_capital_used`，默认 0。  
首次成交后，`apply_fill` 会按 fill_price × filled_size 正确累加 capital_used。
