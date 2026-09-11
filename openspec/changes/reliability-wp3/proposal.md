# Proposal: reliability-wp3

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP3（必要提交门、观察队列与故障控制）**，§4.5/§4.6/§4.7。
> 前置：reliability-wp2 已归档（OrderedEventStore：单事件批次 + batch_id 幂等 + position）。
> 总纲：reliability-remaining-defects 任务组 3；本 change 是其单元化实施。

## Why

H1（落库失败仍对外通知成功）已三层坐实（WP0 夹具、探针、无桩验证——真 SQLite DROP TABLE 下会话照常 FINISHED、死后仍通知 53 个事件、落库 0 行）。根因是 persister 作为普通观察者挂在 fanout 尾部：吞存储异常（persister.py:47-50）、bus 吞 handler 异常（bus.py:112-113）、队列丢弃只有内部计数器（宣称的 `EventsDropped` 元事件不存在）。**通知成功 ≠ 提交成功**这一谎言使崩溃恢复建立在「已通知未落库」的幻影日志上。

WP2 已铺好地基（append_batch/batch_id/position），本 change 在其上装**提交门**：先确认提交、再对外通知；必要状态消费者异常上抛；观察者慢/失败不阻塞但丢弃可观测；存储不可用让会话显式隔离而非继续推进。

## What Changes

- **CommitGate**（新 `core/events/commit_gate.py`，编排层）：emit 路径上、fanout 之前的提交确认点。窗口外事件走 WP2 单事件批次（batch_id=event.id）；**未提交窗口整批一次提交**（commit_provisional → append_batch(round_id)——真批次原子，失败缓冲保留可重试）。
- **EventBus 协议扩展**：可选 `attach_commit_gate(gate)`（默认无 = 不支持）；`subscribe(..., required=True)` 新订阅类别。required 模式下 runtime 构造期检查自定义 bus 是否支持提交门，不支持显式失败并附适配说明（不静默退化）。
- **`event_commit_policy="required"|"best_effort"`**（默认 required）：required = 必须拿到已注册 Store 的写入确认（≠内存后端的磁盘持久性）；best_effort 仅供显式接受丢事件的观测用途，启用即告警并禁用可靠恢复承诺。
- **required 消费者**（ALM/SessionRegistry 的 provisional 订阅显式升级）：推测态照看（窗口内事件立刻到达，pause/cancel 依赖），异常穿出 emit 不再被 bus 吞；窗口 discard 后对受影响 agent 重发当前聚合事实（修推测态漂移，不做全量对账）。
- **观察者通道**：独立有界队列 + 背压丢弃发 `EventsDropped` 元事件（transient、不落库；payload 带 position 区间，客户端可按 `read_range` 补读——WP2 的 position 在此兑现）。
- **派生事件两颗雷**：round_id 经 contextvar 传播（ALM 在 handler 里同步 emit 的 Agent* 派生事件常无 task_id，按 task_id 查窗口会漏出未提交窗口）；提交短临界区、派生事实在父事实之后独立提交、观察通知按 committed position 有序（子不先于父可见）。
- **`PersistenceUnavailableError` + 会话隔离**：先于通用重试处理；会话标记 `storage_unavailable` 可查询；停止调度新 task/LLM/tool；不把存储错误当可重试 task 失败反复发终态事件；`wait_for_finish` 显式抛错；恢复前用 batch_id 确认未知提交。
- **翻转与改写**：WP0 夹具 `test_runtime_storage_failure` 翻转为 required 语义；`test_persister_swallows_store_errors` 改写为 required/best_effort 双契约；无桩验证 h1 与探针 H1 判定翻转（缺陷不再复现）。

## 不做什么

- **不动快照与恢复读取**（WP4）：h2 判定保持复现；`read_after(id)` legacy 不动。
- **不宣称 H1 完全解决到「任意业务写入与事件日志同事务」**——EventStore 与 MemoryProvider 仍是两个存储（方案 §4.10 边界）。
- 不做全量 live 状态对账（discard 后只重聚合受影响 agent）。
- 不接 PostgreSQL 真库验证（WP8）。

## Capabilities

### New Capabilities

- `event-commit`: 事件提交确认与通知分离契约——required 提交、必要消费者异常上报、观察者背压与丢弃可观测、provisional 批次原子、存储不可用隔离（4 条 requirement，承自总纲已评审稿）。

### Modified Capabilities

（无——`event-log` 的存储契约不变，本 change 只在其上新增提交编排行为。）

## Impact

- **代码**：`protocols/events.py`（bus 协议扩展 + `EventsDropped` + `PersistenceUnavailableError`）、`providers/events/bus/in_process/bus.py`（gate hook / required 类别 / 丢弃元事件 / commit_provisional 整批）、新 `core/events/commit_gate.py`、`core/runtime.py` 构造期接线与策略、`orchestrator/lifecycle/{agent_manager,session_registry}.py`（required 升级 + discard 重聚合）、TaskManager/_run_loop 异常链前置分支、`wait_for_finish`。
- **测试**：新建 test_commit_gate / test_observer_backpressure；翻转/改写三处旧契约（见上）；无桩与探针判定翻转。
- **破坏性**：required 成为默认——自定义 EventBus 不支持提交门的宿主构造期失败（原可静默运行）；bus 吞 handler 异常的旧行为对 required 订阅者终结。
- **性能**：窗口外每次 emit 多一次同步存储确认（单事件批）；round 批次从 N 次往返降为 1 次。≤15% p95 预算（方案 §10.1）在验收矩阵核对。
