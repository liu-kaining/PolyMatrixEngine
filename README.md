# PolyMatrix Engine

PolyMatrix Engine 是一个针对 Polymarket 的自动化高频做市与统计套利引擎，采用异步架构与严格的订单状态管理，目标是在零手续费环境下，通过提供网格化流动性与风险严格受控的方式，长期、稳定地赚取 Maker 收益与流动性补贴。

## 功能特性

- **Market Data Gateway（订单簿网关）**
  - 通过官方 Market WebSocket 订阅目标 `token_id` 的盘口增量更新。
  - 使用本地 `LocalOrderbook` 缓存，将增量数据合并成完整订单簿，再生成 **Top-5 BBO 快照**。
  - 对每个 token：
    - 持久化最新快照到 Redis：`ob:{token_id}`
    - 推送 Tick 到 Redis：`tick:{token_id}`，供 QuotingEngine 消费。
  - 在 `/markets/{condition_id}/start` 时，会先通过 CLOB REST API `GET /book?token_id=...` 触发一次**初始全量快照**，避免低波动市场长期“无 Tick”导致引擎不工作。

- **Quoting Engine（动态做市引擎）**
  - 通过 Redis PubSub 订阅：
    - `tick:{token_id}`：盘口快照
    - `control:{condition_id}`：控制信号（`suspend` / `resume`）
  - 使用 `AlphaModel` 基于：
    - BBO 中间价 `mid`
    - 盘口失衡度 OBI：\((bid\_size - ask\_size)/(bid\_size + ask\_size)\)
    - 当前 inventory（Yes/No 敞口）
    计算：
    - **动态 Fair Value（含盘口/库存 skew）**
    - **动态 Spread 宽度**
  - 在每个有效 tick 上：
    - 先从数据库读取 `InventoryLedger` 和 `MarketMeta`，计算当前 exposure（Yes 或 No）。
    - Debounce：若新 Fair Value 与上一次锚定价差小于阈值，则跳过重置，减少不必要改价。
    - 基于 Fair Value ± dynamic spread，按 `GRID_LEVELS` + `BASE_ORDER_SIZE` 生成新的买卖网格指令。
    - **严格顺序执行：先 cancel 当前全部活动订单，再按新网格下单**，保证不会产生长期幽灵挂单。
  - 支持控制信号：
    - `suspend`：设置内部 `suspended=True`，同步执行 `cancel_all_orders()`，形成真正的 Kill Switch。
    - `resume`：恢复正常响应 tick。

- **OMS（Order Management System，订单管理系统）**
  - 使用 `py-clob-client` 与 Polymarket CLOB 集成。
  - 订单生命周期：
    1. 在 DB 中插入 `OrderJournal`：`PENDING` 状态（本地临时 `local_...` ID）。
    2. 通过 `CircuitBreaker` 调用 `client.create_and_post_order()`（通过 `asyncio.to_thread` 下沉到线程池，不阻塞事件循环）。
    3. 成功则将 `order_id` 更新为真实链上/系统 ID，并标记为 `OPEN`；失败则标记为 `FAILED` 并写入错误 payload。
  - 撤单：
    - 正常模式：调用 `client.cancel(order_id)`，成功后标记 `CANCELED`。
    - 附加：撤单后会同步调用 `client.get_order(order_id)` 检查 `size_matched`，用于发现“先部分成交再撤单”等粉尘情况，并记录到 payload 中，方便对账。
  - **CircuitBreaker 熔断器：**
    - 连续多次 API 级错误（如 400 余额不足 / 最小订单尺寸不满足等）会累积失败计数。
    - 达到阈值后进入 `OPEN` 状态，暂时阻止新的下单/撤单请求，防止在资金不足 / 配置错误时疯狂轰炸 API。
    - 隔一段时间会自动尝试从 `OPEN` → `HALF_OPEN` → `CLOSED`。
  - **Dry-Run / 实盘切换：**
    - 当 `.env` 中 `LIVE_TRADING_ENABLED=False` 或 `ClobClient` 未成功初始化时：
      - 所有下单/撤单都会以 `[DRY-RUN]` 方式在本地 DB 内模拟 `OPEN/CANCELED` 状态，不会真正访问 CLOB。

- **Risk Watchdog（风控守护进程）**
  - 独立协程常驻后台，周期性读取 `InventoryLedger` 等数据：
    - 监控每个 `market_id` 的 Yes / No 敞口。
    - 若敞口绝对值超过 `MAX_EXPOSURE_PER_MARKET`：
      - 通过 Redis 发布 `control:{condition_id}` → `{"action": "suspend"}`。
      - 调用 `oms.cancel_market_orders(condition_id)` 撤光该市场全部 `OPEN/PENDING` 挂单。

- **Dashboard（Streamlit 驾驶舱）**
  - **Control Panel：**
    - 输入 `condition_id`，一键调用 `/markets/{condition_id}/start`，自动：
      - 从 Gamma 获取 CLOB tokenIds；
      - 订阅 Market WS + User WS；
      - 启动双 token 的 QuotingEngine；
      - 触发 REST 初始快照 + 第一轮网格挂单。
  - **Emergency Controls：**
    - 输入 `condition_id` 后：
      - `🛑 Stop`：调用 `/markets/{id}/stop` → 软撤单 + 挂牌暂停。
      - `☢️ Liquidate All`：调用 `/markets/{id}/liquidate` → 先撤单，再按 0.01 价位砸盘清仓 Yes/No 敞口。
  - **Inventory & Risk：**
    - 实时展示 `inventory_ledger`：
      - Yes/No 各市场敞口条形图。
      - `gamma_link`: 跳转到 `https://gamma-api.polymarket.com/markets?condition_ids=...`。
      - `polymarket_link`: 跳转到 `https://polymarket.com/event/{slug}`。
  - **Active Orders：**
    - 来自 `orders_journal` 的 `OPEN/PENDING` 订单列表，含价格、方向、created_at 等。

- **日志与时区**
  - FastAPI 进程启动时强制设置 `TZ=Asia/Shanghai` 并 `time.tzset()`，所有 `%(asctime)s` 日志使用北京时间（UTC+8）。

## 环境依赖

- Docker 和 Docker Compose
- PostgreSQL、Redis（由 docker-compose 提供）
- Polymarket 账户与资金（USDC.e）
- Polymarket CLOB API 的私钥 (`PK`) 与 `FUNDER_ADDRESS`

## 安装与运行

整个应用通过 Docker 一键部署。

1. **克隆仓库**

```bash
git clone https://github.com/liukaining/PolyMatrixEngine.git
cd PolyMatrixEngine
```

2. **配置环境变量**

```bash
cp .env.example .env
# 编辑 .env，至少配置：
# PK, FUNDER_ADDRESS, LIVE_TRADING_ENABLED, BASE_ORDER_SIZE, GRID_LEVELS, MAX_EXPOSURE_PER_MARKET
```

3. **启动服务**

```bash
docker compose up --build -d
```

- API: `http://localhost:8000`
- Dashboard: `http://localhost:8501`

4. **查看日志**

```bash
docker compose logs -f api
```

## Dashboard & Monitoring

PolyMatrix Engine 自带一个基于 Streamlit 的监控面板。

- 打开浏览器访问：`http://localhost:8501`
- 面板功能：
  - **Control Panel**：输入 `condition_id`，一键启动 Quoting Engine。
  - **Inventory & Risk**：条形图展示每个市场 Yes/No 敞口与已实现 PnL。
  - **Active Orders**：展示当前所有 `OPEN` / `PENDING` 订单。
  - **Emergency Controls**：对指定 `condition_id` 执行 Stop / Liquidate。

## 主要 API

- 健康检查

```bash
curl http://localhost:8000/health
```

- 启动某个市场的做市

```bash
curl -X POST http://localhost:8000/markets/{condition_id}/start
```

- 停止 / 强平

```bash
curl -X POST http://localhost:8000/markets/{condition_id}/stop
curl -X POST http://localhost:8000/markets/{condition_id}/liquidate
```

- 查看市场风险

```bash
curl http://localhost:8000/markets/{condition_id}/risk
```

- 查看当前活动订单

```bash
curl http://localhost:8000/orders/active
```

## 关键配置项（.env）

- `LIVE_TRADING_ENABLED`：`True` 实盘 / `False` Dry-Run。
- `MAX_EXPOSURE_PER_MARKET`：单市场最大敞口上限（USDC 名义）。
- `BASE_ORDER_SIZE`：每笔限价单的下单 size（需 ≥ Polymarket `orderMinSize`）。
- `GRID_LEVELS`：每一侧（买/卖）的网格层数。
- 其他基础设施和网络配置：
  - `PM_WS_URL` / `PM_API_URL` / `PM_CHAIN_ID`
  - `DATABASE_URL` / `REDIS_URL`
  - `ALCHEMY_RPC_URL`（当前版本仅预留）

## Disclaimer

This software is provided for educational and experimental purposes. Using it to trade on Polymarket carries significant financial risk. The developers assume no responsibility for any trading losses.
