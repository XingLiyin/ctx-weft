# Delta Spec: event-commit

## Purpose

定义事件提交确认与通知分离的行为契约：必要提交必须获得存储确认后才能对外宣称 committed，观察者故障不阻塞也不伪装成功，存储不可用时 会话进入显式隔离——堵死「落库失败仍通知成功」（H1）。

## ADDED Requirements

### Requirement: required 提交确认

`event_commit_policy="required"`（默认）下，非瞬态事件 MUST 在已注册 Store 确认写入（append_batch 成功、拿到 receipt）之后才对外发布 committed 通知；写入失败时调用方 SHALL 收到错误而非静默继续。`best_effort` 仅用于显式接受丢事件的观测用途，启用时 SHALL 启动告警并禁用可靠恢复承诺。自定义 EventBus 不支持提交门时，required 模式构造 SHALL 失败并给出适配说明，不得静默退化。

#### Scenario: 存储失败不再伪装成功

- **WHEN** required 模式下 Store 写入失败
- **THEN** 提交方收到错误，无 committed 通知发出，会话停止推进新副作用

#### Scenario: 不支持的 bus 显式失败

- **WHEN** 传入不支持提交门的自定义 EventBus 且 policy=required
- **THEN** Runtime 构造失败并附适配说明，而非静默走观察者路径

### Requirement: 必要消费者与观察者分离

必要进程内状态消费者（ALM/SessionRegistry 的状态推进）SHALL 不经可丢弃队列、异常向上报告；外部观察者 SHALL 走独立有界队列，慢/失败不阻塞必要状态推进。观察者队列溢出丢弃 MUST 可观测（补齐注释宣称的 `EventsDropped` 或等价机制：事件携带缺口信息，客户端可按位置补读持久事件）。

#### Scenario: required consumer 异常进入隔离

- **WHEN** 必要消费者处理事件抛错
- **THEN** 会话进入健康故障状态，异常不被吞掉继续执行

#### Scenario: 慢观察者不拖垮主循环

- **WHEN** 某观察者不消费、队列打满
- **THEN** 主循环不挂死；丢弃被显式观测到（元事件/指标），其余观察者不受影响

### Requirement: provisional 批次原子提交

未提交窗口的 commit SHALL 走封闭批次（WP2 的 append_batch）：要么全部提交要么全部未提交；失败不 pop 缓冲、不重复执行业务；COMMITTING 期间该 round 的新发射等待批次结果且不持 Store 锁；嵌套派生事件经执行上下文继承 round 归属，撤销时宿主可见流无逃逸事件。

#### Scenario: commit 失败缓冲保留

- **WHEN** provisional 批次提交时存储失败
- **THEN** 缓冲不丢、业务不重放；同 batch_id 重试成功后恰好提交一次

### Requirement: 存储不可用隔离

持久化故障 SHALL 以 `PersistenceUnavailableError` 先于通用重试处理：会话标记 `storage_unavailable` 并可查询；停止调度新 task/LLM/tool；不把存储错误当可重试 task 失败反复发 TaskFinished/RunFinished；`wait_for_finish` SHALL 明确抛存储不可用错误而非等 300 秒超时；恢复前用 batch_id 确认未知提交。

#### Scenario: 隔离与恢复

- **WHEN** 会话 A 存储故障、会话 B 正常
- **THEN** A 进入隔离并停止推进、`wait_for_finish` 抛 PersistenceUnavailableError；B 不受影响继续运行
