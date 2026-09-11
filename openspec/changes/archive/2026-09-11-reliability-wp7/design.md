# Design: reliability-wp7

## Context

三个死字段已核实（template.py:75-76 定义 + _loader.py:185-186 解析；task.py timeout_ms 透传至 converters/reducers/session_registry 投影；core 内唯一 `asyncio.timeout` 是 HITL 等待）。活限制对照组：`max_turns_per_act`（act.py:68）/ `max_turns_per_observe`（observe/background_observe）/ `context_limit`（budget）。INTERRUPTED 通道（wp6 TOOL_OUTCOME_UNKNOWN 同族）与 TaskErrorCode 枚举就绪。

## Goals / Non-Goals

**Goals:** ExecutionLimits opt-in 语义成立（默认 None = 全量回归零变化）；四错误码 + unknown 接缝；三个旧字段去重警告；L-T 系列测试钉住。

**Non-Goals:** 不激活旧默认、不做 OS 沙箱、不动既有活限制、PostgreSQL（WP8）。

## Decisions

### D1：ExecutionBudget 模块与注入面（P1）

`core/control/execution_budget.py`：`ExecutionLimits`（frozen dataclass，五字段全默认 None/5.0）+ `ExecutionBudget`（mutable 计量器：`consumed_active_sec` / `consumed_turns` / `parked_accum`，`park()/resume()` 累计等待时段，`check()` 返回首个超限项）。注入：`RuntimeConfig.execution_limits: ExecutionLimits | None = None`；runtime 构造期把 limits 连同可注入 monotonic clock 装进 `LoopContext.config` 相邻位（与 wp3 的 `event_commit_policy` 同一注入面先例）。**per-task 实例**：TaskManager 派发时按 `(session, task)` 建 budget，恢复路径从 task 投影字段（`task.metadata` 增 `budget_consumed`，随 TASK_STARTED 事件投影持久化——复用既有投影通道，不建新存储）。

### D2：计量实现——wall-clock 减 parked（P2）

```python
active_now = monotonic() - run_started_monotonic - parked_accum   # 本 run 净活动
total_active = persisted_consumed + active_now                    # 跨 retry 累计
```

park/resume 挂点：**HITL park 上抛/恢复**（HitlPark 分支与 `_resume_in_existing_tm`）、**SUSPENDED 等子任务**（SuspendStep 与 resume 重入）。排队不计 = budget 只在 run 进行中流动，TM 排队期天然不 tick。actor turns：act 的每次**逻辑** LLM 请求前 `budget.consume_turn()`（`stream_llm_resilient` 的自愈重试在内部，不经过）；派发前**预占**一轮（方案原文：崩溃恢复不超支）。checkpoint：StepDriver 每步边界 `budget.check()` + act 工具循环内每轮边界——纯内存比较，≤1s 周期由 act 长循环内附加边界保证；**持久化时机** = 每 checkpoint 把 consumed 写回 task 内存字段、随下一次 TASK_* 事件投影落库（不额外发事件）。

### D3：超限结局走 INTERRUPTED 现有通道（P2 收尾）

`budget.check()` 命中 → 抛 `ExecutionLimitExceeded(code)`（CtxWeftError 子类，`retriable=False`）→ `_run_loop` 现有 except 链自然归 INTERRUPTED（与 LLM_OUTAGE 同形）；`task.error_code` 置四码之一。**provider 超时**：`_stream_tool` 外包 `asyncio.timeout(provider_timeout_sec)`，超时 → CancelledError 捕获后：先查账本状态（wp6 的 ledger record），已 started → 按超时不证明未发生原则走 `_mark_unknown` 路径（复用 reconcile 的 helper 提为模块级）；未 started → 直接 PROVIDER_DEADLINE_EXCEEDED。**cleanup_grace**：gateway `_stream_events_safe` 的 CancelledError 分支已有 provider.cancel() 安全网——grace 等待加在其后（`asyncio.wait_for(shield, grace)`）；超时后 provider 标记 `_uncooperative` 集合（runtime 级），后续同会话 invoke 检查拒绝。

### D4：旧字段警告的去重点（P3）

两个发点各持 `warnings.warn(..., DeprecationWarning, stacklevel=2)` + 模块级 `_warned: set[str]`（进程内每字段一次）；loader（`_loader.py` 解析非默认值时）与 runtime（SessionRegistry 建 task 时 `timeout_ms != default` 时）。**不激活**：不删字段、不改默认值、不映射。README 事件系统节后加「执行限制」小节。

### D5：测试策略——算术与集成分离

- `test_execution_budget.py`：注入 fake monotonic（列表弹出式），纯算术断言（park 不计 / retry 累计 / 重启恢复 / 漏记 ≤ 周期 / check 首超限项）。
- `test_execution_limits.py`（integration）：真 runtime + MockLLM——合作超时（LLM 慢响应 barrier 驱动）、等 HITL 不计（park 真实发生）、retry 累计（observer 判 retry 两次）、重启恢复（事件投影含 budget_consumed）、不合作 provider（stub 永不返回）、旧字段警告（pytest.warns 去重）。
- 全量回归 = 默认 None 零变化门禁。

## Risks / Trade-offs

- [park/resume 挂点遗漏（某等待形态未停表）] → L-T02 覆盖 HITL + 子任务两形态；遗漏表现为限制偏紧（多计等待），不安全事故方向。
- [consumed 持久化随事件投影滞后] → 漏记声明 ≤1 checkpoint 周期（文档如实）；计费级精确度归宿主另接账本。
- [provider_timeout 与 LLM 自愈预算叠加混乱] → provider_timeout 只包 provider 流消费，自愈预算在 LLM 层独立——两层各自声明，文档说明叠加关系。
- [默认 None 被意外破坏] → 全量回归（3027+ 基线）是硬门禁。

## Migration Plan

无数据迁移（budget_consumed 新字段随投影自然出现）；回滚 = revert（旧字段警告无害保留）。宿主灰度：先小范围只读任务配 task deadline，再全量。

## Open Questions

（无——P1/P2/P3 已定；方案 §6 固定其余。）
