# Delta Spec: tool-operations

## Purpose

定义工具执行的稳定身份、操作账本与恢复策略契约：跨重启的同一逻辑调用拥有稳定 operation_id，副作用是否已发生可判定，结果未知的操作交宿主显式处置——堵死「恢复盲重跑副作用」（H3）。

## ADDED Requirements

### Requirement: 稳定逻辑调用身份

每个工具调用 SHALL 拥有跨重启稳定的 `operation_id`，由 `(tenant_id, session_id, agent_id, assistant_record_id, tool_ordinal)` 确定性生成；assistant_record_id MUST 在执行前持久化。`invocation_id` 保留为单次执行尝试身份（取消用）；`tool_call_id` 仅为 LLM wire 配对字段。普通执行、热 HITL resume、冷恢复 SHALL 引用同一 operation_id；两次内容相同的合法调用 MUST 得到不同 operation_id（不误去重），模型复用 call_1 也不得让旧结果覆盖新调用。

#### Scenario: 同一调用跨重启身份不变

- **WHEN** 一次工具调用在崩溃后经冷恢复重入
- **THEN** 恢复路径读到与首次执行相同的 operation_id

#### Scenario: 两次合法同参调用不去重

- **WHEN** 两条 assistant 消息都用 call_1 且参数相同
- **THEN** 两个不同 operation_id，各自完整执行

### Requirement: 操作账本状态机

`OperationStore` SHALL 提供 prepare / compare-and-set / get，记录状态 `prepared → started → completed`（含 `waiting_human` / `unknown`），保存授权后参数指纹、完整规范化结果（非审计截断文本）、attempt IDs、恢复策略与 memory result ID 引用。执行顺序 SHALL 为：持久身份 → 授权校验 → prepared → CAS started（持久确认）→ 调 Provider → 保存 outcome completed（持久确认）→ 幂等写 TOOL_RESULT → 发 CapabilityFinished。账本 completed 而 memory 写失败时，恢复 SHALL 从账本重建 memory、不再次执行工具。

#### Scenario: completed 后 memory 写前崩溃

- **WHEN** 工具外部成功且账本已 completed，进程在 TOOL_RESULT 写入前退出
- **THEN** 恢复自账本补结果，外部副作用仍恰好一次

### Requirement: 恢复策略表

`ToolCapability` SHALL 声明显式 `recovery_policy`（`retry_safe | idempotent | queryable | manual`），默认 `manual`（不从 side_effects 推断安全）。各状态恢复动作按方案 §5.4 表执行：completed 复用结果；prepared 未 started 可首执；started+各策略按 Provider 承诺（幂等键/权威查询）；manual 与结果未知一律不自动执行。声明 queryable 的 Provider MUST 实现 QueryResult，未实现则启动校验失败。控制工具单独核验（delegate 用 operation_id 找回已建子任务、finish/metadata 同身份幂等、ask_user 复用请求），不得全局标 retry_safe。

#### Scenario: manual 副作用不重跑

- **WHEN** started 后崩溃且策略为 manual
- **THEN** 操作置 unknown 等宿主处置，恢复不得自动重执行

### Requirement: 结果未知的宿主处置

未知操作 SHALL 将 task 置 INTERRUPTED + `TOOL_OUTCOME_UNKNOWN` 错误码并发布带 operation_id/工具名/可用动作的事件；宿主经 `resolve_operation(operation_id, decision, expected_revision)` 处置：`supply_result`（宿主核实外部结果后代入）/ `retry_confirmed`（宿主显式承担重复风险，原 operation_id 不变）/ `cancel_task`。revision 不匹配 SHALL 拒绝（双宿主并发只有一个成功）；unknown 状态下普通 recover_agent MUST NOT 绕过决策重跑工具。

#### Scenario: 两个宿主并发处置

- **WHEN** 两个请求以不同 decision 并发 resolve 同一 operation
- **THEN** 恰好一个成功，另一个因 revision 不匹配被拒绝
