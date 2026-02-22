# PolyMatrix Engine: 技术架构白皮书

### 1. 架构设计原则

将这些预测市场视同于高波动的金融衍生品交易，系统的底座必须坚若磐石。我们将采用纯异步的微服务设计，确保在高并发请求下系统不阻塞。数据层必须保证绝对的强一致性，任何一笔订单的状态流转（Pending -> Open -> Filled/Canceled）都不能在宕机重启后丢失。

### 2. 核心技术栈选型

* **核心语言与框架**：`Python 3.11+` 搭配 `FastAPI` 纯异步框架，兼顾复杂的数学模型计算与高并发 I/O。
* **官方 SDK 集成**：
* 订单交互与签名：`py-clob-client`
* 免 Gas 交易中继：`py-builder-relayer-client`


* **内存状态与高频缓存**：`Redis`。用于存储毫秒级刷新的 Orderbook 快照和高频 API 频率限制（Rate Limit）计数器。
* **持久化与状态机存储**：`PostgreSQL`。利用 JSONB 字段实现复杂订单 Payload 的高并发原子性写入，确保订单状态机的绝对安全。
* **区块链 RPC 节点**：`Alchemy` (Free Tier)。专供 Kill Switch 智能合约直连使用。

### 3. 系统模块架构详解

#### 3.1 接入层：Market Data Gateway

负责与 Polymarket 建立 WebSocket 长期连接。

* **HA 机制**：必须实现底层的心跳检测（Heartbeat）与断线重连逻辑。当检测到连接异常，使用指数退避算法（Exponential Backoff）进行重连，防止因瞬间高频请求被官方 IP 封禁。收到 Tick 数据后，通过 Pub/Sub 模式直接推入 Redis 供策略引擎消费。

#### 3.2 决策层：Alpha & Quoting Engine

此模块运行二元期权定价模型。

* **逻辑流**：从 Redis 读取最新 Orderbook -> 根据定价模型计算最优买卖价差（Spread） -> 生成网格挂单指令 -> 发送至 OMS。

#### 3.3 执行层：Order Management System (OMS)

整个系统的心脏，管理每一笔交易的生命周期。

* **状态机设计**：所有发往 `py-clob-client` 的请求，在发送前必须在 PostgreSQL 中创建一条 `Status=PENDING` 的记录。收到回执后原子级更新为 `OPEN` 或 `FAILED`。
* **全局断路器 (Circuit Breaker)**：在与 Polymarket API 的交互层植入断路器。当 API 返回连续的 `502` 或 `429` 错误达到阈值时，断路器熔断，阻止新订单发出，并发出高优告警，避免系统雪崩。

#### 3.4 风控层：Watchdog / Risk Monitor

独立于主业务流程之外的守护进程。

* 每秒扫描 PostgreSQL 中的实际持仓量与 Redis 中的活跃订单预期敞口。一旦识别到单边极值，直接向 OMS 发送最高优先级的 `CANCEL_ALL` 信号，并可选触发 Alchemy RPC 节点的链上智能合约熔断。

### 4. 数据表结构预研 (PostgreSQL 核心表)

| 表名 | 核心字段 | 说明 |
| --- | --- | --- |
| `markets_meta` | `condition_id`, `slug`, `end_date`, `status` | 追踪系统正在做市的目标市场元数据 |
| `orders_journal` | `order_id`, `market_id`, `side`, `price`, `size`, `status`, `payload (JSONB)` | 订单流水。`payload` 用于存储 SDK 返回的完整原始 JSON，方便后期对账与回溯。 |
| `inventory_ledger` | `market_id`, `yes_exposure`, `no_exposure`, `realized_pnl` | 实时计算的风险敞口账本，由 OMS 在订单成交（Filled）后通过事务更新。 |