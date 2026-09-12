# Delta Spec: execution-limits

## Purpose

定义可选执行限制的语义契约：opt-in 的 deadline/轮数限制可解释、可测试、错误码明确；历史公开却从未执行的旧字段明确弃用且不被静默激活。

## ADDED Requirements

### Requirement: opt-in ExecutionLimits

RuntimeConfig SHALL 支持注入独立的 `ExecutionLimits`（step_active_timeout_sec / task_active_timeout_sec / provider_timeout_sec / max_actor_turns_per_task / cleanup_grace_sec），默认全 None = 不新增限制。计量语义固定：task/step active time 为实际活动墙钟（排队、等子任务、等 HITL 不计）；provider timeout 限制单次调用活动时间、progress 不续命；actor turns 按逻辑 LLM 请求计数、跨 task retry 累计、网络自愈不重复计；deadline 用 monotonic clock，持久化的是已消费量而非进程内绝对值。超限 SHALL 进入 INTERRUPTED 并携带专用错误码（TASK_DEADLINE_EXCEEDED / STEP_DEADLINE_EXCEEDED / PROVIDER_DEADLINE_EXCEEDED / ACTOR_TURN_LIMIT），不以 USER_CANCEL 混淆、不无限重试。工具被超时取消时若已 started 且结果未知，先走 tool-operations 的 unknown 规则。

#### Scenario: opt-in deadline 生效

- **WHEN** 配置 task_active_timeout_sec 且任务活动超时
- **THEN** 任务 INTERRUPTED + TASK_DEADLINE_EXCEEDED；未配置时行为与现状完全一致

#### Scenario: 等待人不计量

- **WHEN** 任务长时间等待 HITL 人工答复后恢复
- **THEN** 等待时长不计入 active time，恢复后继续使用剩余预算

### Requirement: 旧无效配置弃用

`max_turns_per_agent` / `Task.timeout_ms` / `timeout_per_step_sec`（验证附带发现）SHALL 在模板解析/Runtime 校验中发出去重的 DeprecationWarning，README 明确「旧字段不提供限制保障，迁移到 ExecutionLimits」；SHALL NOT 激活其历史默认值（20 轮 / 60 秒 / 步超时），`max_turns_per_agent` 不自动映射到 task 轮数（语义不同）。删除留待有明确迁移说明的破坏性版本。

#### Scenario: 旧字段警告不激活

- **WHEN** 模板声明 max_turns_per_agent=20 或 Task.timeout_ms=60000
- **THEN** 各发一次 DeprecationWarning；任务行为与未声明时一致（不被 20 轮/60 秒截断）
