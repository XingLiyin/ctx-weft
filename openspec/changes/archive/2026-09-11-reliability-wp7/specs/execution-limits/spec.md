# Delta Spec: execution-limits

## Purpose

定义可选执行限制的行为契约：opt-in 的 deadline/轮数限制语义可解释、可测试、错误码明确，历史公开却从未执行的旧字段明确弃用且不被静默激活——让「配置了限制」重新等于「限制会生效」。

## ADDED Requirements

### Requirement: opt-in ExecutionLimits

RuntimeConfig SHALL 支持注入 `ExecutionLimits`（step_active_timeout_sec / task_active_timeout_sec / provider_timeout_sec / max_actor_turns_per_task / cleanup_grace_sec），默认全 None = 不新增任何限制（现有有效限制——act 轮数 / observe 轮数 / context_limit / LLM 自愈预算——独立保留不受影响）。

计量语义 SHALL 固定为：task active time 为从装配到本次 run 停止的实际墙钟、跨自动 retry 累计，排队、等待子任务、等待 HITL 人工答复 MUST NOT 计入；actor turns 按逻辑 LLM 请求计数（同请求的网络自愈 MUST NOT 重复计）、跨 task retry 累计、人工处理后继续同 task MUST NOT 自动清零；deadline 使用 monotonic clock，持久化的是已消费时长与轮数（崩溃恢复后继续剩余预算，不从零重计）；预算检查在阶段转换及最长每 1 秒 checkpoint——崩溃漏记 MUST NOT 超过声明的 checkpoint 周期，文档 MUST NOT 将其称为严格计费上限。

#### Scenario: opt-in deadline 生效且默认无感

- **WHEN** 配置 task_active_timeout_sec 且任务活动超时 / 未配置任何限制
- **THEN** 超时任务进入 INTERRUPTED + TASK_DEADLINE_EXCEEDED；未配置时行为与现状逐字节一致

#### Scenario: 等待人不计量、恢复续预算

- **WHEN** 任务长时间等待 HITL 后恢复，以及进程重启后恢复任务
- **THEN** 等待时长不计入 active time、恢复后继续使用剩余预算；重启后已消费计量从持久化值恢复，漏记不超过声明的 checkpoint 周期

#### Scenario: actor 轮数跨 retry 累计

- **WHEN** 任务自动重试多轮
- **THEN** active time 与 actor turns 均不因 retry 重置；轮数按逻辑 LLM 请求计（网络自愈不重复计）

### Requirement: 超限结局与超时取消的 unknown 接缝

超限 SHALL 使 task 进入 INTERRUPTED 并携带专用错误码（TASK_DEADLINE_EXCEEDED / STEP_DEADLINE_EXCEEDED / PROVIDER_DEADLINE_EXCEEDED / ACTOR_TURN_LIMIT），MUST NOT 以 USER_CANCEL 混淆原因、MUST NOT 无限自动重试。工具被超时取消时若已 started 且结果未知，SHALL 先按 tool-operations 的 unknown 规则处置（timeout MUST NOT 被当作「副作用未发生」的证据）。协作取消后至多等待 cleanup_grace_sec；不能合作的进程内 Provider SHALL 被标记为未终止并阻止同会话继续副作用（MUST NOT 假装 asyncio 能终止不合作的 Python 代码）。

#### Scenario: 超时取消撞上已开始的副作用

- **WHEN** provider 调用超时被取消，但外部操作已 started 且结果不可判定
- **THEN** 操作进入 unknown 等 host resolve_operation，不以超时为由自动重跑

#### Scenario: 不合作的 provider 被隔离

- **WHEN** cleanup 宽限耗尽后 provider 仍在运行
- **THEN** 该 provider 标记未终止，同会话后续副作用调用被拒绝并说明原因

### Requirement: 旧无效配置弃用

`max_turns_per_agent` / `Task.timeout_ms` / `timeout_per_step_sec` SHALL 在模板解析与 Runtime 校验中发出去重的 DeprecationWarning；SHALL NOT 激活其历史默认值（20 轮 / 60 秒 / 120 秒）；`max_turns_per_agent` MUST NOT 自动映射到 task 轮数（语义不同）；README SHALL 明确「旧字段不提供限制保障，迁移到 ExecutionLimits」。删除留待有明确迁移说明的破坏性版本。

#### Scenario: 旧字段警告且不激活

- **WHEN** 模板声明 max_turns_per_agent=20 / timeout_per_step_sec=120，或 Task 携带 timeout_ms=60000
- **THEN** 各发一次（去重）DeprecationWarning；任务行为与未声明时一致——不被 20 轮 / 120 秒 / 60 秒截断
