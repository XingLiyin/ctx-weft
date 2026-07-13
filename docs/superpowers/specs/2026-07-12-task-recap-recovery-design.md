# 崩溃恢复 background observe（Task Recap）方案

> 日期：2026-07-12
> 目的：让被崩溃打断的 background observe（段摘要 / finish 对 refine）在恢复时**重跑**，并把因此卡在
> 非终态的 session 收干净。围绕根因给出事件溯源的自洽方案，覆盖**全部** observe 段边界。
> 关联：`2026-06-28-background-observe-as-observe-summarizer-design.md`（background observe 语义）、
> `2026-07-01-observe-recap-budget-escalating-compact-design.md`（段折叠双路径）、
> `2026-07-04-tm-mechanism-and-recovery-design.md`（recover_session / 恢复机制全景）。

---

## 1 · 问题与根因

### 1.1 症状

一个 session，root task 已 `finish_task` 收尾（`TASK_FINISHED` 落盘、task 投影 = FINISHED），但收尾段的
background observe 还没跑完，进程被中断。下次启动：前端显示"恢复会话"按钮，**点击后卡死、无反应**。

### 1.2 根因链（代码确定，无需运行数据）

对 finish 收尾，step 流是 `observe → finalize`：

1. **`observe`**（`loop/steps/observe.py:263-267`）：在 `actor_done` 边界 fire-and-forget 启动
   `launch_background_observe(boundary="finish")`，经 `track_background` 登记到 TaskManager，然后进 `finalize`。
2. **`finalize`**（`loop/steps/finalize.py`）：发 `TASK_FINISHED` + `TASK_FINALIZED`（**落盘**）→ task 投影 = FINISHED；
   同时 `_synthesize_dispatch_pair` 写占位 finish 对、`register_close_synth` 登记（**纯内存**，待 bg 回调 replace）。
3. run 返回 → `_run_task` → `on_task_finished` → `is_done()` 为真 → `_fire_session_done()`。
4. **关键**：`_fire_session_done`（`orchestrator/task_manager.py:692-720`）在发
   `SESSION_STATUS_CHANGED(SUCCEEDED)` / `SESSION_FINISHED` **之前**，先
   `await asyncio.gather(*self._background_asyncio_tasks)`（`:698-699`）——阻塞等那个没跑完的 background observe。
5. 进程正是在这个 gather 里被中断 → 两条 session 终态事件**从未落盘**。

崩溃后持久化里的一对矛盾：

| 对象 | 持久化状态 | 原因 |
|---|---|---|
| root task | **FINISHED**（终态） | `TASK_FINISHED` 已落盘 |
| session | **RUNNING**（非终态） | `sess.status` 只被 `SESSION_*` 事件推动（`reducers.py:381-428`）；`SESSION_FINISHED` 没来得及发。`RUN_FINISHED(FINISHED)` 只改瞬态 `view.session_status`、**不改** `sess.status`（`reducers.py:371-378`） |

### 1.3 为什么"点了卡死"

`recover_session` → `_recover_session_locked`（`runtime.py:909`）。冷启动无存活 owner → 走重建：

```python
resumable = [t for t in all_tasks if t.status not in _TERMINAL]   # runtime.py:970
if not resumable:
    raise RuntimeError(f"Session {session_id!r} has no resumable tasks")   # :972-973
```

唯一的 task 已 FINISHED → `resumable` 为空 → **直接抛 `RuntimeError`**。恢复死胡同：事件流上什么都没发
（没有 `SESSION_RESUMED`、没有新 run），前端 SSE 等不到任何东西 → "卡死"。session 永远停在 RUNNING，
**每次重启复现同一死胡同**。

一句话：**session 实际已完成（唯一 task FINISHED），但它的"完成"落盘被 `_fire_session_done` 里对后台 observe 的
`gather` 门控；崩在这窗口后，恢复路径把"session 非终态 + 无可恢复 task"当错误抛出，而不是当"补收尾"。**

### 1.4 关键事实（设计据此成立）

- **没有生命周期标记**：现有 `BACKGROUND_OBSERVE_*` 事件（`events/types.py:148-151`）是**每轮 LLM 交互**的流式事件，
  不是"这个 observe 起了 / 完了"的信号；`BACKGROUND_OBSERVE_TOKEN_STREAMED` 还是 transient（不落盘）。
- **`await_pending_background_observe` 无生产调用方**——即便正常运行，段折叠也是 best-effort/racy，崩溃只是让它彻底不发生。
- **重建 loop 上下文有先例**：`compact_session`（`runtime.py:1057`）已示范从投影重建 session/agent/task +
  `_build_assembler/_build_gateway/_build_loop_ctx/_resolve_llm` 跑一次性 step。
- `LoopContext`（`runtime.py:1444-1460`）携带 `event_bus` 与 `task_manager`，故 observe 内可发标记、`_build_loop_ctx`
  传入新 TM 即可让重跑的 observe 经 `track_background` 登记到它。

---

## 2 · 方案总览

给 background observe 套一对**持久化生命周期事件**，使其**重启安全**：恢复时折出 pending 集合，逐个重建 loop 状态
并在新 TaskManager 上**重跑**，随后交给**既有** `_fire_session_done` 的 gather 机制收尾。

命名：新事件属于 **Task Recap** 域（background observe 的产物即"段 recap / finish 对 refine"）。
`launch_background_observe` 等函数名不变，只有新的持久化生命周期事件用 Task Recap 命名。

---

## 3 · 检测：两个持久化事件 + 一个 fold

### 3.1 新 EventType（`events/types.py`）

```python
TASK_RECAP_STARTED = "TaskRecapStarted"   # payload: {task_id, boundary, agent_id}
TASK_RECAP_DONE    = "TaskRecapDone"      # payload: {task_id}
```

- **非 transient**：必须落盘（不进 `TRANSIENT_EVENT_TYPES`）。
- **reducer no-op**（task/session 状态）：不进 `TASK_STATUS_BY_EVENT`，`_apply` 不处理——它们无状态机含义，
  只作恢复记账（与 memory 类事件同性质）。

### 3.2 fold（`control/reducers.py`）

```python
def fold_pending_task_recap(events) -> dict[str, dict]:
    """started − done，按 task_id last-write-wins（仿 fold_pending_hitl）。
    返回 {task_id: {"boundary": str, "agent_id": str}}。"""
```

- 语义：某 task 有 `TASK_RECAP_STARTED` 而无其后的 `TASK_RECAP_DONE` → pending（该 recap 未持久完成）。
- 供 `rebuild_view` 的调用方（`_recover_session_locked`）折出待重跑集合。轻查询可复用现有
  `read_session_events_of_types` 只取这两类，退化时全量过滤（与 `_pending_hitl` 同型）。

---

## 4 · 打标记：`loop/steps/background_observe.py`

### 4.1 起点

`launch_background_observe(state, ctx, *, boundary)`：在 `asyncio.create_task` 前，用快照 `state` 经
`ctx.event_bus` 发 `TASK_RECAP_STARTED`，payload = `{task_id: state.task.id, boundary, agent_id: state.agent.id}`。

### 4.2 终点

`_run_background_observe`：在 `finally` 里发 `TASK_RECAP_DONE(task_id)`，**在所有 memory 写之后**
（`_replace_finish_report` / `_close_report` 落槽 / `apply_compact` 均已完成）。因此"started-without-done"
可靠地表示"未持久折叠"。现有 best-effort `except`（段保 raw）保留；DONE 无论成功/空报告/异常都发——
**唯有崩溃**才会让 DONE 不发。

> 注意 DONE 必须在 memory 写**之后**发，否则先发 DONE 再崩会漏掉重跑；见 §6 幂等窗口分析。

---

## 5 · 恢复：`_recover_session_locked` 重跑 + 收尾

### 5.1 折出 pending 并逐个重跑（全边界）

`rebuild_view` 后：`pending_recap = fold_pending_task_recap(events)`。对其中每个 `task_id`（不在重建 tasks 里则跳过）：

1. **重建 LoopState**（仿 `compact_session`）：由投影 `agent_id` 经 `lm.instantiate_agent(existing_agent_id=...)`
   得 agent；`task_from_projection` 得 task；`scope = MemoryScope(session, task, agent)`；
   `_build_assembler/_build_gateway/_resolve_llm/_build_loop_ctx` 构 ctx，**`_build_loop_ctx(task_manager=新TM)`**
   使重跑 observe 登记到新 TM。
2. **close 边界**（`finish`/`normal`）：从 memory 读占位 finish 对的 `tool_call_id`（`AGENT_CONVERSATION_TURN` 中
   `role==assistant` 且 `origin_task_id==task_id`、带 `finish_task` tool_call 的那条），由 task 状态定 `outcome`
   （FINISHED→success，FAILED→fail），`register_close_synth(task_id, tool_call_id, scope, outcome)`——
   使重跑经既有 `_replace_finish_report` 路径 supersede 占位对。找不到 finish 对则跳过 replace 登记（best-effort，占位保留）。
3. `launch_background_observe(state, ctx, boundary=boundary)` → 登记到新 TM。

interrupt/plain_text 的 task 同时出现在 `resumable`（被 restore 重排），其段 recap 在此并发重跑，与被 resume 的 run
互不阻塞——与今天的 best-effort、非阻塞语义一致。

### 5.2 无可恢复 task 时显式收尾（卡死场景）

finish/normal 卡死场景**无可恢复 task**，故去掉今天的 `raise RuntimeError("no resumable tasks")`，改为：

- 有 pending_recap 但无 resumable → 仍 `_register_and_drain`（把重跑 recap 登记为 tracked 后台任务），
  再**显式**驱动会话收尾：新增 `TaskManager.finalize_idle_session(status)`，复用 `_fire_session_done` 体
  （`await asyncio.gather(后台任务)` → 发 `SESSION_STATUS_CHANGED` + `SESSION_FINISHED` → 回调）。
  终态镜像 `on_task_finished`：`"FAILED" if session.failure_counter > 0 else "SUCCEEDED"`（failure_counter 取自投影）。
- **既无 resumable 又无 pending_recap** → 保持 `raise RuntimeError("no resumable tasks")`（确无事可做，行为不变）。

> 空队列的新 TM 不会自发 `_fire_session_done`（`is_done` 为真但无 `on_task_finished` 触发），故 §5.2 的显式驱动是必要的。

---

## 6 · 幂等与错误处理

唯一隐患：崩溃落在 memory 写**之后**、`TASK_RECAP_DONE` 落盘**之前** → 恢复重跑一个已完成的折叠。两路不同：

| 路径 | 重跑行为 | 结论 |
|---|---|---|
| **close（`_replace_finish_report`）** | 按 `(tool_call_id, origin_task_id)` 定位已 refine 的 finish 对，supersede 后按新 recap 重写。无重复（supersede 按 id 幂等），内容语义等价。 | **天然幂等，安全** |
| **compact（`apply_compact`，interrupt/plain_text）** | 丢失的那次已 supersede raw 并写 `TASK_COMPACT_SUMMARY`；重跑面对"raw 已消失"的段，可能产冗余第二段胶囊。 | **唯一非幂等路径** |

**compact 路径护栏**（采纳）：重跑前查该段是否仍有未折 active raw（末 `USER_PROMPT`/`TASK_COMPACT_SUMMARY` 锚点
之后的 `LLM_RESPONSE`/`TOOL_*`）。已无 → 视为已折叠，直接发 `TASK_RECAP_DONE`、跳过重跑。护栏局部、廉价、精确。
（退化选项：接受罕见的 cosmetic 双胶囊——recap 本就 best-effort，spec §3.6。本方案取护栏。）

其余均 best-effort：单个 task 的重建失败 → 记日志并继续，绝不阻塞其余 recap 重跑或 session 收尾。

---

## 7 · 测试计划（TDD，先写失败测试）

- **Unit `fold_pending_task_recap`**：started-only → pending；started+done → 空；同 task_id last-write-wins。
- **Unit 打标记**：`launch_background_observe` 发 `TASK_RECAP_STARTED`；`_run_background_observe` 在**全部**出口
  （成功 / 空报告 / 异常）发 `TASK_RECAP_DONE`。
- **Integration（复现报告的 bug）**：event store 预置 root task `FINISHED` + `TASK_RECAP_STARTED`（无 DONE）+
  session 投影 `RUNNING`（无 `SESSION_FINISHED`）。`recover_session` → 断言 observe 重跑、finish 对在 memory 被 replace、
  发 `SESSION_STATUS_CHANGED(SUCCEEDED)` + `SESSION_FINISHED`、session 达 `SUCCEEDED`。直接闭合"卡死"。
- **Integration（interrupt/plain_text）**：崩溃时有 pending recap + 一个 SUSPENDED resumable task → 恢复既重排 task
  又重跑 recap；均完成；护栏防止双折。
- **Edge**：无 resumable task **且**无 pending recap → 仍 `raise "no resumable tasks"`（行为不变）。
- **Regression**：`test_background_observe*`、`test_crash_recovery_reconcile`、`test_outage_resume` 保持绿。

---

## 8 · 改动清单

| 文件 | 改动 |
|---|---|
| `core/events/types.py` | 加 `TASK_RECAP_STARTED` / `TASK_RECAP_DONE`；确认不进 `TRANSIENT_EVENT_TYPES` / `TASK_STATUS_BY_EVENT` |
| `core/control/reducers.py` | 加 `fold_pending_task_recap`；`_apply` 对两新事件 no-op（默认即 no-op，无需分支） |
| `core/loop/steps/background_observe.py` | `launch_background_observe` 发 STARTED；`_run_background_observe` 在 `finally` 发 DONE（memory 写之后）；compact 路径重跑护栏（供恢复复用的"仍有未折 raw"判定） |
| `core/orchestrator/task_manager.py` | 加 `finalize_idle_session(status)`（复用 `_fire_session_done` 体） |
| `core/runtime.py` | `_recover_session_locked`：折 pending_recap、重建 LoopState 重跑（close 边界补 `register_close_synth`）、无 resumable 但有 pending 时显式收尾；去掉无脑 `raise`（仅"既无 resumable 又无 pending"才抛） |
| `tests/` | §7 全部 |

---

## 9 · 非目标（YAGNI）

- 不把 background observe 建模为一等持久 Task（与调度/失败计数纠缠，过度设计）。
- 不引入状态形状推断作为**唯一**检测（覆盖不了 interrupt/plain_text）——事件标记是单一真相。
- 不给 `_fire_session_done` 改"先发终态再 gather"（那是另一条正交改进；本方案通过恢复重跑闭合根因，不改正常路径的收尾顺序）。
