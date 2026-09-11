# Proposal: reliability-wp6

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP6（恢复政策、未知结果处理与控制工具）**，§5.4/§5.5/§8-WP6。
> 前置：reliability-wp5 已归档（操作账本 + operation_id 三性质 + gateway 五步序 + completed 短路）。
> 总纲：reliability-remaining-defects 任务组 6；本 change 完成 = **H3 解决**，可靠性方案六项假设仅剩 H5/H6。

## Why

H3 的盲重跑路径仍在（无桩实测：真硬杀 + recover_agent 冷恢复，副作用 1→2）。WP5 建好了账本但**没切恢复行为**——reconcile 的完成判定仍是「task view 里全部 tool_call_id 集合」（reconcile.py:77-81），两宗罪都在：call_1 复用串扰（旧结果配新调用）、副作用完成但结果未写时盲重跑。账本停在 `started`（manual 默认）下 WP5 的 completed 短路也不触发。

恢复需要的是**判据**而不是更多执行：每个 dangling 该不该重跑、能不能重跑、重跑不了该问谁——这正是恢复策略表 + unknown 处置要给的。

## What Changes

- **`ToolCapability.recovery_policy`**：`retry_safe | idempotent | queryable | manual`，**默认 `manual`**（不从 `side_effects` 推断安全——MCP 描述可能不完整）。声明 `queryable` 的 Provider MUST 实现 `QueryResult` 接口（查询外部真值），未实现则启动校验失败。
- **reconcile 匹配切换**：dangling 完成判定从 tool_call_id 集合 → **账本 operation_id**（`completed` 即复用不重执行；配对写 `memory_result_id` 双通道校验）。策略表消费：completed→复用；prepared-未-started→首次执行；started+retry_safe→同 op_id 重试；started+idempotent→幂等键重试；started+queryable→先查（definitely_not_started 才重跑）；started+manual 或结果不定→**unknown**；waiting_human→既有 HITL 恢复。**存量无账本身份的 dangling 默认 unknown**（不用随机 id 自动执行副作用工具——方案明令）。
- **unknown 处置**：task 置 INTERRUPTED + `TaskErrorCode.TOOL_OUTCOME_UNKNOWN` + 发布 `OperationUncertain` 事件（payload：operation_id/工具名/revision/可用动作/脱敏摘要）；unknown 下普通 `recover_agent` MUST NOT 绕过决策重跑工具（闸门）。
- **宿主处置接口 `runtime.resolve_operation(operation_id, decision, expected_revision)`**：`supply_result`（宿主已核实外部结果 → 账本 CAS completed + 按确定性 id 补写 memory）/ `retry_confirmed`（宿主显式承担重复风险 → 原 op_id 重排）/ `cancel_task`（task 终态；不声称撤销已发生的外部动作）。revision 不匹配拒绝（双宿主并发只有一个成功）。
- **控制工具核验**（测试钉子而非新机制）：delegate 的 op completed 后确认丢失 → 重入找回原 child 不生成第二棵子树；finish/metadata 同身份幂等；ask_user 复用已有请求。
- **翻转面（D7 式成对）**：reconcile 测试组按 policy 参数化（retry_safe→重跑保持 / manual→unknown 停住）；`test_tool_outcome_unknown` 夹具翻转（副作用 1 次 + INTERRUPTED + TOOL_OUTCOME_UNKNOWN）；无桩 h3 翻转（after_recovery_effects==1）；探针 H3 转 True。

## 不做什么

- 不对外部系统承诺 exactly-once（方案明令不据此声称——retry_safe/idempotent 的承诺来自 Provider 声明）。
- 不实现分布式锁/leader election（单 owner 架构已是 WP3 之后的现状）。
- 不动 WP5 的 gateway 五步序（reconcile 决策在前，gateway 短路仍是第二道防线）。
- 不做 PostgreSQL 真库验证（WP8）。

## Capabilities

### New Capabilities

（无——`tool-operations` capability 已在 wp5 立卷。）

### Modified Capabilities

- `tool-operations`: 后两条 requirement（「恢复策略表」〔wp6 交付〕、「结果未知的宿主处置」〔wp6 交付〕）从标注待交付转为交付——内容已在主 spec，本 delta 补充细节场景（存量无身份 dangling→unknown、闸门语义、控制工具核验三枚）。

## Impact

- **代码**：`protocols/capability.py`（ToolCapability +recovery_policy）、`protocols/operations.py`（QueryResult 接口）、`core/loop/steps/reconcile.py`（匹配切换 + 策略表消费 + unknown 上抛）、`core/models/discriminators.py`（+TOOL_OUTCOME_UNKNOWN）、`protocols/events.py`（+OPERATION_UNCERTAIN）、`core/runtime.py`（resolve_operation 公开方法 + 闸门）。
- **测试**：reconcile 测试组 policy 参数化翻转；`test_tool_outcome_unknown` 翻转；新建 `test_operation_recovery_policy.py` / `test_operation_crash_matrix.py`（策略表全分支 + 子进程强退矩阵）；无桩 h3 + 探针 H3 翻转。
- **破坏性**：默认 manual 使「无策略声明的副作用工具崩溃后自动重跑」终结——这正是 H3 修复的本体；宿主需为可安全重跑的工具显式声明 retry_safe（升级说明随 README）。
