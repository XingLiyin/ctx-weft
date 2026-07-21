# 胶囊 close 末段 raw 延迟折叠（deferred close raw fold）

日期：2026-07-20
状态：已批准（用户确认设计后实施）

## 问题

`_close_one`（finalize.py）对非 short 任务在 close 时刻**无条件同步 supersede 末段 raw**
（LLM_RESPONSE / TOOL_INVOCATION / TOOL_RESULT）。但规则降级 observe（root task 或无
observe ROLE）产出的 finish 对只是模板占位（`Ran N conversation round(s)…`），真摘要要等
后台 close observe 异步产出并经 `_replace_finish_report` 替换。两个后果：

1. **竞态窗口**：紧随其后的任务在 bg observe 完成前装配 context（一个 run 只在 prepare
   装配一次），整个 run 只能看到占位符——raw 已删、真摘要未到（实证：
   `tsk_01KY161P064JMVSKW3DYT8TY3W` 全部 36 轮 prompt 均为占位符）。
2. **信息丢失**：bg observe 失败/无可用报告/进程崩溃且无恢复时，占位符永久留存，
   末段执行内容不可恢复。

## 决策

**「折 raw」推迟到「LLM 总结真正落地」之后**。close 时刻若尚无 LLM 总结，末段 raw 保持
active（渲染形态与既有 short 任务同构：全 raw + finish 对）；bg observe 替换 finish 对成功
后补删 raw；bg 失败则 raw 永久保留（降级 = 保 raw，与段边界折叠的既有契约对齐）。

判据：`verdict.reported`——True 表示前台 LLM observe 走成了 `report_task_outcome`，
close 时 task_summary 已是真摘要。

## 行为矩阵

| close 时状态 | close 时刻 | 之后 |
|---|---|---|
| short 任务 | 不删 raw（不变） | — |
| 非 short + `reported=True` | 同步删 raw（不变） | — |
| 非 short + `reported=False`，bg 已先完成（`pop_close_report` 命中） | 替换 finish 对后立即补删 raw | — |
| 非 short + `reported=False`，bg 未完成 | 不删 raw，登记 `raw_fold_scope` | bg 替换成功 → 补删；bg 失败/无报告 → raw 保留 |
| 崩溃恢复 `_relaunch_task_recap`（close 边界） | — | 登记带 `raw_fold_scope`，重跑 recap 成功后补删 |

## 改动点

1. **`finalize.py`**
   - `FinalizeStep.execute` → `finalize_task_memory(..., has_llm_summary=bool(verdict and verdict.reported))`。
   - `finalize_task_memory` / `_close_one` 增 keyword `has_llm_summary: bool = True`
     （默认 True = 旧行为，兼容既有测试）。
   - `_close_one`：`raw_fold_scope = state.scope if (not short and not has_llm_summary) else None`；
     同步 supersede 仅在 `not short and has_llm_summary` 时执行；`raw_fold_scope` 透传给
     当次实际发生的 `_synthesize_dispatch_pair` 调用（same-agent 嵌套 / own-root 二选一）。
   - `_synthesize_dispatch_pair` 增 keyword `raw_fold_scope: MemoryScope | None = None`：
     `pop_close_report` 命中 → `_replace_finish_report` 后若有 `raw_fold_scope` 立即
     `_supersede_final_raw_segment`；未命中 → `register_close_synth(..., raw_fold_scope)`。
   - `_supersede_final_raw_segment` 签名改收 `provider_ctx`（原收 LoopContext），供
     finalize 与 bg 模块两侧共用。
2. **`background_observe.py`**
   - `_close_synth` 元组扩为 `(tool_call_id, scope, outcome, raw_fold_scope)`；
     `register_close_synth` 增第 5 参（默认 None）。
   - close 回调 `_replace_finish_report` 成功后，若 `raw_fold_scope` 非 None →
     `_supersede_final_raw_segment(ctx.memory, raw_fold_scope, ctx.provider_ctx)`。
   - 失败/无可用报告路径不变（弹登记、raw 保留）。
3. **`runtime.py` `_relaunch_task_recap`**：close 边界 `register_close_synth` 补传
   `raw_fold_scope=scope`（该路径只对规则 observe 的 close 触发，raw 必然 active；
   `_supersede_final_raw_segment` 对已删 raw 是 no-op，幂等安全）。

## 不变量修订

spec 2026-06-28 §3.2「末段已由 finish 对的 Process Report 承载 → 直接 supersede」修订为：
**末段 raw 与「真实 Process Report」二者始终至少存其一**。finish 对占位期间 raw 保持
active；真报告落地的同一因果链内完成折叠。

## 影响面

- 渲染：deferral 窗口内形态 = short 任务形态（全 raw + finish 对），composer 无需改动。
- 代价：窗口内下一任务的 prompt 携带上一任务末段 raw（token 增大），换取信息不丢失。
- 同 agent 子任务若规则 observe close（无 bg observe 发射）：raw 永久保留——符合
  「无 LLM 总结即用 raw」语义。
- `register_close_synth` 泄漏面与现状相同（bg 失败路径已弹登记）。

## 测试

`tests/unit/test_close_process_report_a1.py` 扩展 + `tests/unit/test_close_task.py`：
1. 非 short + `has_llm_summary=False` close → raw 仍 active；
2. bg 回调替换成功 → raw 被 supersede；
3. bg 失败（无可用报告/异常）→ raw 保留、登记弹掉；
4. 非 short + `has_llm_summary=True` close → close 即删（回归）；
5. short 任务 → 永不删（回归）；
6. `pop_close_report` 先到 → 替换后立即补删；
7. 既有 3 元组解包用例更新为 4 元组。
