# Delta Spec: event-commit

## Purpose

定义事件提交确认与通知分离的行为契约：必要提交必须获得存储确认后才能对外宣称 committed，必要状态消费者的失败不被吞掉，观察者背压可观测，存储不可用时 会话进入显式隔离——堵死「落库失败仍通知成功」（H1）。

## ADDED Requirements

### Requirement: required 提交确认

`event_commit_policy="required"`（默认）下，非瞬态事件 MUST 在已注册 Store 确认写入（append_batch 成功、取得 receipt）之后才对外发布 committed 通知；写入失败时 emit SHALL 抛出 `PersistenceUnavailableError` 且不 fanout，不得静默继续。`best_effort` 仅用于显式接受丢事件的观测用途，启用时 SHALL 启动告警并禁用可靠恢复承诺。自定义 EventBus 不支持提交门时，required 模式下 Runtime 构造 SHALL 失败并给出适配说明，不得静默退化为观察者路径。

瞬态事件（token/progress）继续走实时通知、不逐条确认，且 MUST NOT 被宿主当作持久成功事实。

#### Scenario: 存储失败不再伪装成功

- **WHEN** required 模式下 Store 写入失败
- **THEN** emit 抛 PersistenceUnavailableError，无 committed 通知发出，会话进入隔离并停止推进新副作用

#### Scenario: 不支持的 bus 显式失败

- **WHEN** 传入不支持 attach_commit_gate 的自定义 EventBus 且 policy=required
- **THEN** Runtime 构造失败并附适配说明，而非静默走观察者路径

### Requirement: provisional 批次原子提交

未提交窗口的 commit SHALL 走封闭批次：整个窗口缓冲作为一次 `append_batch`（round 级 batch_id）提交——要么全部提交、要么全部未提交；提交失败不 pop 缓冲、不重复执行业务，同 batch_id 重试成功后恰好提交一次。窗口内事件 SHALL 先到达必要消费者（明确视为推测态），COMMITTING 期间该 round 的新发射等待批次结果且不持 Store 锁。嵌套派生事件（如消费侧同步再发射的 Agent 状态事件）SHALL 经执行上下文继承窗口归属，不得因缺 task_id 而逃逸到已提交流；窗口 discard 后，受影响 agent 的 live 聚合状态 SHALL 被重新聚合校正。

#### Scenario: commit 失败缓冲保留可重试

- **WHEN** provisional 批次提交时存储失败
- **THEN** 缓冲不丢、业务不重放、外部观察者未收到该批任何事件；同 batch_id 重试成功后恰好提交一次

#### Scenario: 派生事件不逃逸窗口

- **WHEN** 窗口内某事件的处理回调同步派生出无 task_id 的会话级事件，随后窗口被 discard
- **THEN** 派生事件随窗口一起消失，宿主可见流中不出现逃逸事件；受影响 agent 的聚合状态被重聚合校正

### Requirement: 必要消费者与观察者分离

必要进程内状态消费者（ALM/SessionRegistry 的状态推进）SHALL 以 `required` 类别订阅：不经可丢弃队列、其 handler 异常 SHALL 穿出 emit 使会话进入健康故障状态（不被 bus 吞掉）。其余观察者 SHALL 走独立有界队列：慢或失败不阻塞必要状态推进与提交；队列溢出丢弃 MUST 可观测——发布 `EventsDropped` 元事件（transient、不落库），payload 携带被丢事件的 position 区间与订阅者标识，客户端可按 `read_range` 补读持久事件。观察通知 SHALL 按 committed position 有序分发，嵌套派生的子事实不得先于父事实对观察者可见。

#### Scenario: required consumer 异常进入隔离

- **WHEN** required 消费者处理某事件时抛错
- **THEN** 异常穿出 emit，会话进入健康故障状态并停止推进；观察者收不到该事件的 committed 通知

#### Scenario: 慢观察者丢弃可观测

- **WHEN** 某观察者不消费、队列打满
- **THEN** 提交与必要状态推进不受阻塞；该观察者收到 EventsDropped 元事件（含 position 区间），可按 read_range 补读，其余观察者不受影响

### Requirement: 存储不可用隔离

持久化故障 SHALL 以 `PersistenceUnavailableError` 先于通用重试语义处理：会话标记 `storage_unavailable` 且可经公开接口查询原因；停止调度新 task/LLM/tool；不得把存储错误当作可重试 task 失败而反复发射 TaskFinished/TaskRequeued/RunFinished；`wait_for_finish` SHALL 抛出存储不可用错误而非等待通用超时让宿主猜测；故障隔离后内存中的 task/agent 状态不得继续被当作可靠状态推进；恢复前 SHALL 用 batch_id 确认未知提交并处理推测性记录。其他会话不受影响。

#### Scenario: 隔离与恢复确认

- **WHEN** 会话 A 存储故障、会话 B 正常
- **THEN** A 进入隔离（停止推进、wait_for_finish 抛错）、B 继续正常运行；A 恢复时先以 batch_id 确认未知提交，不盲目重发新 ID
