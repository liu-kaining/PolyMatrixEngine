# PolyMatrix Engine V6.4 - 架构图集

> 面向 Polymarket 的准机构级自动化做市与流动性引擎

## 📁 目录结构

```
spec/architecture_diagrams/
├── 00_INDEX.md                 # 图集索引
├── 01_system_overview.md       # 系统整体架构图
├── 02_module_relationships.md  # 核心模块关系图
├── 03_quoting_state_machine.md # 做市引擎状态机
├── 04_tick_processing_flow.md # Tick 处理流程
├── 05_differential_quoting.md  # 差分报价机制详解
├── 06_fill_processing_flow.md  # 成交处理流程
├── 07_risk_control_layers.md   # 多层风控体系
├── 08_watchdog_mechanism.md    # Watchdog 监控机制
├── 09_auto_router.md           # 自动路由与组合管理
├── 10_hard_reset_flow.md       # 硬重置流程
├── 11_database_erd.md          # 数据库实体关系
├── 12_plantuml_overview.puml   # PlantUML 架构图
├── 13_plantuml_state.puml      # PlantUML 状态机
├── 14_plantuml_risk.puml       # PlantUML 风控图
├── 15_system_overview.md       # 系统概览 (Mermaid)
└── README.md                   # 使用指南
```

## 🎨 专业配色方案

所有 Mermaid 图使用统一的专业沉稳配色：

| 用途 | 颜色 | Hex |
|------|------|-----|
| 主色调 | 深蓝 | `#1e3a5f` |
| 数据面 | 青色 | `#0891b2` |
| 核心引擎 | 紫色 | `#7c3aed` |
| 风控 | 红色 | `#dc2626` |
| 执行 | 绿色 | `#059669` |
| 路由 | 橙色 | `#d97706` |
| 基础设施 | 灰色 | `#64748b` |

## 🎯 技术亮点速览

### 1. 热路径零数据库读取
```
传统方案: DB 读取 ──→ 处理 ──→ 返回  (瓶颈在 DB)
PolyMatrix: 内存读取 ──→ 处理 ──→ 返回  (热路径零延迟)
```

### 2. 统一定价 Oracle
```
YES FV = clamp(mid + OBI × 0.015, 0.01, 0.99)
NO FV = 1 - YES FV
```

### 3. 差分报价 (业界领先)
```
精确匹配保留 ──→ 只撤不一致订单 ──→ 只补缺失档位
+ 三重保护: Lifetime(8s) + 价格偏移(0.005) + Rewards Band
```

### 4. 四层风控
```
L1: 报价前预检 (MAX_EXPOSURE_PER_MARKET = $50)
L2: Watchdog 硬熔断 (每秒检查)
L3: REST 周期对账 (60s 间隔)
L4: 硬重置强制对账 (5 分钟)
```

### 5. 自动路由评分
```
Score = daily_roi × rate × (10000/liquidity) × time_decay
```

## 📊 图集预览

### Mermaid 图渲染

所有 `.md` 文件包含 Mermaid 代码块，可直接在以下平台渲染：

| 平台 | 支持情况 |
|------|----------|
| GitHub | ✅ 原生支持 |
| GitLab | ✅ 原生支持 |
| 语雀 | ✅ 启用 Mermaid 插件 |
| Typora | ✅ 原生支持 |
| VS Code | ✅ Mermaid 插件 |
| Notion | ❌ 需第三方工具 |

### PlantUML 图渲染

`.puml` 文件需要 PlantUML 渲染器：

```bash
# 安装 PlantUML
brew install plantuml  # macOS

# 生成 PNG
plantuml -Tpng -dpi 300 12_plantuml_overview.puml

# 生成 SVG
plantuml -Tsvg 12_plantuml_overview.puml
```

## 🚀 使用建议

### 技术大会分享
- **推荐**: 使用 `01_system_overview.md` 作为主架构图
- **概览**: `15_system_overview.md` 快速了解全貌

### 详细技术讲解
- **入场**: `01_system_overview` + `02_module_relationships`
- **核心**: `03_quoting_state_machine` + `04_tick_processing_flow` + `05_differential_quoting`
- **差异化**: `06_fill_processing_flow` (内存优先设计)
- **风控**: `07_risk_control_layers` + `08_watchdog_mechanism`
- **高级**: `09_auto_router` + `10_hard_reset_flow`

### 面试/技术评估
- **架构**: `01_system_overview` + `02_module_relationships`
- **数据库**: `11_database_erd`
- **概览**: `15_system_overview.md` 快速概览

## 📝 导出为 PPT

### 方式一: Mermaid → 图片
1. 复制 Mermaid 代码到 [Mermaid Live Editor](https://mermaid.live)
2. 点击 "Actions" → "Export PNG"
3. 插入 PPT

### 方式二: PlantUML → 高清图
```bash
plantuml -Tpng -dpi 300 spec/architecture_diagrams/12_plantuml_overview.puml
```

---

*Generated for 技术大会分享 | PolyMatrix Engine V6.4*
