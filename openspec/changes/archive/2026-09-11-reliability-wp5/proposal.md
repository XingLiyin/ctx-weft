# Proposal: reliability-wp5

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP5（operation identity、账本与参数保存）**，§5.2/§5.3/§8-WP5。
> 前置：WP1 已修参数通道（wp0-wp1 归档）；WP2–WP4 已交付 position/提交门/快照切面。
> 总纲：reliability-remaining-defects 任务组 5；本 change 完成 ≠ H3 解决（WP6 才切恢复行为）。

## Why

H3（恢复盲重跑副作用）的根因之一是身份缺失：`tool_call_id` 是 LLM wire 字段（模型复用 call_1 是常态）、`invocation_id` 是单次执行尝试身份（每次新生成）——「同一逻辑调用跨重启」今天无法指认。执行路径无记账：provider 调用前后只有审计视角（TOOL_AUDIT/事件），「副作用是否已发生、结果是什么」无处可查——WP6 的恢复策略表（retry_safe/idempotent/queryable/manual）没有输入可依据。无桩验证实测：真硬杀 + recover_agent 冷恢复，外部副作用 1→2。

## What Changes

- **稳定逻辑调用身份 `operation_id`**：由 `(tenant_id, session_id, agent_id, assistant_record_id, tool_ordinal)` 确定性派生——跨重启稳定（同一逻辑调用同 id）、同参两次合法调用不同 id（不误去重）、call_1 复用不串扰。`invocation_id` 保留为单次执行尝试身份（cancel 用）；`tool_call_id` 仅为 LLM wire 配对字段。
- **`_ingest_assistant_turn` 接口升级**：返回结构化 `PersistedAssistantTurn(record_id, tool_calls)`——`assistant_record_id` 即 memory 记录 id（ingest 已产出，只是没往外返）。act 的调用点与 reconcile 复用据此携带 record_id + ordinal。
- **`ProviderContext.operation_id: str | None`**：新字段；`invocation_id` 与 `extra['tool_call_id']` 兼容保留。
- **`OperationStore` 协议 + 双实现**（`protocols/operations.py` + `providers/operations/{in_memory,sql}.py`）：`get / prepare / compare_and_set`；状态机 `prepared → started → completed`（+ `waiting_human` / `unknown`）。OperationRecord 含：身份五元组、授权后参数指纹（复用 WP1 的 effective 通道）、恢复策略字段（本包只存不消费——WP6 读）、attempt IDs、完整规范化结果或 blob 引用（非审计截断文本）、error、memory result ID 引用。
- **执行顺序接入 gateway 单一咽喉点**（方案 §5.3）：持久身份 → 授权校验 → prepared → CAS started（持久确认）→ provider → 保存 outcome completed（持久确认）→ 幂等写 TOOL_RESULT → CapabilityFinished。silent/dispatch 控制工具同样入账（方案明令）。memory result id 由 operation_id 确定性生成——completed 后 memory 写失败的窗口可由账本修复（恢复重建 memory，不重执行）。
- **账本可用性与提交门同级**：OperationStore 写失败 → 按存储不可用处理（复用 WP3 隔离链路）——账本是 H3 恢复的依据，静默降级会重新制造「伪装成功」。
- **runtime 接线**：默认注册内存 OperationStore；宿主声明跨进程恢复时注入 SQL 实现（未注入只恢复会话与对话，dangling 默认 unknown 的完整语义归 WP6）。

## 不做什么

- **不切恢复行为**：ReconcileStep 完成匹配仍按旧逻辑（task view 的 tool_call_id 集合）；无桩 h3 判定**必须保持复现**（1→2）——这是 WP5 的诚实门禁。
- 不实现恢复策略表消费、unknown 处置、`resolve_operation` 宿主接口（WP6）。
- 不承诺对外部系统 exactly-once（账本与工具副作用不组成分布式事务——方案明令不据此声称）。
- 不动 HITL resume_state 存储（账本记录引用，不改 HitlService）。

## Capabilities

### New Capabilities

- `tool-operations`（部分）：本 change 交付其前两条 requirement——「稳定逻辑调用身份」与「操作账本状态机」；后两条（恢复策略表、结果未知处置）属 WP6，spec 文件按完整 capability 立卷、requirement 级标注交付边界。

### Modified Capabilities

（无——`capability-gateway`/`event-*` 契约不变；账本注入点是实现接线。）

## Impact

- **代码**：`protocols/operations.py`（新）、`providers/operations/`（新，内存+SQL）、`protocols/context.py`（+operation_id）、`core/loop/steps/act.py`（ingest 返回结构化 + 调用点铸 id）、`core/loop/steps/reconcile.py`（携带身份）、`core/loop/capability_gateway.py`（账本五步串接 + blob 引用）、`core/runtime.py`（默认注册 + 注入口）。
- **测试**：新建 `tests/unit/test_operation_identity.py`（身份三性质：跨重启稳定/同参不去重/call_1 复用不串扰）+ `test_operation_store_conformance.py`（参数化内存/SQL：CAS 语义、状态机转移、幂等 get/prepare）；既有 gateway/act 测试全量回归（接线零行为变化）。
- **风险**：act→gateway 接口面改动（3 调用点）——结构化返回向后兼容（旧返回 list[dict] 的消费点逐一升级）；每工具调用 +2 次账本持久确认（SQL 下）——预算实测随验收记录。
