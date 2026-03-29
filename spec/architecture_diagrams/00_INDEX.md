# PolyMatrix Engine 架构图集

> 面向 Polymarket 的准机构级自动化做市与流动性引擎

## 图集索引

| 序号 | 图表名称 | 文件 | 类型 |
|------|----------|------|------|
| 01 | [系统整体架构图](./01_system_overview.md) | Mermaid | 架构图 |
| 02 | [核心模块关系图](./02_module_relationships.md) | Mermaid | 架构图 |
| 03 | [做市引擎状态机](./03_quoting_state_machine.md) | Mermaid | 状态机 |
| 04 | [Tick 处理流程](./04_tick_processing_flow.md) | Mermaid | 流程图 |
| 05 | [差分报价机制](./05_differential_quoting.md) | Mermaid | 流程图 |
| 06 | [成交处理流程](./06_fill_processing_flow.md) | Mermaid | 流程图 |
| 07 | [多层风控体系](./07_risk_control_layers.md) | Mermaid | 架构图 |
| 08 | [Watchdog 监控机制](./08_watchdog_mechanism.md) | Mermaid | 流程图 |
| 09 | [自动路由与组合管理](./09_auto_router.md) | Mermaid | 流程图 |
| 10 | [硬重置流程](./10_hard_reset_flow.md) | Mermaid | 流程图 |
| 11 | [数据库实体关系](./11_database_erd.md) | Mermaid | ER图 |
| 12 | [组件关系总览](./12_architecture_component_diagram.md) | Mermaid | 架构图（原 PlantUML 总览） |
| 15 | [系统概览](./15_system_overview.md) | Mermaid | 架构图 |

## 技术亮点

### 1. 热路径零数据库读取
- Tick 逻辑只读内存 `InventoryStateManager`
- 成交先更新内存，异步队列持久化
- 有界队列 `maxsize=1000` + 关闭排空

### 2. 统一定价 Oracle
- YES 侧计算 FV 并发布到 Redis
- NO 侧消费锚点派生
- 动态 spread 基于 OBI

### 3. 抗干扰差分报价
- Lifetime 保护：成交后 8 秒内不撤
- 价格偏移保护：价格移动 < threshold 时不撤
- Rewards Band 保护：订单仍在奖励带内时不撤

### 4. 多层风控体系
- 报价前预检 → Watchdog 硬熔断 → REST 周期对账（`RECONCILIATION_INTERVAL_SEC`，默认 3600s）→ 硬重置强制对账

### 5. 赛道隔离与事件地平线
- `MAX_SLOTS_PER_SECTOR` / `MAX_EXPOSURE_PER_SECTOR`
- 结算前 24h 自动 graceful_exit

## 投资人与对外叙事

| 文档 | 说明 |
|------|------|
| [投资人技术文章（中文）](../investor_technical_article.md) | 架构理念、风控与选市场叙事；非实现级白皮书 |

---

*Generated for 技术大会分享 | PolyMatrix Engine V6.4*
