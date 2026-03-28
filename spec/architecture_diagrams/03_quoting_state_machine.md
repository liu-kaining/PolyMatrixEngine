# QuotingEngine 状态机

```mermaid
stateDiagram-v2
    [*] --> QUOTING: start_market_making

    %% 正常报价状态
    state QUOTING {
        [*] --> NORMAL: 正常模式
        NORMAL --> QUOTING_BIDS_ONLY: 只报价 BUY
        QUOTING_BIDS_ONLY --> NORMAL: 恢复双向
    }

    %% 风险触发状态
    QUOTING --> LOCKED_BY_OPPOSITE: 对侧暴露 > 阈值
    LOCKED_BY_OPPOSITE --> QUOTING: 对侧平仓完成

    QUOTING --> LIQUIDATING: 手动平仓 / 止损
    LIQUIDATING --> EXTREME_LIQUIDATING: 极端行情
    EXTREME_LIQUIDATING --> LIQUIDATING: 回归正常
    LIQUIDATING --> QUOTING: 平仓完成

    %% 退出状态
    QUOTING --> GRACEFUL_EXIT: 赛道退出 / 事件地平线
    GRACEFUL_EXIT --> QUOTING: 重返市场
    GRACEFUL_EXIT --> POST_RESET_RECONCILE_FREEZE: 对账失败
    GRACEFUL_EXIT --> [*]: 完全退出

    %% 暂停状态
    QUOTING --> SUSPENDED: kill_switch / API暂停
    SUSPENDED --> QUOTING: API恢复

    %% 对账冻结
    POST_RESET_RECONCILE_FREEZE --> QUOTING: 对账成功
    POST_RESET_RECONCILE_FREEZE --> SUSPENDED: 对账持续失败

    state QUOTING {
        [*] --> QUOTING_BIDS_ONLY: 只买单
        QUOTING_BIDS_ONLY --> QUOTING: 恢复
    }

    note right of QUOTING
        主要状态:
        - QUOTING: 正常双向报价
        - QUOTING_BIDS_ONLY: 只买单(NO侧锁止)
        - LOCKED_BY_OPPOSITE: 对侧暴露锁定
        - LIQUIDATING: 平仓中
        - EXTREME_LIQUIDATING: 极端平仓
        - GRACEFUL_EXIT: 优雅退出
        - SUSPENDED: 暂停
        - POST_RESET_RECONCILE_FREEZE: 对账失败冻结
    end note
```

## 状态转换矩阵

| 当前状态 | 触发条件 | 目标状态 | 动作 |
|----------|----------|----------|------|
| QUOTING | `opposite_exposure > threshold` | LOCKED_BY_OPPOSITE | 暂停本侧 BUY |
| QUOTING | `kill_switch triggered` | SUSPENDED | 全部撤单 |
| QUOTING | `manual liquidate` | LIQUIDATING | 开启卖单 |
| QUOTING | `event_horizon reached` | GRACEFUL_EXIT | 平仓退出 |
| LOCKED_BY_OPPOSITE | `opposite_exposure normalized` | QUOTING | 恢复报价 |
| LIQUIDATING | `position < threshold` | QUOTING | 恢复报价 |
| LIQUIDATING | `extreme market` | EXTREME_LIQUIDATING | 加速平仓 |
| GRACEFUL_EXIT | `reconcile failed` | POST_RESET_RECONCILE_FREEZE | 冻结 BUY |
| GRACEFUL_EXIT | `exit complete` | [*] | 清理资源 |
| SUSPENDED | `api resume` | QUOTING | 恢复报价 |
| POST_RESET_RECONCILE_FREEZE | `reconcile success` | QUOTING | 恢复 BUY |

## 状态详细说明

### 1. QUOTING (正常报价)
```python
# 正常双向报价模式
# - YES 侧: SELL at FV+spread, BUY at FV-spread
# - NO 侧: 派生自 YES 锚点
# - 动态 spread: base_spread * (1 + |OBI|)
```

### 2. LOCKED_BY_OPPOSITE (对侧锁止)
```python
# 对侧持仓过高，暂停本侧买单
# 例: NO 侧持仓 > 50 shares → 暂停 YES 买单
# 等待对侧持仓下降或自动平仓
```

### 3. LIQUIDATING (平仓中)
```python
# 手动或自动触发平仓
# - 暂停新买单
# - 只开启卖单(清算持仓)
# - 严格 MTM 监控
```

### 4. EXTREME_LIQUIDATING (极端平仓)
```python
# 行情极端时的加速平仓
# - 更激进的卖单定价
# - 快速缩档策略
```

### 5. GRACEFUL_EXIT (优雅退出)
```python
# 满足以下条件时触发:
# - 赛道评分掉出 Top N
# - 事件地平线到达 (结算前 24h)
# - 只卖不买，逐步清仓
```

### 6. SUSPENDED (暂停)
```python
# kill_switch 或 API 暂停
# - 全部撤单
# - 禁止新单
# - 保持持仓
```

### 7. POST_RESET_RECONCILE_FREEZE (对账失败冻结)
```python
# 硬重置后对账失败
# - 禁止新 BUY
# - 保持现有持仓
# - 等待下次对账成功
```

---

*设计亮点: 完整的状态机覆盖所有边界场景，状态转换清晰可控*
