# PolyMatrix Engine 技术分析报告

> 生成时间：2026-03-23（按最新代码复核）
> 分析版本：v1.1
> 报告用途：架构升级 | 技术尽调 | 重构规划 | 风险评估

## 1. 执行摘要

### 1.1 项目概览

| 属性 | 说明 |
|------|------|
| **项目名称** | PolyMatrix Engine |
| **技术栈** | Python 3.11, FastAPI, PostgreSQL 15, Redis 7, SQLAlchemy (async), WebSockets |
| **代码规模** | 约7,000+行Python代码（按当前仓库文件估算） |
| **架构模式** | 内存优先 + 异步事件驱动 + 状态机 |
| **项目类型** | Web3 量化交易引擎（做市商） |
| **复杂度评级** | 🟡中 |

### 1.2 核心发现

1. **架构亮点**: 采用内存优先架构，tick循环仅读写内存，无数据库访问，确保低延迟
2. **设计亮点**: 完善的差分报价(Diff Quoting)机制，保留时间优先级并减少API调用
3. **风险问题**: API端点缺少认证授权，`/admin/wipe` 可清空全量数据
4. **性能问题**: WebSocket网关的orderbook缓存无限增长，存在内存泄漏风险
5. **代码质量**: 存在大量魔法数字、深嵌套、大函数等问题

### 1.3 风险总览

| 风险等级 | 数量 | 代表性问题 |
|----------|------|------------|
| 🔴P0-P1 | 8 | 密钥硬编码、API无认证、内存泄漏 |
| 🟡P2 | 15+ | N+1查询、缺失索引、代码重复 |
| 🟢P3 | 20+ | 魔法数字、命名不一致 |

### 1.4 建议优先级总表

| 优先级 | 建议项 | 预期收益 | 修复成本 |
|--------|--------|----------|----------|
| P0 | API认证与授权 | 安全合规 | 2人天 |
| P0 | 移除硬编码密码 | 安全合规 | 1人天 |
| P1 | 修复内存泄漏 | 长期稳定性 | 2人天 |
| P1 | 修复N+1查询 | 性能提升 | 1人天 |
| P2 | 代码重构 | 可维护性 | 5人天 |

## 2. 整体架构

### 2.1 架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                         PolyMatrix Engine                              │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────┐     ┌─────────────┐     ┌─────────────────────────┐ │
│  │   FastAPI   │     │  Dashboard  │     │      Auto-Router        │ │
│  │  (Port 8000)│     │ (Streamlit)  │     │   (Background Task)      │ │
│  │  控制平面    │     │   Port 8501  │     │                         │ │
│  └──────┬──────┘     └──────┬──────┘     └───────────┬─────────────┘ │
│         │                   │                        │               │
│  ┌──────┴───────────────────┴────────────────────────┴─────────────┐ │
│  │                      Market Lifecycle Manager                      │ │
│  │                   (任务生命周期管理与协调)                           │ │
│  └──────┬───────────────────┬────────────────────────┬─────────────┘ │
│         │                   │                        │               │
│  ┌──────┴──────┐     ┌──────┴──────┐          ┌──────┴──────┐        │
│  │ Quoting     │     │ Risk        │          │ Inventory   │        │
│  │ Engine      │     │ Watchdog    │          │ State       │        │
│  │ (per-market)│     │             │          │ (Singleton) │        │
│  └──────┬──────┘     └──────┬──────┘          └──────┬──────┘        │
│         │                   │                        │               │
│  ┌──────┴──────┐     ┌──────┴──────┐          ┌──────┴──────┐        │
│  │ OMS         │     │ Data API   │          │ Redis      │        │
│  │ (Order API) │     │ Reconciliation│        │ (Pub/Sub)  │        │
│  └──────┬──────┘     └─────────────┘          └──────┬──────┘        │
│         │                                             │               │
│  ┌──────┴──────────────────────────────────────────────┴─────────────┐│
│  │                    External Services                                ││
│  │  Polymarket CLOB WS │ Polymarket Gamma API │ Polymarket Data API  ││
│  └────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                         Docker Compose Stack                          │
├──────────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐ │
│  │  API Service │  │  Dashboard  │  │  PostgreSQL │  │    Redis   │ │
│  │   (FastAPI)  │  │  (Streamlit) │  │    (15)     │  │    (7)     │ │
│  │   Port 8000  │  │   Port 8501  │  │   Port 5432 │  │  Port 6379 │ │
│  └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 核心数据流

```
1. 市场数据流:
   Polymarket WS → MarketDataGateway → Redis(tick:{token}) → QuotingEngine.on_tick()

2. 报价决策流:
   on_tick() → AlphaModel.calculate_yes_anchor() → 差分报价计算 → OMS.create_order()

3. 订单成交流:
   UserStream WS → handle_fill() → inventory_state.apply_fill() → 异步DB持久化

4. 风险监控流:
   Watchdog (每秒) → check_exposure()；并行后台每60秒（默认）→ reconcile_positions() → Data API 对账
```

### 2.3 模块依赖关系

```
app/main.py (FastAPI入口)
├── app/core/config.py (配置)
├── app/db/session.py (数据库会话)
├── app/core/redis.py (Redis客户端)
├── app/core/inventory_state.py (库存状态)
├── app/core/market_lifecycle.py (市场生命周期)
├── app/market_data/gateway.py (市场数据网关)
├── app/market_data/user_stream.py (用户流)
├── app/market_data/gamma_client.py (Gamma API客户端)
├── app/quoting/engine.py (报价引擎)
├── app/oms/core.py (订单管理系统)
└── app/risk/watchdog.py (风险监控)
```

## 3. 核心领域模型

### 3.1 核心实体关系图

```mermaid
classDiagram
    class MarketMeta {
        +String condition_id (PK)
        +String slug
        +DateTime end_date
        +Enum status
        +String yes_token_id
        +String no_token_id
        +Float rewards_min_size
        +Float rewards_max_spread
        +Float reward_rate_per_day
    }
    class OrderJournal {
        +String order_id (PK)
        +String market_id (FK)
        +Enum side (BUY/SELL)
        +Float price
        +Float size
        +Enum status
        +JSON payload
        +DateTime created_at
        +DateTime updated_at
    }
    class InventoryLedger {
        +String market_id (PK, FK)
        +Float yes_exposure
        +Float no_exposure
        +Float yes_capital_used
        +Float no_capital_used
        +Float realized_pnl
    }
    
    MarketMeta "1" --> "N" OrderJournal : has orders
    MarketMeta "1" --> "1" InventoryLedger : tracks position
```

### 3.2 状态枚举

```python
class OrderStatus(Enum):
    PENDING = "PENDING"    # 已提交待确认
    OPEN = "OPEN"          # 订单在场内
    FILLED = "FILLED"      # 已成交
    CANCELED = "CANCELED"  # 已取消
    FAILED = "FAILED"      # 失败

class OrderSide(Enum):
    BUY = "BUY"            # 买
    SELL = "SELL"          # 卖

class MarketStatus(Enum):
    ACTIVE = "active"       # 活跃
    CLOSED = "closed"       # 已关闭
    RESOLVED = "resolved"   # 已结算
    SUSPENDED = "suspended" # 已暂停

class QuotingMode(Enum):
    QUOTING = "QUOTING"                    # 正常报价
    QUOTING_BIDS_ONLY = "QUOTING_BIDS_ONLY" # 仅买报价
    LOCKED_BY_OPPOSITE = "LOCKED"          # 被对手锁定
    GRACEFUL_EXIT = "GRACEFUL_EXIT"        # 优雅退出
    LIQUIDATING = "LIQUIDATING"            # 平仓中
    SUSPENDED = "SUSPENDED"                 # 暂停
```

## 4. 核心功能模块

### 4.1 报价引擎 (QuotingEngine)

#### 4.1.1 功能流程图

```mermaid
flowchart TD
    A[收到Tick] --> B{检查引擎状态}
    B -->|SUSPENDED| Z[跳过报价]
    B -->|正常运行| C[获取统一公允价值]
    C --> D[获取库存快照]
    D --> E{预算检查}
    E -->|预算超限| Z
    E -->|预算正常| F[生成目标报价网格]
    F --> G[同步订单差异]
    G --> H[下单/撤单]
    H --> I[记录日志]
```

#### 4.1.2 核心定价公式

```python
# YES锚定价格计算
FV_yes = clamp(mid_yes + OBI_yes * 0.015, 0.01, 0.99)

# 动态点差
spread = base_spread * (1.0 + |OBI|)

# 有效订单尺寸
effective_size = BASE_ORDER_SIZE * size_multiplier * inventory_adjustment
```

#### 4.1.3 关键设计点

1. **差分报价(Diff Quoting)**: 仅创建缺失订单、取消过时订单，保持时间优先级
2. **严格MTM预算**: 实时计算浮动盈亏，确保风险可控
3. **状态机模式**: 通过QuotingMode枚举管理报价状态转换

### 4.2 风险监控 (RiskWatchdog)

#### 4.2.1 对账流程

```mermaid
sequenceDiagram
    participant Watchdog
    participant InventoryState
    participant DataAPI
    participant OMS
    
    Watchdog->>InventoryState: get_snapshot()
    Watchdog->>DataAPI: reconcile_single_market()
    DataAPI-->>Watchdog: actual_positions
    Watchdog->>Watchdog: 对比差异
    alt 差异超限
        Watchdog->>InventoryState: apply_reconciliation_snapshot()
        Watchdog->>OMS: trigger_kill_switch()
    end
```

### 4.3 自动路由 (AutoRouter)

#### 4.3.1 市场评分公式

```python
score = daily_roi * log10(liquidity) * time_decay

# 时间衰减因子
time_decay = 1.0  (days_left <= 90)
time_decay = 0.5  (90 < days_left <= 180)
time_decay = 0.01 (days_left > 180)
```

## 5. 设计模式分析

| 模式类型 | 应用位置 | 使用场景 | 评估 |
|----------|----------|----------|------|
| 单例模式 | `InventoryStateManager` | 全局唯一库存状态 | ✅ 合理 |
| 状态机模式 | `QuotingEngine` | 报价状态转换 | ✅ 合理 |
| 断路器模式 | `CircuitBreaker` | OMS熔断保护 | ✅ 合理 |
| 观察者/发布订阅 | Redis Pub/Sub | Tick数据分发 | ✅ 合理 |
| 异步队列 | `inventory_state.py` | DB持久化缓冲 | ✅ 合理 |
| 策略模式 | `AlphaModel` | 定价算法 | ⚠️ 未来可扩展 |

## 6. API与接口分析

### 6.1 REST API端点

| 接口路径 | 方法 | 职责 | 认证要求 | 风险等级 |
|----------|------|------|----------|----------|
| `/health` | GET | 健康检查 | 无 | 🟢低 |
| `/markets/{condition_id}/start` | POST | 启动做市 | 无 | 🔴高 |
| `/markets/{condition_id}/stop` | POST | 停止做市 | 无 | 🔴高 |
| `/markets/{condition_id}/liquidate` | POST | 紧急平仓 | 无 | 🔴高 |
| `/markets/{condition_id}/risk` | GET | 风险数据 | 无 | 🟡中 |
| `/markets/status` | GET | 市场状态 | 无 | 🟡中 |
| `/orders/active` | GET | 活跃订单 | 无 | 🟡中 |
| `/admin/wipe` | POST | 清空数据 | 无 | 🔴极高 |

### 6.2 WebSocket连接

| 连接类型 | URL | 用途 |
|----------|------|------|
| Market Data WS | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | 订单簿数据 |
| User Stream WS | `wss://ws-subscriptions-clob.polymarket.com/ws/user` | 用户订单/成交 |

### 6.3 外部API

| 服务 | URL | 用途 |
|------|------|------|
| Polymarket CLOB REST | `https://clob.polymarket.com` | 订单操作 |
| Gamma API | `https://gamma-api.polymarket.com` | 市场元数据 |
| Polymarket Data API | `https://data-api.polymarket.com` | 持仓对账 |

## 7. 数据模型分析

### 7.1 数据库表结构

#### markets_meta (市场元数据)

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| condition_id | VARCHAR | PK | 市场唯一标识 |
| slug | VARCHAR | | 市场名称 |
| end_date | TIMESTAMP | | 结束时间 |
| status | ENUM | | active/closed/resolved/suspended |
| yes_token_id | VARCHAR | | YES代币ID |
| no_token_id | VARCHAR | | NO代币ID |
| rewards_min_size | FLOAT | | 奖励最小尺寸 |
| rewards_max_spread | FLOAT | | 奖励最大价差 |
| reward_rate_per_day | FLOAT | | 每日奖励率 |

#### orders_journal (订单日志)

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| order_id | VARCHAR | PK | 订单ID |
| market_id | VARCHAR | FK, INDEX | 市场ID |
| side | ENUM | | BUY/SELL |
| price | FLOAT | | 价格 |
| size | FLOAT | | 数量 |
| status | ENUM | INDEX | PENDING/OPEN/FILLED/CANCELED/FAILED |
| payload | JSON | | 原始API响应 |
| created_at | TIMESTAMP | | 创建时间 |
| updated_at | TIMESTAMP | | 更新时间 |

#### inventory_ledger (库存台账)

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| market_id | VARCHAR | PK, FK | 市场ID |
| yes_exposure | FLOAT | | YES持仓暴露 |
| no_exposure | FLOAT | | NO持仓暴露 |
| yes_capital_used | FLOAT | | YES占用资金 |
| no_capital_used | FLOAT | | NO占用资金 |
| realized_pnl | FLOAT | | 已实现盈亏 |

## 8. 深度风险分析

### 8.1 风险总览

| 风险维度 | P0-P1数量 | P2数量 | P3数量 | 风险评级 |
|----------|-----------|--------|--------|----------|
| 🔴架构风险 | 2 | 3 | 2 | 🟡中 |
| 🔴安全风险 | 4 | 5 | 2 | 🔴高 |
| 🔴性能风险 | 3 | 4 | 3 | 🟡中 |
| 🔴数据风险 | 1 | 2 | 1 | 🟢低 |
| 🔴业务风险 | 2 | 3 | 4 | 🟡中 |

### 8.2 安全风险

| 风险点 | 描述 | 严重度 | 漏洞类型 | 建议修复 |
|--------|------|--------|----------|----------|
| **硬编码密码** | `config.py:19` 包含明文密码 `postgres_password` | 🔴P0 | CWE-798 | 迁移至环境变量或密钥管理服务 |
| **API无认证** | 所有FastAPI端点缺少认证授权 | 🔴P0 | CWE-306 | 添加JWT/API Key认证 |
| **危险管理操作** | `/admin/wipe` 无任何保护即可清空全量数据 | 🔴P0 | CWE-269 | 添加多因素确认+操作审计 |
| **异常吞没** | 多处 `except Exception: pass` 隐藏错误 | 🟡P1 | CWE-396 | 细化异常类型，添加日志 |
| **敏感日志** | `user_stream.py:121` 可能记录认证失败详情 | 🟡P1 | CWE-532 | 脱敏处理异常消息 |

### 8.3 架构风险

| 风险点 | 描述 | 影响 | 概率 | 风险值 |
|--------|------|------|------|--------|
| **orderbook内存泄漏** | `gateway.py` 的 `self.books` 字典无限增长 | 高 | 中 | 45 |
| **订阅集合无清理** | `user_stream.py` 的 `subscribed_markets` 只增不减 | 中 | 中 | 30 |
| **engine_tasks泄漏** | `market_lifecycle.py` 任务异常可能未清理 | 中 | 低 | 20 |

### 8.4 性能风险

| 风险点 | 描述 | 影响 | 优化建议 |
|--------|------|------|----------|
| **N+1 Redis查询** | `get_markets_status()` 每市场4次串行Redis调用 | 高延迟 | 使用 `MGET` 或 Pipeline |
| **缺失复合索引** | `orders_journal` 缺少 `(market_id, status)` 索引 | 查询慢 | 添加复合索引 |
| **连接池未配置** | SQLAlchemy使用默认池大小5 | 突发瓶颈 | 根据并发需求调整 |
| **无API限流** | 所有端点无Rate Limiting | DoS风险 | 引入限流中间件 |

### 8.5 数据风险

| 风险点 | 场景描述 | 影响 | 缓解措施 |
|--------|----------|------|----------|
| **持久化队列满** | `inventory_state.py` 队列满时丢弃写入 | DB数据漂移 | 增加重试机制 |
| **时间戳冲突** | 300秒重置窗口内WS可能丢失 | 幽灵订单 | 8秒Buffer对账 |

### 8.6 业务风险

| 风险点 | 业务影响 | 当前措施 | 改进建议 |
|--------|----------|----------|----------|
| **幂等性依赖WS** | 依赖WebSocket推送更新库存 | 300秒强制对账 | 考虑补充轮询 |
| **错误码不统一** | 各模块错误处理各异 | 无 | 建立错误码规范 |

### 8.7 运维风险

| 风险点 | 现状评估 | 建议 |
|--------|----------|------|
| **可观测性** | 仅日志，无指标采集 | 引入Prometheus metrics |
| **告警机制** | 无自动化告警 | 接入监控系统 |
| **容器健康检查** | 无Liveness/Readiness探针 | 添加健康检查端点 |

## 9. 技术债务分析

### 9.1 债务清单

| 债务类型 | 债务项 | 修复成本(人天) | 优先级 |
|----------|--------|---------------|--------|
| 🔴安全 | 移除硬编码密码 | 1 | P0 |
| 🔴安全 | API认证授权 | 2 | P0 |
| 🔴安全 | 管理端点保护 | 1 | P0 |
| 🟡性能 | 修复orderbook内存泄漏 | 2 | P1 |
| 🟡性能 | 修复N+1 Redis查询 | 1 | P1 |
| 🟡性能 | 添加复合索引 | 0.5 | P1 |
| 🟡性能 | 配置连接池 | 0.5 | P1 |
| 🟡性能 | 添加API限流 | 1 | P1 |
| 🟡代码 | 重构大函数 (>100行) | 5 | P2 |
| 🟡代码 | 消除魔法数字 | 2 | P2 |
| 🟡代码 | 消除重复代码 | 2 | P2 |
| 🟢代码 | 统一命名规范 | 1 | P3 |

### 9.2 债务可视化

```mermaid
pie title 技术债务分布
    "安全债务" : 30
    "性能债务" : 35
    "代码规范" : 25
    "可观测性" : 10
```

## 10. 代码质量评估

### 10.1 量化指标

| 指标 | 数值 | 评级 | 参考标准 |
|------|------|------|----------|
| 总代码行数 | ~6,000（Python） | - | - |
| Python文件数 | 22 | - | - |
| 核心类圈复杂度 | 8-15 | 🟡中 | >10 高风险 |
| 大函数 (>100行) | 8 | 🟡中 | 越少越好 |
| 深嵌套 (>4层) | 4 | 🟡中 | >3 高风险 |
| 魔法数字 | 50+ | 🔴高 | 越少越好 |
| 重复代码块 | 3处 | 🟡中 | <3处 达标 |

### 10.2 问题清单

#### 🔴P0 - 严重问题

| 问题 | 位置 | 严重度 | 建议 |
|------|------|--------|------|
| 硬编码数据库密码 | `config.py:19` | P0 | 使用环境变量 |
| API端点无认证 | `main.py` | P0 | 添加认证中间件 |
| 管理端点无保护 | `main.py:373` | P0 | 添加操作保护 |

#### 🟡P1 - 重要问题

| 问题 | 位置 | 严重度 | 建议 |
|------|------|--------|------|
| orderbook内存泄漏 | `gateway.py:21` | P1 | 添加unsubscribe清理 |
| N+1 Redis查询 | `main.py:300-347` | P1 | 使用MGET/Pipeline |
| 队列满丢弃数据 | `inventory_state.py:222` | P1 | 添加重试机制 |
| 异常吞没 | 多处 `except Exception` | P1 | 细化异常类型 |

#### 🟢P2 - 优化建议

| 问题 | 位置 | 严重度 | 建议 |
|------|------|--------|------|
| 魔法数字 | 50+处 | P2 | 提取为常量 |
| 大函数 | `on_tick` 390行 | P2 | 拆分为子方法 |
| 深嵌套 | 4+层 | P2 | 使用早返回模式 |
| 代码重复 | 3处 | P2 | 提取公共函数 |

## 11. 优化建议

### 11.1 短期优化（1周内）

#### P0 - 紧急

**1. 移除硬编码密码**
```python
# Before
DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres_password@localhost:5432/polymatrix"

# After
DATABASE_URL: str = Field(default="", description="postgresql+asyncpg://user:pass@host:5432/db")
```

**2. 添加API认证**
```python
# 引入认证依赖
from fastapi import Security, HTTPBearer
from typing import Optional

auth_scheme = HTTPBearer(auto_error=False)

@app.post("/markets/{condition_id}/start")
async def start_market(
    condition_id: str,
    authorization: Optional[str] = Security(auth_scheme)
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # ... 业务逻辑
```

**3. 添加管理端点保护**
```python
@app.post("/admin/wipe")
async def wipe_all_data(
    confirmation: str = Body(..., embed=True),
    admin_key: str = Header(...)
):
    if admin_key != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if confirmation != "CONFIRM_WIPE":
        raise HTTPException(status_code=400, detail="Confirmation required")
    # ... 业务逻辑
```

### 11.2 中期改进（1-2周）

| 优化项 | 当前问题 | 目标状态 | 修复成本 |
|--------|----------|----------|----------|
| 修复内存泄漏 | `self.books` 无限增长 | 添加清理机制 | 2人天 |
| 优化Redis查询 | 4N次串行调用 | 使用MGET批量获取 | 1人天 |
| 添加数据库索引 | 缺失 `(market_id, status)` | 创建复合索引 | 0.5人天 |
| 添加限流 | 无Rate Limiting | 引入 slowapi | 1人天 |
| 异常处理重构 | 吞没所有异常 | 细化异常类型 | 2人天 |

### 11.3 长期规划（1-2月）

| 方向 | 描述 | 预期收益 | 前置条件 |
|------|------|----------|----------|
| 代码重构 | 拆分大函数，消除重复代码 | 可维护性提升 | P2完成 |
| 可观测性 | 引入Prometheus + Grafana | 运维效率提升 | 指标定义 |
| 灰度发布 | 支持多版本引擎 | 降低升级风险 | 配置中心 |

## 12. 扩展点设计

### 12.1 如何新增报价策略

```python
# 1. 实现AlphaPricingModel接口
class NewPricingModel(AlphaPricingModel):
    def calculate_yes_anchor(self, book: LocalOrderbook) -> float:
        # 自定义定价算法
        pass

# 2. 在配置中添加策略选择
class Settings(BaseSettings):
    pricing_model: str = "default"  # "default" | "new_model"

# 3. 工厂创建
def create_pricing_model(name: str) -> AlphaPricingModel:
    models = {
        "default": AlphaPricingModel(),
        "new_model": NewPricingModel(),
    }
    return models.get(name, models["default"])
```

### 12.2 如何添加风控规则

```python
# 在RiskMonitor中添加新规则
class RiskMonitor:
    async def check_custom_rule(self, condition_id: str) -> bool:
        # 自定义风控规则
        # 返回 True 表示通过，False 表示触发风控
        pass
    
    async def check_exposure(self, condition_id: str):
        # ... 现有检查 ...
        
        # 添加自定义规则检查
        if not await self.check_custom_rule(condition_id):
            logger.warning(f"Custom rule triggered for {condition_id}")
            await self.trigger_kill_switch(condition_id)
```

## 13. 状态机-日志对照表（运行期判读手册）

本节用于将运行日志直接映射到引擎状态机，快速判断是“系统自我保护”还是“真正故障”。
结论优先级：先看 `Mode` 与 `Order Instructions Payload`，再看 OMS HTTP 状态码，最后看是否出现持续异常堆栈。

### 13.0 10秒值班卡片（极简版）

- 看到 `Mode: LOCKED_BY_OPPOSITE` + `Payload: []`：先判定为**风控主动保护**，不是下单链路故障。  
- 看到 `POST/DELETE /order` 大量 `200`：OMS/CLOB 通道**正常**。  
- 看到 `HARD RESET -> Resuming quoting -> create missing -> POST 200`：系统**自愈闭环成功**。  
- 看到 `POST_RESET_RECONCILE_FREEZE`：属于**保守冻结模式**，优先查对账，不要误报交易故障。  
- 只有当 `Circuit breaker OPEN` 持续跨恢复窗口且无成功单，才升级为**通道异常**。  

### 13.1 核心判读原则

| 判读信号 | 代表含义 | 优先级 |
|----------|----------|--------|
| `Mode: LOCKED_BY_OPPOSITE` 且 `Payload: []` | 风控主动暂停一侧 BUY，属于保护性停单 | P0 |
| `POST/DELETE /order -> 200 OK` | 交易链路健康，OMS/CLOB 通道可用 | P0 |
| `[HARD RESET] ... Resuming quoting` | 幽灵单清理 + 对账 + 恢复流程正常完成 | P1 |
| `Order failed: Circuit breaker is OPEN` | OMS 熔断器在保护期内，短时拒绝请求 | P1 |
| 重复 `GRID EXEC` 高频打印 | 高频行情驱动或双引擎并发，不等同故障 | P2 |

### 13.2 关键状态与日志映射

| 状态/机制 | 典型日志特征 | 触发条件（代码语义） | 系统意图 | 是否需要人工干预 |
|-----------|--------------|----------------------|----------|------------------|
| `LOCKED_BY_OPPOSITE` | `CROSS-TOKEN LOCK... Suspend BUY on YES/NO` + `Order Instructions Payload: []` | 对侧 `capital_used` 超过阈值窗口（与 `liquidate_threshold`/极值阈值联动） | 停止继续加仓，保留现金给风险侧去库存 | 否（默认）；仅当长期不解锁时排查敞口回落链路 |
| `QUOTING_BIDS_ONLY` | `Mode: QUOTING_BIDS_ONLY`，通常仅 BUY 或空载荷 | 当前侧库存低、策略只做被动建仓或受奖励/预算约束 | 控制库存方向，避免双边无差别做市 | 否 |
| `TWO_WAY_QUOTING` | `Mode: TWO_WAY_QUOTING` + BUY/SELL 双指令 | 库存/预算处于安全区 | 常规双边做市 | 否 |
| `MAKER UNWINDING` | `INVENTORY HIGH ... MAKER UNWINDING` | 当前持仓高于库存阈值 | 优先挂卖单减仓，赚价差出清库存 | 否 |
| `EXTREME TAKER`/`EXTREME_LIQUIDATING` | `EXTREME INVENTORY ...` 或 `GRACEFUL EXIT: forcing EXTREME TAKER` | 库存或退出态达到极值 | 牺牲价格优先回收现金与风险 | 视情况（若频繁触发，需调策略参数） |
| `POST_RESET_RECONCILE_FREEZE` | `FREEZE NEW BUYS ... reconcile did not succeed` | Hard reset 后 REST 对账失败 | 短时冻结新 BUY，防止不确定库存下继续加仓 | 是（需尽快查对账失败原因） |
| 奖励保护（Rewards Guard） | `[REWARDS] Shrunk size ... Dropping BUY order` | 可下单规模低于奖励最低门槛 | 避免“慈善报价”（不达奖励却暴露风险） | 否 |
| 严格预算上限（Strict Budget） | `STRICT BUDGET CAP ... no new BUY orders` | `MTM + pending` 达到市场预算上限 | 禁止新增 BUY，防资金超配 | 否（策略预期） |
| OMS 熔断（Circuit Breaker OPEN） | `Circuit breaker is OPEN. Blocking request.` | 短时连续瞬态错误达到阈值 | 防止错误风暴放大，等待恢复窗口 | 视持续时长；短时无需干预 |

### 13.3 `HARD RESET` 标准成功链路

出现以下日志序列可判定为“自愈成功”：

1. `[HARD RESET] Initiating physical CLOB Cancel-All ...`
2. `DELETE https://clob.polymarket.com/order "HTTP/2 200 OK"`（可多条）
3. `already closed on CLOB; marking as CANCELED locally`
4. `Post-reset REST inventory sync completed for condition`
5. `[HARD RESET] Local state synced ... Resuming quoting`
6. 后续出现 `Diff quoting: create missing=...` 与 `POST /order 200`

若步骤 4 失败，会进入 `POST_RESET_RECONCILE_FREEZE`，此时仅允许卖出/减仓，属于保守模式，不是失控模式。

### 13.4 本次日志事件复盘（2026-03-23 20:45 段）

| 时间窗口 | 观察到的日志事实 | 判定 |
|----------|------------------|------|
| 20:45:09-20:45:11 | `630235` 连续 `CROSS-TOKEN LOCK` + `Payload: []` | 风控锁仓生效，主动不买入 |
| 20:45:11 | `HARD RESET` + cancel + 对账成功 + `Resuming quoting` | 自愈流程正常完成 |
| 20:45:11 后 | `244835/347224` 成功 `POST /order 200` | OMS/CLOB 通道健康 |
| 20:45:11 后 | `630235/980224` 仍反复 `LOCKED_BY_OPPOSITE` | 风险未回落，保护持续生效 |
| 全段 | 无持续 5xx 风暴；有选择性下单成功 | 非基础设施故障，属于策略保护 |

### 13.5 值班快速决策清单（Runbook）

1. 先看 `Mode`：若为 `LOCKED_BY_OPPOSITE`，优先按风控态处理，不按“交易故障”升级。  
2. 再看 OMS 通道：若 `POST/DELETE` 大量 `200`，说明链路正常。  
3. 看是否有 `HARD RESET -> Resuming quoting -> create missing -> POST 200` 闭环。  
4. 若出现 `POST_RESET_RECONCILE_FREEZE` 持续不消失，优先排查 Data API 对账与本地 ledger 漂移。  
5. 仅当 `Circuit breaker OPEN` 持续跨越恢复窗口且无成功单，再按通道故障处理。  

## 附录

### A. 文件结构树

```
PolyMatrixEngine/
├── app/
│   ├── main.py                    # FastAPI入口 (~395行)
│   ├── core/
│   │   ├── config.py              # Pydantic配置 (~66行)
│   │   ├── redis.py               # Redis客户端 (~47行)
│   │   ├── inventory_state.py     # 库存状态管理 (~318行)
│   │   ├── market_lifecycle.py    # 生命周期管理 (~169行)
│   │   └── auto_router.py         # 自动路由 (~500行)
│   ├── db/
│   │   └── session.py             # SQLAlchemy会话 (~30行)
│   ├── models/
│   │   └── db_models.py           # ORM模型 (~66行)
│   ├── market_data/
│   │   ├── gateway.py             # 市场数据网关 (~283行)
│   │   ├── user_stream.py         # 用户流 (~327行)
│   │   └── gamma_client.py        # Gamma API (~150行)
│   ├── oms/
│   │   └── core.py                # 订单管理 (~479行)
│   ├── quoting/
│   │   └── engine.py              # 报价引擎 (~1150行)
│   └── risk/
│       └── watchdog.py            # 风控 (~341行)
├── dashboard/
│   ├── app.py                     # Streamlit面板 (~1162行)
│   └── i18n.py                    # 国际化 (~240行)
├── alembic/
│   ├── env.py                     # 迁移配置
│   └── versions/                  # 迁移脚本
│       ├── 001_init_tables.py
│       ├── 002_add_token_ids.py
│       ├── 003_add_rewards_fields.py
│       ├── 004_add_reward_rate.py
│       └── 005_add_capital_used.py
├── docker-compose.yml             # Docker编排
├── Dockerfile                     # 容器定义
├── requirements.txt              # Python依赖
└── spec/
    └── technical-review-report.md # 本报告
```

### B. 关键配置项

```properties
# 数据库配置
# 应用默认值（本地开发）
DATABASE_URL=postgresql+asyncpg://postgres:${PASSWORD}@localhost:5432/polymatrix
# Docker Compose 覆盖值（容器网络）
# DATABASE_URL=postgresql+asyncpg://postgres:${PASSWORD}@postgres:5432/polymatrix

# Redis配置
# 应用默认值（本地开发）
REDIS_URL=redis://localhost:6379/0
# Docker Compose 覆盖值（容器网络）
# REDIS_URL=redis://redis:6379/0

# Polymarket配置
PM_API_URL=https://clob.polymarket.com
PK=${PK}
FUNDER_ADDRESS=${FUNDER_ADDRESS}

# 风控参数
MAX_EXPOSURE_PER_MARKET=50.0
GLOBAL_MAX_BUDGET=1000.0
BASE_ORDER_SIZE=10.0
GRID_LEVELS=2
QUOTE_BASE_SPREAD=0.02

# 自动路由
AUTO_ROUTER_ENABLED=false
AUTO_ROUTER_MAX_MARKETS=4
AUTO_ROUTER_SCAN_INTERVAL_SEC=3600
MAX_SLOTS_PER_SECTOR=2
MAX_EXPOSURE_PER_SECTOR=300.0
```

### C. 术语表

| 术语 | 解释 |
|------|------|
| Polymarket | 基于Polygon区块链的预测市场平台 |
| CLOB | Central Limit Order Book (中央限价订单簿) |
| Gamma API | Polymarket的市场元数据API |
| MTM | Mark-to-Market (盯市估值) |
| OBI | Order Imbalance (订单不平衡度) |
| Diff Quoting | 差分报价，仅变更必要订单 |
| Ghost Order | 幽灵订单，已提交但WS未确认的订单 |

---

*报告生成工具：Code Review Gen Skill*
*分析时间：2026-03-23（按当前主干代码复核）*
