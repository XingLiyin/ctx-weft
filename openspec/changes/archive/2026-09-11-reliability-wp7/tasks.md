# Tasks: reliability-wp7

> 完成后六项假设仅剩 H6（wp9 可选）。硬门禁：默认 None 下全量回归零变化。

## 1. 类型与预算模块

- [x] 1.1 新建 `core/control/execution_budget.py`：`ExecutionLimits`（五字段默认 None/5.0）+ `ExecutionBudget`（park/resume/check/consume_turn/预占；monotonic 可注入）+ `ExecutionLimitExceeded`（retriable=False，携四错误码之一）；`TaskErrorCode` 增四码；`RuntimeConfig.execution_limits` 字段
- [x] 1.2 新建 `tests/unit/test_execution_budget.py`（fake monotonic）：park 不计 / 跨 retry 累计 / 重启恢复（persisted_consumed 续算）/ 漏记 ≤ checkpoint 周期 / check 返回首超限项 / turns 预占与逻辑计次（自愈不重计）

## 2. 接线与检查点

- [x] 2.1 runtime 构造：limits 注入 LoopContext；TaskManager 派发建 per-task budget（恢复路径从 task 投影 `budget_consumed` 恢复）；StepDriver 步边界 + act 轮边界 checkpoint（check→`ExecutionLimitExceeded` 上抛，走既有 INTERRUPTED 链、`task.error_code` 置位）；consumed 写回 task 内存字段并随下一次 TASK_* 事件投影（reducers/converters 补 `budget_consumed` 透传）
- [x] 2.2 park/resume 挂点：HitlPark 分支与恢复、SUSPENDED 等子任务与重入——两处 `budget.park()/resume()`；单测（budget 集成桩）确认等待不计
- [x] 2.3 provider 超时：`_stream_tool` 包 `asyncio.timeout(provider_timeout_sec)`；超时→已 started 走 unknown（复用 reconcile 的 `_mark_unknown` 提为模块级 helper）、未 started 直接 PROVIDER_DEADLINE_EXCEEDED；cleanup_grace 后仍运行的 provider 入 `_uncooperative` 集合、同会话后续 invoke 拒绝

## 3. 旧字段弃用

- [x] 3.1 模板 loader 与 SessionRegistry 建任务处：三个旧字段非默认值时发去重 DeprecationWarning（模块级 `_warned`）；单测 `pytest.warns`（L-T06：警告 + 行为不变——不被 20 轮/120 秒/60 秒截断）

## 4. 集成测试与门禁

- [x] 4.1 新建 `tests/integration/test_execution_limits.py`（barrier 驱动）：L-T01 合作型超时（错误码正确、grace 内停）/ L-T02 长 HITL 等待不计（恢复续剩余）/ L-T03 自动 retry 计量不重置 / L-T04 进程重启（新 runtime 从投影恢复 consumed）/ L-T05 不合作 provider（标记 + 同会话拒绝）/ 超时取消撞 started 工具 → unknown 接缝
- [x] 4.2 全量回归：默认 None 零新增失败（基线 2 既知预存）；探针 `--expect fixed` 保持四 True；无桩四项保持

## 5. 收尾

- [x] 5.1 README「执行限制」小节（ExecutionLimits 用法 + 旧字段弃用说明 + 不假装严格计费）；ARCHITECTURE.md §3/§11 相应一句；`openspec validate` + 总纲任务组 7 回勾（记分板 H5 ✅）；提交拆分：类型+模块 / 接线 / 弃用+测试+docs
