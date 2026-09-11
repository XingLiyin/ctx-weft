# Proposal: reliability-wp7

> 上游方案：`docs/plans/2026-09-11-agent-core-reliability-plan.md` **WP7（执行限制与无效配置迁移）**，§6/§8-WP7。
> 前置：wp3–wp6 已归档（INTERRUPTED 通道、操作账本 unknown 处置就绪——超时取消与 unknown 的接缝依赖后者）。
> 总纲：reliability-remaining-defects 任务组 7；完成后六项假设仅剩 H6（wp9，可选）。

## Why

H5：三个公开限制字段零执行点（静态核实 + 无桩对照：`max_turns_per_act`/`context_limit` 真被消费，`max_turns_per_agent=20`、`timeout_per_step_sec=120`、`Task.timeout_ms=60_000` 只被定义/透传/投影）。宿主按字段名设了限制以为生效——实际什么都不会发生。直接激活旧默认值不可行：会改变大量已有任务的结束时机（且 agent 生命周期横跨多轮用户请求，「20 轮」语义不清）。

## What Changes

- **`ExecutionLimits`**（新 `core/control/execution_budget.py`，经 RuntimeConfig 注入）：`step_active_timeout_sec` / `task_active_timeout_sec` / `provider_timeout_sec` / `max_actor_turns_per_task`（默认全 None = 不新增限制）+ `cleanup_grace_sec=5.0`。
- **计量语义固定**（方案 §6 原文）：task active time = 装配到本次 run 停止的实际墙钟，跨自动 retry 累计，**排队/等子任务/等 HITL 不计**；actor turns = 一次逻辑 LLM 请求 1 轮（网络自愈不重复计），跨 retry 累计、人工后续跑同 task 不清零；deadline 用 monotonic clock；**持久化已消费量**（崩溃恢复续用剩余预算）；崩溃漏记不超过声明 checkpoint 周期（≤1s）——**如实文档，不假装严格计费上限**。
- **超限结局**：task INTERRUPTED + 专用错误码（`TASK_DEADLINE_EXCEEDED` / `STEP_DEADLINE_EXCEEDED` / `PROVIDER_DEADLINE_EXCEEDED` / `ACTOR_TURN_LIMIT`，入 TaskErrorCode）；不混淆 USER_CANCEL；不无限重试。超时取消时工具已 started 且结果未知 → **先走 wp6 的 unknown 规则**（timeout 不证明副作用未发生）。
- **不合作 provider**：协作取消后最多等 `cleanup_grace_sec`；仍不合作的标记未终止并**拒绝同会话继续副作用**（不假装 asyncio 能强杀 Python 代码；硬隔离归宿主的进程/沙箱）。
- **旧字段弃用**：`max_turns_per_agent` / `timeout_per_step_sec` / `Task.timeout_ms` 在模板解析与 Runtime 校验发**去重 DeprecationWarning**；不激活、不自动映射（语义不同）；删除留给下个有迁移说明的破坏性版本；README 写明「旧字段不提供限制保障」。

## 不做什么

- 不激活任何旧默认值；`max_turns_per_agent` 不映射 task 轮数。
- 不实现 OS 级沙箱/进程强杀（宿主职责）。
- 不改既有有效限制（act 轮数 / observe 轮数 / context_limit / LLM 自愈预算）。
- 不做 PostgreSQL 真库验证（WP8）。

## Capabilities

### New Capabilities

- `execution-limits`: opt-in 执行限制契约——语义（active time 排除等待 / turns 计量 / monotonic + 持久化已消费量）、超限错误码四态、旧字段弃用不激活（承自总纲已评审稿）。

### Modified Capabilities

（无。）

## Impact

- **代码**：新 `core/control/execution_budget.py`；`RuntimeConfig` + 字段；StepDriver 步边界检查点；TaskManager 派发前预占 actor 轮数；LLM/Gateway provider 等待边界（`provider_timeout_sec`）；模板 loader 与 Runtime 校验的弃用警告。
- **测试**：`test_execution_budget.py`（可注入 monotonic clock 验计量算术）+ `test_execution_limits.py`（barrier 驱动集成：L-T01 合作超时 / L-T02 等 HITL 不计 / L-T03 retry 不重置 / L-T04 重启恢复 / L-T05 不合作 provider / L-T06 旧字段警告不激活）。
- **风险**：默认 None 保证零行为变化（全量回归门禁）；检查点开销可忽略（纯内存比较）。
