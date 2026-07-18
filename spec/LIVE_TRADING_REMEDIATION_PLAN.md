# PolyMatrix Engine 实盘亏损整改与架构重构计划

> 状态：静态代码审查结论，未启动服务、未连接 Polymarket、未执行任何订单操作。
>
> 结论：当前版本不具备继续实盘的安全条件。在完成 P0 整改和离线验收前，实盘状态必须视为 **NO-GO**。

## 1. 目标与边界

本计划解决的不是简单的参数调优，而是以下四个根问题：

1. 交易目标从“满足奖励挂单条件”改为“风险调整后的净收益为正”。
2. 订单、成交、仓位和盈亏必须形成可重放、可核对、幂等的事实账本。
3. 所有下单都必须经过原子资金预留与组合级风险闸门，任何状态不确定均进入 Reduce-Only 或 Halt。
4. 实盘准入必须由自动化证据驱动，不能再以服务能运行、能挂单作为上线标准。

本次静态审查可以确认代码缺陷和风险放大器，但不能仅凭代码断言每一笔历史亏损的精确原因。精确归因需要后续导入订单、成交、盘口和奖励历史，进行离线重放与损益分解；这一过程不需要也不允许产生实盘订单。

## 2. 重大问题总览

### 2.1 P0：策略没有可验证的盈利边际

当前公允价只是 YES 最优买卖价中点加一档盘口不平衡：

```text
FV_yes = mid_yes + top_level_OBI * 0.015
spread = base_spread * (1 + abs(OBI))
```

它存在以下根本问题：

- 信号和成交市场来自同一个盘口，没有外部信息优势，也没有证明 OBI 能覆盖逆向选择。
- 只使用最优一档数量，容易受到撤单、诱导流动性和瞬时噪声影响。
- 只以 YES 盘口作为锚，没有利用 NO 盘口深度、YES/NO 平价偏差和跨 outcome 流动性质量。
- 没有波动率、短期趋势、成交毒性、盘口年龄、深度、跳价和事件风险溢价。
- 没有库存倾斜；持仓增加后报价中心没有系统性向减仓方向移动。
- 没有对成交后的 1s/5s/30s markout 做记录，无法识别自己是否长期被更快、更有信息优势的交易者击中。
- 奖励模式会把点差压到奖励阈值；当奖励允许点差小于基础点差时，当前 `min(max(dynamic, base), target)` 实际会选择更窄的 `target`。奖励资格覆盖了最低盈利点差。

这意味着系统可能稳定获得挂单或成交，却在每次成交后承担超过点差和奖励收入的价格损失。

### 2.2 P0：自动选池优化的是奖励指标，不是净收益

当前 Router 的核心排序近似为：

```text
score = reward_rate * (reward_rate / min_size) / competition_penalty * capital_scale
```

它没有纳入：

- 本账户实际可获得的奖励份额；
- 成交概率和库存周转速度；
- 历史 adverse markout；
- 买卖价差收益、退出滑点与资金占用时间；
- 市场深度、盘口稳定性、价格极值和事件跳跃风险；
- 已实现及未实现亏损；
- 解析争议、临近重大事件和缺失元数据的风险。

结果是“奖励池大”可能被误当成“值得交易”。奖励很可能只是对高竞争、高毒性或高库存风险的补偿，不代表账户净收益为正。

### 2.3 P0：订单与成交账本不具备 exactly-once 语义

当前成交处理直接把每次 WS 事件中的 `filled_size` 累加到订单和库存：

- 没有以 trade id / transaction id 建立唯一约束；重复或重放事件会重复计仓。
- 没有区分累计成交量与本次增量成交量。
- 真实订单返回前先使用本地临时 order id；若成交事件先于数据库主键替换到达，事件会找不到订单并被直接丢弃。
- 成交处理是多个 fire-and-forget task，订单级别以外没有事件序列和持久 inbox。
- SELL 没有防止重复事件导致库存变负。
- 持久化队列满时会丢弃数据库写入，重启后可能恢复到旧仓位。

这是实盘系统的基础正确性问题：一旦本地仓位低估，系统可能继续买入；一旦高估，则可能错误卖出或长期停止报价。

### 2.4 P0：风险系统只控制名义额度，没有控制损失

现有 Watchdog 主要检查 `capital_used`，但缺失：

- 账户净值、未实现 PnL、总 PnL、日内损失和最大回撤；
- 单市场 stop-loss、组合 loss limit、连续毒性成交阈值；
- 价格跳跃、盘口失效、连接异常时的统一风险状态；
- 全局预算超限后的实际 kill action；当前代码只写 CRITICAL 日志。

同时存在确定的完整性缺陷：

- 全局预算只汇总已经加载进进程内存的市场；重启后的冷仓位可能不计入全局风险。
- 对账发现“本地为 0、远端大于 0”的仓位时，`capital_used` 不会被重建，可能继续保持为 0。
- Watchdog 只遍历仍在运行的 engine；退出中、冷仓位和无 engine 仓位不进入每秒硬检查。
- YES/NO engine 各自计算余额并并发下单，没有组合级原子 reservation，存在同时通过检查后共同超限的 TOCTOU 竞态。
- 差量报价的 anti-churn 会保留已不在目标集合中的 BUY，只要其年龄、价格偏差或奖励带条件满足；风险模式不一定立即撤掉旧 BUY。

### 2.5 P0：Periodic Hard Reset 会制造未知订单状态

每个引擎周期性触发 wallet-wide `cancel_all`，随后本地强制清除订单缓存并重新挂单。当前实现有几个危险组合：

- `cancel_all` 失败或超时后流程仍继续。
- `cancel_all_ok` 没有成为重新买入的必要条件。
- `cancel_all_orders(force_evict=True)` 即使单笔撤单失败也会从本地缓存删除。
- 仓位对账只核对 position，不核对仍在交易所开放的 order。
- 之后预算被释放并可能创建新单，交易所 ghost order 与新单可能同时存在。
- wallet-wide 撤单会影响其他市场；其他 engine 的本地缓存和交易所状态可能瞬间分叉。

这条链路可以直接造成重复挂单、预算低估、时间优先级丢失和跨市场状态错乱。应删除这种“定时清空一切”的机制，改为交易所开放订单的权威增量对账。

### 2.6 P0：退出逻辑可能把正常亏损放大为灾难性滑点

当前存在两种激进退出：

- 极端库存时 SELL 价格使用 `best_bid - 0.02`。
- `/liquidate` 直接以 `0.01` 提交全部持仓 SELL。

后者没有盘口深度检查、最大滑点、最小可接受回收金额、分批执行或成交结果确认，并且忽略并发任务异常后仍返回 `liquidating`。在薄盘口上，它会把“需要退出”实现成“允许任何可成交价格出售”。

此外，Graceful Exit 把小于 5 shares 的持仓直接当作完成，但并没有真正处理、合并或赎回该仓位。

### 2.7 P0：行情可信度不足，旧盘口也可能触发交易

本地 orderbook 没有：

- exchange sequence / hash 连续性检查；
- snapshot 版本和 event 去重；
- 每个 asset 的最后更新时间；
- 最大盘口年龄；
- reconnect 后强制 REST 重播/校验；
- crossed book、异常价差、深度骤降和价格跳跃保护。

QuotingEngine 收到 tick 后直接使用。30 秒 socket timeout 只能说明连接是否安静，不能证明当前某个 token 的盘口仍然新鲜和完整。

### 2.8 P0：多 outcome 市场被错误简化为 YES/NO

Gamma 层记录了 `outcome_count`，但生命周期只取 `tokens[0]` 和 `tokens[1]`，并始终创建两个 engine。Router 不拒绝 categorical market，只给它更小额度。

这不是“更保守地支持多 outcome”，而是把多 outcome 合约错误映射为二元合约。整改前必须明确只允许 `outcome_count == 2`；真正的 categorical 支持应作为独立设计，不可通过额度缩小代替正确建模。

### 2.9 P0：Router 在退出完成前释放了槽位和风险容量

Router 发送 `graceful_exit` 后立即从本地 `active_set`、sector slots 和 start-time 状态中删除市场，然后可以启动新市场。但旧 engine 可能仍在持续减仓，持仓和退出单仍然存在。

这会造成：

- 活跃市场数和真实运行 engine 数不一致；
- 退出中仓位不计入 Router 的 sector slot；
- 新市场与旧市场同时消耗资本；
- 组合容量在最需要保守时被提前释放。

此外，当扫描成功但目标列表为空时，当前主循环不调用 rebalance，已有市场不会执行 event-horizon 检查。

### 2.10 P1：盈亏展示不是会计意义上的 realized PnL

当前逻辑在 BUY 时把成本从 `realized_pnl` 扣除，在 SELL 时把卖出收入加回。因此该字段更接近累计交易现金流，而不是已实现盈亏；持有未平仓仓位时，Dashboard 会把资产购买成本显示成“已实现亏损”。

系统还缺少：

- 平均成本和逐笔 lot；
- realized / unrealized / mark-to-market PnL 分离；
- 奖励、返佣、gas、费用和滑点归因；
- 按市场、策略、时间窗口和退出原因的损益分解；
- 账户净值与交易所余额对账。

没有正确的盈亏账本，Router 和风控都无法基于真实经济结果决策，也无法准确解释历史亏损。

### 2.11 P1：生命周期、管理面和部署安全不足

- `/stop` 只 suspend，不终止 engine task；再次 `/start` 会返回 `already_running`，但 engine 仍可能保持 suspended。
- API 的 start、stop、liquidate、wipe 均无认证授权。
- `/admin/wipe` 不先停止 engine，就删除数据库和 Redis；运行中的 task 仍可能继续工作。
- health 始终返回 `ok`，不检查行情新鲜度、用户流、数据库、Redis、对账状态、未知订单或风险闸门。
- Docker 构建使用 `COPY . .`，仓库没有 `.dockerignore`，存在把 `.env`、虚拟环境和 Git 历史打进镜像的风险。
- 依赖未锁定；无测试套件、CI、覆盖率、静态质量门禁和数据库迁移演练。

## 3. 目标架构

```text
Market Data Ingest
  -> Versioned Book Store
  -> Freshness / Sequence / Sanity Gate
  -> Pure Quote Decision Engine
  -> Portfolio Risk + Atomic Reservation
  -> OMS Command Outbox
  -> Exchange
  -> Durable Event Inbox
  -> Idempotent Fill Processor
  -> Positions / Lots / PnL / Risk State

Router -> Candidate Risk Filter -> Expected Net Edge Scorer -> Lifecycle State Machine
                                                      |
                                               Portfolio Risk Gate
```

核心原则：

1. **交易所事实优先**：开放订单、成交和仓位必须可与交易所状态核对。
2. **命令与事件分离**：下单/撤单是命令，订单/成交更新是事件；都需要持久化状态机。
3. **所有事件幂等**：重复、乱序、延迟、重连均不能改变最终会计结果。
4. **资金先预留再下单**：预留、下单和释放必须形成状态机，不能靠两个 engine 各自估算。
5. **不确定即降级**：行情、订单、仓位或连接不确定时只允许撤单和减仓。
6. **奖励是附加收益**：只有预期交易净收益不为负时才考虑奖励资格。
7. **退出中仍占用容量**：直到订单清零、仓位归零或进入显式 residual 状态前，不释放 slot 和预算。

## 4. 分阶段整改计划

### Phase 0：止血与实盘互锁（P0）

目标：任何误操作、配置漂移或服务重启都不能自动进入真实交易。

修改项：

- 引入 `TRADING_MODE=disabled|paper|live`，默认且镜像内固定为 `disabled`。
- live 模式要求独立的短期 arm token、账户白名单、预算上限和启动确认；仅设置一个布尔变量不能解锁。
- live 启动前必须通过 readiness gate：DB/Redis、用户流、行情新鲜度、订单对账、仓位对账、风险账本完整性全部为真。
- 奖励排名 Router 永久限制为 paper-only；live 模式不提供解锁开关。
- 移除或隔离 `/admin/wipe`；管理 API 增加认证、权限和审计日志。
- 增加 `.dockerignore`，明确排除 `.env`、密钥、venv、`.git`、日志和本地数据。
- 增加一键 `halt` 状态：停止新单、撤销已知订单、保持风险状态，不自动清数据库。

验收门槛：

- 任意缺失 readiness 条件都无法进入 live。
- 重启、容器重建和任何 Router 配置都不会绕过 live arm；奖励排名 Router 在 live 下始终被拒绝。
- 管理接口未认证时全部拒绝。
- 镜像层和构建上下文不包含 `.env` 或密钥文件。

### Phase 1：重建 OMS、成交与会计事实层（P0）

目标：订单、成交、仓位与 PnL 可幂等重放并保持一致。

数据模型：

- `order_commands`：client_order_id、exchange_order_id、intent、reservation_id、状态、版本。
- `order_events`：exchange event id、event type、exchange timestamp、received timestamp、raw payload，唯一约束防重。
- `fills`：trade id、order id、token id、side、price、size、fee、transaction id，唯一约束防重。
- `position_lots` 或等价平均成本表。
- `cash_ledger`、`reward_ledger`、`pnl_snapshots`。
- `risk_reservations`：NEW、COMMITTED、PARTIALLY_FILLED、RELEASED、UNKNOWN。

实现项：

- 下单前生成稳定 client order id；本地 id 与 exchange id 不再通过修改主键完成。
- 使用 transactional outbox/inbox；先持久化命令，再发送；事件先持久化再处理。
- Fill Processor 以 trade id 幂等，支持乱序、重复和累计成交量转换。
- 将订单状态机扩展为 CREATED、SUBMITTING、OPEN、PARTIAL、FILLED、CANCELING、CANCELED、REJECTED、UNKNOWN。
- UNKNOWN 状态保留资金 reservation，并阻止增加风险，直到开放订单对账确认。
- 重写 PnL：realized、unrealized、cash flow、reward、fees、slippage 分离。
- 持久化失败不得静默丢弃；进入 Halt/Reduce-Only 并告警。

验收门槛：

- 相同 fill 事件重复 100 次，仓位和 PnL 只变化一次。
- 任意事件乱序排列后的最终状态一致。
- 成交早于下单响应、撤单与成交并发、部分成交后断线均能恢复。
- 进程在任意状态点崩溃后重启，离线重放得到同一账本结果。
- `cash + MTM positions + rewards - costs` 能与账户权益变化对齐。

### Phase 2：组合级风险引擎与原子资金预留（P0）

目标：风险由一个组合级单一决策点控制，而不是分散在两个 token engine 中。

实现项：

- 启动时加载全部非零仓位、开放订单和 reservation；完成权威同步前禁止新增风险。
- 原子 `reserve -> submit -> commit/release`，按 wallet、sector、event、condition、token 多层限额。
- 全局预算超限必须触发实际状态迁移，不再仅记录日志。
- 风险状态机：RUNNING、REDUCE_ONLY、HALT、RECONCILING、MANUAL_RECOVERY。
- 增加单市场/组合最大未实现亏损、日内损失、最大回撤、连续毒性成交、最大未知订单数。
- 所有退出中、冷仓位、residual 和无 engine 仓位继续占用资本与风险限额。
- 对账发现未知仓位时使用保守成本估计，并立即进入 Reduce-Only，不能把 `capital_used` 留为 0。
- anti-churn 在风险收缩时必须失效：期望 BUY 集合为空时立即撤掉全部 BUY。
- 风险判定使用当前 MTM、最坏可执行价值和 cost basis 三个口径，不再只看历史 `capital_used`。

验收门槛：

- YES/NO 与多个市场并发申请预算时，总 reservation 永不超过全局上限。
- 重启后冷仓位必然进入风险汇总。
- 撤单超时、用户流断开、仓位不一致时不能新建 BUY。
- 所有 risk breach 都有确定的自动动作、状态记录和人工恢复步骤。

### Phase 3：行情质量层与安全报价引擎（P0/P1）

目标：只在盘口可信、预期净边际充足时挂单。

行情层：

- 为每个 token 保存 snapshot id/sequence、exchange timestamp、receive timestamp 和 book checksum。
- sequence gap、重连、crossed book、异常跳价时标记 INVALID，强制重拉快照。
- 配置最大 book age；超时立即撤单并进入 Reduce-Only/Halt。
- 同时校验 YES 与 NO 的盘口、深度和互补关系。

报价层：

- 把策略计算重构成无副作用纯函数，输入 market snapshot + inventory + risk + configuration，输出 quote intents 和 reason codes。
- 公允价使用双方盘口的深度加权 microprice/parity，并允许接入独立 alpha；没有独立优势时采用保守 maker 模式。
- 增加 inventory skew：持仓越大，买价越保守、卖价越积极，但不得突破最大滑点。
- 增加 volatility、momentum、orderbook depletion、recent fills、markout toxicity 和 event risk spread。
- 最小允许点差必须覆盖预期 adverse selection、费用、滑点和目标利润；奖励阈值不能缩窄它。
- 每笔报价记录 decision snapshot 和 reason code，支持离线解释。
- 固定 `tick_size=0.01` 改为按市场读取并校验。

验收门槛：

- 旧盘口、序列断裂、跨盘、深度不足、异常跳价均不能产生新增订单。
- 库存增加时风险方向的报价单调收缩。
- 奖励点差小于安全点差时必须放弃奖励，而不是降低安全标准。
- 离线回放可以逐笔解释“为何挂、为何撤、为何不挂”。

### Phase 4：安全执行与退出重构（P0）

目标：撤单不确定不会释放风险；退出不会无上限地牺牲价格。

实现项：

- 删除周期性 wallet-wide hard reset。
- 增加开放订单权威对账：本地已知、交易所已知和 UNKNOWN 三方差集处理。
- 撤单成功必须由交易所状态或终态事件确认；失败/超时保持 reservation。
- 默认 maker-only/post-only；是否允许 taker 由明确 execution policy 控制。
- 退出使用盘口深度和最大滑点分批执行，支持 deadline、participation rate、最小回收价值和人工确认。
- 删除 `0.01` 全量抛售和固定 `best_bid - 0.02`；任何越过滑点上限的退出都必须停止并告警。
- 小于最小订单量的 residual 进入独立状态，等待合并、赎回、补量或人工处理，不能伪装成 exposure cleared。
- cancel-all 仅作为人工灾难恢复工具，并在执行后强制做 orders + positions 双对账。

验收门槛：

- 任意撤单失败都不会导致同一预算被再次使用。
- 退出价格永不突破配置的最大滑点/最小回收价值。
- 退出完成必须同时满足：开放订单为零、reservation 为零、仓位低于可解释 residual 阈值。

### Phase 5：Router 与生命周期重写（P1）

目标：只选择结构正确、预期净收益为正且组合可以承受的市场。

实现项：

- 第一阶段只支持严格二元市场；`outcome_count != 2` 直接拒绝。
- 缺失 end date、tick size、token mapping、深度或奖励元数据时 fail closed。
- 候选硬过滤：盘口深度、book age、价格区间、event horizon、解析风险、波动率、毒性、最低净 edge。
- Score 改为预期净收益：spread capture + expected reward share - adverse selection - exit cost - capital charge。
- 预期奖励份额必须基于竞争和本账户报价规模估算，不能使用整个 reward pool 作为收益。
- 引入策略反馈：持续负 markout 或负净 PnL 的市场自动降权/禁用。
- 生命周期显式化：DISCOVERED -> VALIDATED -> STARTING -> ACTIVE -> REDUCE_ONLY -> EXITING -> FLAT/RESIDUAL -> STOPPED。
- EXITING 未完成前不释放 slot、sector exposure 或 budget。
- 空目标列表仍执行已有市场的安全检查与退出判断。
- min-hold 不能阻止 stop-loss、毒性保护和数据失效退出。

验收门槛：

- categorical market 无法进入 engine。
- EXITING 市场持续计入组合限制，直到终态。
- reward 高但预期净收益为负的市场必然被拒绝。
- Router 的每次选择和淘汰都带可审计 reason codes。

### Phase 6：可观测性、控制面与运维治理（P1）

实现项：

- health/readiness 分离；readiness 覆盖 market WS、user WS、book age、DB、Redis、OMS、reconciliation、risk state。
- 指标：订单延迟、撤单延迟、未知订单、重复事件、对账差异、仓位、reservation、PnL、drawdown、markout、spread capture、退出滑点。
- 结构化日志和 correlation id：market、token、client order、exchange order、trade、decision。
- 管理 API 使用认证授权、幂等请求、CSRF/网络隔离和审计记录。
- Dashboard 明确区分 shares、USDC cost、MTM、realized/unrealized PnL、rewards 和风险状态。
- 锁定 Python 与依赖版本；增加 migrations check、lint、type check、unit/integration/property/replay tests 的 CI。
- 修正文档版本、默认参数、真实状态机和部署前置条件。

## 5. 测试与验证策略（禁止实盘订单）

所有整改首先只进行离线验证：

1. **Unit tests**：报价、风险、PnL、状态机全部纯函数化测试。
2. **Property tests**：库存不为负、幂等、预算不超限、PnL 恒等式、退出不新增风险。
3. **Deterministic replay**：使用保存的行情与用户事件，重复运行必须得到相同订单意图与账本。
4. **Fault injection**：重复事件、乱序、延迟、WS 断线、REST 过期、撤单超时、DB 重启、Redis 丢失。
5. **OMS fake exchange**：模拟部分成交、立即成交、撤单撞成交、未知订单和 API 失败。
6. **Migration tests**：从当前数据库结构升级，验证历史订单和库存不会丢失。
7. **Security tests**：未授权管理请求、镜像密钥扫描、危险默认配置。

在用户重新明确授权之前，不把真实下单、微额试单或实盘 shadow execution 作为验收手段。

## 6. 实盘准入硬门槛

以下条件必须全部满足；任何一项失败都保持 NO-GO：

- 所有 P0 项关闭，且有自动化回归测试。
- 订单/成交事件在重复、乱序、断线场景下保持 exactly-once 会计结果。
- 启动完成 orders + positions 权威对账前无法新增风险。
- 原子 reservation 在并发和崩溃恢复测试中从不突破组合限额。
- 行情失效、用户流失效、未知订单和对账差异会自动进入 Reduce-Only/Halt。
- realized/unrealized/reward/fee/slippage 可独立核对。
- 离线回放样本覆盖高波动、薄盘口、部分成交、断线和退出，并通过全部风险不变量。
- Router 只选择严格二元、元数据完整、预期净收益为正的市场。
- 没有无上限滑点退出路径。
- live 模式需要显式 arm，默认配置、镜像和重启路径均不能自动实盘。
- 完成独立代码审查、威胁建模、回滚演练和操作手册。

满足这些门槛只能说明系统达到“可受控试运行”的工程标准，不能保证盈利。是否具有持续盈利能力，必须由独立的离线历史回放、费用与奖励归因、markout 分析和统计显著性证明。

## 7. 推荐实施顺序与提交边界

建议按以下独立变更批次推进，避免在一个巨型提交中同时更换所有行为：

1. `safety-interlock`：交易模式、live arm、readiness、管理面保护、`.dockerignore`。
2. `event-ledger`：订单命令、事件 inbox、fills 幂等、PnL 账本和迁移。
3. `portfolio-risk`：全量启动恢复、原子 reservation、风险状态机和 loss limits。
4. `market-data-integrity`：sequence、freshness、快照校验和 fail-closed。
5. `safe-execution`：删除 hard reset、订单权威对账、深度感知退出。
6. `quote-engine-v2`：纯函数报价、双方盘口、inventory skew、toxicity、profitability gate。
7. `router-v2`：binary-only、净收益评分、正确生命周期和组合反馈。
8. `observability-and-ops`：指标、Dashboard、认证、CI、依赖锁定和文档。

每个批次必须包含迁移、测试、回滚方式和行为变更说明；前一个批次未通过门槛时，不进入后一个涉及实盘行为的批次。

## 8. 历史亏损离线归因清单

如果后续提供历史数据，按以下维度离线分解，不进行实盘验证：

- 每笔成交前后的 1s/5s/30s/5m markout；
- maker spread capture 与退出 slippage；
- YES/NO 库存不对称持续时间和方向性损失；
- reward/rebate 与交易损失的净额；
- hard reset 前后开放订单数量、重复挂单和资金占用；
- WS 事件重复、丢失、乱序与 REST 对账修正；
- `realized_pnl` 展示值与真实 realized/unrealized PnL 差异；
- Router 入选市场的 reward score 与实际净收益相关性；
- extreme taker 和 `/liquidate` 产生的滑点贡献；
- 重启、断线、撤单失败期间的仓位及订单偏差。

这一归因结果应决定 Quote Engine V2 和 Router V2 的参数与模型，而不是继续凭直觉调整点差和订单大小。

## 9. 当前实施进度

### 已完成：Phase 0 safety-interlock

- `TRADING_MODE=disabled|paper|live`，旧布尔变量不能单独解锁实盘。
- wallet + expiry + budget 绑定的 24h 内短时 arm、钱包白名单和 live budget ceiling。
- DB、Redis、OMS、Market WS、行情完整性、User WS、positions、open orders、risk reservations、risk monitor、accounting integrity、alpha evidence 十二项运行时 readiness。
- `open_orders_reconciled` 在权威订单对账落地前强制为 false，因此当前仍无法发送真实新增订单。
- 管理写接口 Bearer Token；sticky `/admin/halt`；wipe 默认关闭并增加三重保护。
- 旧 `0.01` liquidation 与 wallet-wide Periodic Hard Reset 执行路径均已删除。
- `.dockerignore` 排除密钥和本地资产；Dashboard 不再挂载包含私钥的 `.env`。

### 已完成：Phase 1 第一批事实层

- 新订单保留稳定 local/client primary key，单独保存 `exchange_order_id`。
- 新增 `fill_events` durable inbox；确定性 event id 防止重复成交事件重复记账。
- 成交先落 inbox，再以订单和库存行锁事务处理；早到成交进入 `UNMAPPED`，订单映射完成后重试。
- 新增 `state_version`，避免并发事务提交后的旧内存快照覆盖新状态。
- realized PnL 改为 fee-aware average-cost：BUY 成本包含明确手续费，SELL 只实现已关闭 lot 的净盈亏。
- 历史错误口径标记为 `accounting_version=v1`，必须离线重建，禁止与 v2 继续混算。

### 已完成：Phase 2 部分止血

- 启动时加载全部库存账本，冷仓位进入组合风险汇总。
- Watchdog 监控活动和冷仓位；全局预算超限执行 sticky Halt、全市场 suspend/cancel，不再只记录日志。
- 对账发现本地为零或成本为零的外部仓位时，使用最坏 $1/share 风险资本、标记旧会计口径并 Halt。
- 外部存在但本地完全没有 ledger 的仓位会阻止 positions readiness。
- 新增 wallet singleton 行锁与 `risk_reservations`：并发 BUY 先原子预占 USDC，SELL 先原子预占可卖 shares。
- reservation 与订单 journal 绑定；部分成交在同一事务内扣减 reservation、更新成本/仓位、订单状态与 fill inbox。
- 网络异常或非确定提交响应进入 `UNKNOWN`；撤单后的剩余额度进入 `CANCEL_PENDING_RECONCILE`，在权威对账前不释放。
- Watchdog 的硬限额口径统一为全部冷/热仓位 cost basis + durable BUY reservations。

### 已完成：Phase 3 第一批行情完整性

- 本地订单簿对全量快照和 delta 做有限数、价格/数量边界、双边流动性和 crossed/locked 检查。
- 保存 receive timestamp、exchange timestamp、sequence 和 snapshot id；sequence 回退丢弃、gap 要求全量重同步。
- 行情超过最大年龄、WS 断开或完整性失败时，向引擎发布 invalid tick，撤已知订单并暂停报价。
- live 默认要求 exchange sequence 与 timestamp；数据源不提供时 readiness 失败关闭。

### 已完成：安全退出与新风险准入第一批

- 永久删除 `0.01` 全量抛售实现与 Dashboard 操作，旧 `/liquidate` 固定返回 HTTP 410。
- 删除 QuotingEngine/OMS 中 wallet-wide `cancel_all`、失败也 `force_evict` 本地订单的 Hard Reset 实现及全部配置入口。
- 极端/退出 SELL 改为只消耗最大冲击范围和成本亏损下限内的可见买盘深度，不满足时等待并告警。
- `OFFLINE_VALIDATED_ALPHA_ENABLED=False` 默认关闭所有新增 BUY 风险。
- 单一布尔开关不再构成 alpha 证明；新增内置离线评估器与 `alpha-evidence-v2` 本地 JSON + SHA-256 内容固定，作为 live readiness 的独立硬门槛。
- 证据必须满足样本外、费用 100% 完整、去重/数据完整性/未来函数检查通过、最小 1000 fills/20 markets/30 天、扣除 rewards 后交易 PnL 为正、PnL 与 30s markout 的 95% 下界非负/为正，以及最大回撤比例上限。
- 即使显式开启，新 BUY 仍必须通过执行成本、逆向选择与最小净边际门槛；reward 不进入交易准入。
- 风险收缩或经济性不成立时，旧 BUY 不再被 anti-churn 或 rewards band 保留。
- evaluator 从严格原始 fills、30s mark、terminal mark 与策略权益曲线计算 fee-aware cash+terminal-mark PnL、市场聚类 bootstrap 下界和回撤；输入结构拒绝 rewards 字段、重复 fills、未来函数、超卖和未标记持仓。
- 证据绑定 `ALPHA_STRATEGY_ID`、规范化运行参数 SHA-256、关键交易源码 bundle SHA-256 和 `APP_CODE_COMMIT`；代码或参数变化会让旧报告立即失效。
- 奖励驱动 size/spread/SELL 保活已从执行路径删除；奖励排名 Auto-Router 在 live 模式无条件拒绝。
- Dashboard 的奖励/流动性排名改为明确的 research telemetry，删除“最推荐/收益/安全”措辞，并禁止从该榜单直接启动策略。
- live 静态准入校验最小订单、网格、点差/成本缓冲、退出损失、行情时效、市场/全局额度及 sequence/timestamp 开关的合法范围。

### 已完成：异常与生命周期加固

- 交易 SDK 与 API 凭据改为本地审计/arm 之后延迟导入和初始化；disabled/paper/普通模块导入不接触交易所。
- 远端 submit 后本地状态提交失败会保留 reservation 并 sticky Halt；撤单响应必须明确包含目标 exchange order id，不能用无关 canceled 列表或泛化 success 猜测。
- diff 报价只有在所有旧单撤单确认后才允许补新单；消息解析/策略处理异常会 suspend、撤单并 sticky Halt。
- engine finally、单市场 stop、手动 halt、全局 Kill 和进程 shutdown 都覆盖撤单确认；手动 halt/全局 Kill 也扫描没有活动引擎的冷订单。
- 删除未使用的 memory-first fill + async persist queue，库存内存层只接受 DB 已提交版本快照。
- Watchdog 加入 live readiness；循环异常、限额突破或撤单不完整均 fail-closed；最近 fill 导致 REST 对账延期时 positions readiness 保持 false。
- 活跃市场越线时即使数据库已经是 `suspended` 也会重新广播暂停，避免重启/陈旧引擎漏掉 Kill 信号。
- `/admin/wipe` 禁止在存在未决订单或非零持仓时清除本地恢复证据。

### 已完成：开放订单权威对账第一批

- 新增 `order_reconciliation_runs` 与 `exchange_order_snapshots`，持久化每次权威对账结论和原始订单事实。
- live 启动时使用认证 `get_orders()` 获取开放订单；对本地存在但不在开放列表的订单，必须再用 `get_order()` 获取终态。
- 只有 exchange terminal 状态、累计 matched size、本地已入账 fill 和 reservation 全部一致，才允许关闭订单或释放剩余额度。
- exchange 仍开放时，按权威 remaining size 校准 BUY notional 或 SELL shares reservation。
- 外部孤儿开放订单、缺失成交、identity/side/price/size 冲突、无 exchange id 与不支持的终态全部进入 `UNKNOWN` 并 Halt。
- 新增认证管理入口用于无活动引擎时重新对账，以及不暴露 raw payload 的审计查询。
- 新增 GitHub Actions 离线安全流水线：Ruff、compile、unit tests、全量 migration SQL 渲染和 Compose 配置解析。

### 已完成：会计完整性与 PnL 可信度第一批

- 新增 `fill_cash_ledger`，每个已处理 fill 在同一事务内生成唯一、不可变、带方向的 gross cash fact。
- 只接受 payload 中明确的绝对手续费字段；不会根据未经验证的 bps/费率猜金额，也不会把缺失手续费默认为 0。
- 明确手续费在 BUY 时资本化到持仓成本，在 SELL 时从成交收入扣除；`v2` 的 realized PnL 因此是平均成本净已实现损益。
- 缺失手续费时现金账保留 `UNKNOWN`/空 net cash，库存降级为 `v2_fee_incomplete`，会计 readiness 失败并 sticky Halt。
- 每个 fill 保存对应 `accounting_state_version`；启动及管理端审计按版本从零确定性重放全部 v2 fill，核对 YES/NO exposure、cost basis 和净 realized PnL。
- 任意未处理 fill、cash fact 缺失/孤儿、版本断档、非法映射、cash identity/notional 不一致、旧账或远端覆盖都会阻止会计 readiness。
- Data API 仓位差异不再被当作成功账务修复；任何外部数量覆盖都标记 `unverified_external` 并 Halt。
- Risk API 和 Dashboard 仅在最新审计为 `SAFE` 且账本为 `v2` 时展示净已实现 PnL，否则返回/显示 `N/A`。
- live 静态授权新增 `LIVE_FEE_ACCOUNTING_VALIDATED`，默认 false；未完成真实 adapter 费用字段契约测试前无法解锁。

### 尚未完成

- 对 `MISSING_FILLS` 的认证 trade history 回填，以及无 exchange id 提交结果的人工恢复流程。
- 真实 CLOB payload/状态契约验证；按用户要求当前未进行任何实盘或在线验证。
- 真实成交 payload 的手续费字段、单位、payer 和舍入规则契约测试；当前仅实现明确绝对费用字段的保守 adapter。
- 行情 checksum 独立计算、YES/NO 互补盘口校验与故障注入回放。
- residual 人工恢复、退出成交跟踪和完整 lifecycle。
- Quote Engine V2 的外部/独立 alpha、库存倾斜、毒性/波动率保护与统计验证。
- Router V2 净收益评分和完整退出生命周期。
- 历史 v1 账本离线重建工具（必须先取得完整历史 fills/fees）、端到端数据库故障注入和回放框架。

### 本轮纯离线验收（2026-07-19）

- `ruff check . --exclude venv`：通过。
- `python -m unittest discover -s tests -v`：104 项通过（含远端并发变更合并后的 fail-closed Router 盘口过滤测试）。
- `python -m compileall -q app dashboard tests alembic`：通过。
- Alembic 静态 SQL 从 `001` 连续渲染至唯一 head `009`：通过。
- `docker compose config --quiet` 与 `git diff --check`：通过。
- 未启动 API、Dashboard、Postgres、Redis 或容器，未连接 Polymarket，未提交/撤销任何真实订单。
