# Delta Spec: tool-operations

> wp5 已交付前两条 requirement（稳定身份 / 账本状态机）；本 delta 把后两条从〔wp6 交付〕
> 标注转为交付，并补充三枚细节场景（存量无身份 dangling、闸门、控制工具核验）。
> 同步动作：主 spec 的〔wp6 交付〕标注移除。

## MODIFIED Requirements

### Requirement: 恢复策略表

`ToolCapability` SHALL 声明显式 `recovery_policy`（`retry_safe | idempotent | queryable | manual`），默认 `manual`（不从 side_effects 推断安全）。恢复时按状态×策略分派：`completed` 复用结果不重执行；`prepared` 且从未进入 `started` 可首次执行（仍先重新检查授权）；`started + retry_safe` 以同 operation_id 重试；`started + idempotent` 以相同 operation_id 作幂等键重试；`started + queryable` 先查询，仅 `definitely_not_started` 为权威结论时重跑；`started + manual` 与一切结果不定情形一律置 `unknown` 不自动执行；`waiting_human` 经既有 HumanResumable 协议恢复，不从头重新 invoke。声明 `queryable` 的 Provider MUST 实现 `QueryResult`，未实现则启动校验失败。Reconcile 的完成匹配 SHALL 以账本 operation_id 为判据，MUST NOT 再以 tool_call_id 集合判定（防 call_1 复用串扰）。无账本身份的存量 dangling SHALL 默认置 `unknown`，不得以随机生成的 id 自动执行副作用工具。控制工具单独核验：delegate 以 operation_id 找回已创建子任务（确认丢失重入不生成第二棵子树）、finish/metadata 同身份幂等、ask_user 复用已有请求；MUST NOT 对控制工具整体标记 retry_safe 后省略验证。

#### Scenario: manual 副作用不重跑

- **WHEN** started 后崩溃且策略为 manual
- **THEN** 操作置 unknown 等宿主处置，恢复不得自动重执行

#### Scenario: retry_safe 重跑恰好一次补全

- **WHEN** started 后崩溃且策略为 retry_safe，恢复重入
- **THEN** 以同 operation_id 重试一次；外部副作用由 Provider 承诺幂等；账本与 memory 各落一次

#### Scenario: queryable 的权威否定

- **WHEN** started 后崩溃且策略为 queryable，查询返回 definitely_not_started
- **THEN** 以同 operation_id 重新执行；查询返回 completed 则直接复用外部结果不再执行

#### Scenario: 存量无身份 dangling 保守停住

- **WHEN** 恢复遇到 WP5 之前持久化的 dangling（无账本记录可查）
- **THEN** 默认置 unknown 并要求宿主处置，不自动执行

### Requirement: 结果未知的宿主处置

未知操作 SHALL 将 task 置 INTERRUPTED + `TOOL_OUTCOME_UNKNOWN` 错误码并发布 `OperationUncertain` 事件（payload 至少含 operation_id、工具名、revision、可用处置动作、脱敏摘要）；宿主经 `resolve_operation(operation_id, decision, expected_revision)` 处置：`supply_result`（宿主已核实外部结果，按 Provider 结果结构归一化后入账本并按确定性 id 补写 memory）/ `retry_confirmed`（宿主显式承担重复执行风险，持久记录决定后以原 operation_id 重排）/ `cancel_task`（停止该任务；不声称撤销已发生的外部动作）。revision 不匹配 SHALL 拒绝（双宿主并发处置恰好一个成功）；unknown 状态下普通 `recover_agent` MUST NOT 绕过决策重跑工具（闸门）。

#### Scenario: 两个宿主并发处置

- **WHEN** 两个请求以不同 decision 并发 resolve 同一 operation
- **THEN** 恰好一个成功，另一个因 revision 不匹配被拒绝

#### Scenario: supply_result 补写 memory 不重执行

- **WHEN** 宿主以 supply_result 提供核实过的外部结果
- **THEN** 账本 CAS completed、按 operation_id 确定性派生的 memory id 幂等补写 TOOL_RESULT；provider 不被调用

#### Scenario: unknown 闸门挡住普通恢复

- **WHEN** task 处于 unknown 中断且宿主未 resolve，调用 recover_agent
- **THEN** 恢复不重跑该工具，保持等待宿主处置
