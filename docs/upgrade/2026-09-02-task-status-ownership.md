# 升级须知 · task 状态所有权重构（2026-09-02）· **破坏性**

## 先读这三条

1. **`RunFinished` 新增 `outcome`（run 词表五值之一：`completed` / `awaiting_human` /
   `suspended_on_children` / `interrupted` / `canceled`）。旧键 `final_status`
   **已废弃、下一个发布周期删除**——host 若靠它推 task 状态，改订 task 事件；
   继续读 `final_status` 只会读到「发 `RunFinished` 那一刻」task 的状态（多半仍是
   `ACTIVE`），因为 task 的终态现在是 `RunFinished` 发出之后才由 `TaskManager` 写定的
   （见第 3 条）。
2. **task 状态事件现在只从 `TaskManager` 发出。** loop（`runtime.py` /
   `finalize.py` / `suspend.py` / `observe.py` / `control_capability.py`）只交出
   `RunOutcome`（发生了什么），`TaskManager.apply_run_outcome` 据 `disposition_for`
   那张表判定「task 该变成什么」并发那一条事件——两道静态守卫钉住 loop 侧不再直接写
   `task.status`、不再直接发任何一条 task 状态事件。受影响的九条：`TaskStarted`（本来
   就是 TM 发，不变）、`TaskSuspended`、`TaskAwaitingHuman`、`TaskInterrupted`、
   `TaskFinished`、`TaskFailed`、`TaskCanceled`、`TaskRequeued`（observer 判重试
   那一种形状；reopen 那一种一直是 TM 发，不受影响）、`TaskResumed`（本来就是 TM
   发，不变）。
3. **这些事件的 `run_id` 从有变为 `None`。** `TaskManager` 不属于任何一次 run——
   它在 run 返回之后才动手，连 `run_id` 都拿不到。host 若按 `run_id` 归组 task 事件
   （例如「这次 run 产生了哪些 task 事件」），要改成按 `session_id` + 到达顺序，或者
   干脆不再依赖 `run_id` 做 task 事件的归组。
   **顺带一条事件顺序提醒（brief 没提，这里补上）：** 这些 task 状态事件现在**在
   `RunFinished` 之后**到达（`RunFinished` 在 `_run_loop` 的 `finally` 里无条件发，
   `TaskManager` 要等 `execute()` 把 `RunOutcome` 交回来才处置）。而 `RunFinished`
   历来是 host 关 SSE 流的信号——**若 host 收到 `RunFinished` 就立刻关流，会漏掉紧随
   其后的 task 事件**（task 的终态、error、retry_count 等都在这些事件的 payload
   里）。正确做法：关流前先等这次 run 对应的 task 状态事件到达，或者干脆不再靠
   `RunFinished` 单独判断「可以关流了」。

事件**语义**与 task 的**最终状态**一个都没变——这是一次纯所有权重构，不改行为。

判断你是否受影响：搜这些字符串，命中即需要检查。

```
final_status              run_id                RunFinished
TaskSuspended              TaskFinished          TaskFailed
TaskCanceled                TaskInterrupted      TaskRequeued
run_failure_retry
```

---

## `TaskRequeued.reason`：崩溃后原地重试的字面量变了

崩溃后还能原地重试那一支（`disposition_for` 判 `retriable` 且预算未尽）发的
`TaskRequeued.reason`，从 `"run_failure_retry"` 改成了 `"run_crash"`——与崩溃挂起支
`TaskInterrupted.reason`、run 域 `RunInterrupted.reason` 用的是同一个字面量，三条
事件现在同源、不再各写一份。

`reason` 只是溯源展示，**判据从来是事件类型不是这个字符串**——`"run_failure_retry"`
本身也从未被任何消费方读取过（`grep` 全仓无命中）。若你的 host **恰好**在某处按这个
旧字符串做过分流或展示映射，改成 `"run_crash"`；多数 host 不受影响。

---

## `TaskSuspended` 的 payload 不再带 `task_id`

旧 payload 是 `{task_id, summary, spawn_titles}`；新的是 `{summary, spawn_titles}`。
`task_id` 本来就是 envelope 字段的重复（`docs/events-v2.md` §0：「payload 里不再重复
`task_id`」是 V2 的既定清理方向，`TaskSuspended` 只是这次才补上），`docs/events-v2.md`
§2.3 记的本来也只有 `summary` / `spawn_titles` 两个 payload 字段。若你的 host 曾从
`TaskSuspended.payload.task_id` 取值，改读 envelope 的 `task_id`。

---

## `run_single_task`（Phase-1 compat 路径）的上下文溢出行为向真会话对齐

`run_single_task` 是没有队列、不经 `TaskManager._run_task` 的一次性执行入口
（Phase-1 兼容层）。这次重构把它也接上了同一份 `RunOutcome` → `apply_run_outcome`
消费——之前它压根没人接手 outage/park/取消这些结局，现在四条都对齐了真会话的行为：

| 项 | 旧 | 新 |
|---|---|---|
| `task.status` | `SUSPENDED` | `INTERRUPTED` |
| task 事件 | **一条都不发** | 发 `TaskInterrupted` |
| 队列信号 | `TaskQueueBlocked` | `TaskQueueInterrupted` |
| 会话状态 | `SessionWaiting` | `SessionInterrupted` |

**这不算破坏性变化，理由如下：** 旧的 `SUSPENDED` 是崩溃支就地写的一个过渡值，
这条路径本来就不经 `_run_task`、不调 `_handle_task_failure`——**没有任何东西会去
接手**它，task 停在 `SUSPENDED` 之后不会再被任何 `/resume` 逻辑重排。更关键的是，
它**从未进过事件流**：BASE（重构前）这条路径对上下文溢出**一条 task 事件都不发**，
host 的投影本来就停在 `ACTIVE`（因为唯一能推进投影的信号——事件——从未到达）。
新行为发 `TaskInterrupted`，是这条路径**第一次**把「溢出了」这件事写进事件流；
之前依赖这条路径推 task 状态的 host 集成，一直读到的都是过期的 `ACTIVE`,
这次是把一个从未工作过的信号修好，不是把一个工作着的信号改坏。

若你的 host 走 `run_single_task` 且曾经手工兜底过「这条路径的投影不会动」，
现在可以删掉那个兜底——`TaskInterrupted` / `TaskQueueInterrupted` /
`SessionInterrupted` 会正常到达。

---

## 终态守卫收紧：熔断竞态下旧行为会把 `FAILED` 覆写成 `PENDING`

`apply_run_outcome`（新）在终态上加了一道守卫：task 已经是 `FINISHED` / `FAILED` /
`CANCELED` 时，非 `COMPLETED` 的结局不会再改写它、也不会再发事件——直接返回已写定的
状态。旧的 `_handle_task_failure`（崩溃处置的旧入口）**没有**这道守卫。

两者在下面这条竞态里行为不同：熔断（`FailureThresholdHit`）已经把一个在跑的 root
task 判成 `FAILED`（`TaskManager` 第 6 步「root 判 FAILED」），随后对它发协作取消，
若那次取消没有生效在「取消」上、而是表现为**执行侧崩溃**（run 抛异常、不是
`CancelledError`）——

- **旧行为**：崩溃重抛给 `_handle_task_failure`，它不知道 task 已经是终态，
  按普通崩溃走一遍「能不能重试」的判断，把已经写定的 `FAILED` 覆写成
  `PENDING`（还能重试）或 `INTERRUPTED`（不能重试），并发出对应事件——**已经宣布
  过的 `FAILED` 被悄悄翻回了非终态**。
- **新行为**：`apply_run_outcome` 的终态守卫命中，直接返回 `FAILED`，**不改状态、
  不发事件**。

方向与本计划另一处既有守卫（`_run_loop` 的 `cancel_takes_effect`：熔断判死的 root
不该被那次协作取消的 `CancelledError` 翻回 `CANCELED`）一致——**熔断已经判死的 root
不该被那次清场动作本身产生的任何后续结局复活**。这条竞态窗口本来就窄（取消信号
与执行侧异常的时序重叠），若你的 host 观测到过熔断之后某个 root task 从 `FAILED`
「复活」又消失过，这次修的就是它；若从未观测到过，无需改动。

---

## 事件语义与 task 最终状态：一个都没变

这是纯所有权重构：同一份判据（原来散在 `FinalizeStep` / `ObserveStep` /
`TaskManager._handle_task_failure` / `_run_loop` 的 except 链四处）收进了
`task_disposition.py` 的 `disposition_for` 一张纯函数表，**不改变任何一条今天的
转移结果**。`docs/spec/golden/*.json` 的 task 最终状态逐字未变，只是发射者、
`run_id`、（部分事件）相对 `RunFinished` 的顺序换了——见上面三条。
