# Delta Spec: tool-operations

> 本 change（reliability-wp5）交付前两条 requirement；「恢复策略表」与「结果未知的宿主处置」
> 两条已在总纲定稿、归 reliability-wp6 交付——capability 按完整形态立卷，边界在各 requirement
> 标注。

## Purpose

定义工具执行的稳定身份、操作账本与恢复策略契约：跨重启的同一逻辑调用拥有稳定 operation_id，副作用是否已发生可判定，结果未知的操作交宿主显式处置——堵死「恢复盲重跑副作用」（H3）。

## ADDED Requirements

### Requirement: 稳定逻辑调用身份 〔wp5 交付〕

每个工具调用 SHALL 拥有跨重启稳定的 `operation_id`，由 `(tenant_id, session_id, agent_id, assistant_record_id, tool_ordinal)` 确定性派生；`assistant_record_id` MUST 在执行前持久化（assistant 回合入 memory 的记录 id）。`invocation_id` 保留为单次执行尝试身份（取消用）；`tool_call_id` 仅为 LLM wire 配对字段，MUST NOT 用作恢复匹配依据。普通执行、热 HITL resume、冷恢复 SHALL 引用同一 operation_id；两条内容相同的合法调用 MUST 得到不同 operation_id（不误去重）；模型复用同一 tool_call_id MUST NOT 使旧结果覆盖新调用。silent/dispatch 类控制工具 SHALL 同样拥有账本身份（不入对话 ≠ 不入账本）。

#### Scenario: 同一调用跨重启身份不变

- **WHEN** 一次工具调用在崩溃后经冷恢复重入
- **THEN** 恢复路径读到与首次执行相同的 operation_id（同一 assistant_record_id + ordinal 派生）

#### Scenario: 两次合法同参调用不去重

- **WHEN** 两条 assistant 消息都用 call_1 且参数相同
- **THEN** 两个不同 operation_id（不同 record_id/ordinal），各自完整执行

#### Scenario: call_1 复用不串扰

- **WHEN** 后一轮 LLM 回合复用了前一轮的 tool_call_id
- **THEN** 两轮调用各自 operation_id 独立，前一轮的结果不配对到后一轮

### Requirement: 操作账本状态机 〔wp5 交付〕

`OperationStore` SHALL 提供 `get / prepare / compare_and_set`，按状态机 `prepared → started → completed` 记录操作（另含 `waiting_human` / `unknown`）；OperationRecord MUST 含身份五元组、授权后参数指纹、恢复策略字段、attempt IDs、完整规范化结果或可持久读取的结果引用（非审计截断文本）、error、memory result ID 引用。执行顺序 SHALL 为：持久身份 → 授权校验 → prepared → CAS started（持久确认）→ 调 Provider → 保存 outcome completed（持久确认）→ 幂等写入 TOOL_RESULT memory → 发 CapabilityFinished。账本 completed 而 memory 写入失败时，恢复 SHALL 从账本重建 memory、不再次执行工具。memory result id SHALL 由 operation_id 确定性生成。OperationStore 写失败 SHALL 按存储不可用处理（会话隔离），不得静默降级。宿主未注册持久 OperationStore 时 runtime SHALL 默认提供内存实现并在声明跨进程恢复能力时如实报告缺失。

#### Scenario: completed 后 memory 写前崩溃

- **WHEN** 工具外部成功且账本已 completed，进程在 TOOL_RESULT 写入前退出，随后恢复
- **THEN** 自账本补写 memory 结果（id 由 operation_id 派生），外部副作用仍恰好一次

#### Scenario: CAS 串行化并发推进

- **WHEN** 两个路径并发对同一 operation 推进状态（prepare/started/completed）
- **THEN** revision 不匹配的一方被拒绝，状态机不出现倒退或跳跃

### Requirement: 恢复策略表 〔wp6 交付，本 change 仅落库恢复策略字段〕

`ToolCapability` SHALL 声明显式 `recovery_policy`（`retry_safe | idempotent | queryable | manual`），默认 `manual`（不从 side_effects 推断安全）。各状态恢复动作按方案 §5.4 表执行：completed 复用结果；prepared 未 started 可首执；started + 各策略按 Provider 承诺（幂等键/权威查询）；manual 与结果未知一律不自动执行。声明 queryable 的 Provider MUST 实现 QueryResult，未实现则启动校验失败。控制工具单独核验（delegate 用 operation_id 找回已建子任务、finish/metadata 同身份幂等、ask_user 复用请求），不得全局标 retry_safe。

#### Scenario: manual 副作用不重跑

- **WHEN** started 后崩溃且策略为 manual
- **THEN** 操作置 unknown 等宿主处置，恢复不得自动重执行

### Requirement: 结果未知的宿主处置 〔wp6 交付〕

未知操作 SHALL 将 task 置 INTERRUPTED + `TOOL_OUTCOME_UNKNOWN` 错误码并发布带 operation_id/工具名/可用动作的事件；宿主经 `resolve_operation(operation_id, decision, expected_revision)` 处置：`supply_result` / `retry_confirmed` / `cancel_task`。revision 不匹配 SHALL 拒绝（双宿主并发只有一个成功）；unknown 状态下普通 recover_agent MUST NOT 绕过决策重跑工具。

#### Scenario: 两个宿主并发处置

- **WHEN** 两个请求以不同 decision 并发 resolve 同一 operation
- **THEN** 恰好一个成功，另一个因 revision 不匹配被拒绝
