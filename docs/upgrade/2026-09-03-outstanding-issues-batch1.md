# 升级须知：遗留问题批次一（2026-09-03）

对应计划 `docs/superpowers/plans/2026-09-03-outstanding-issues-batch1.md`，
依据 `docs/follow-ups/2026-09-03-outstanding-issues.md`。

**本批次共五处 host 可见的变更**，按需要采取动作的紧迫度排列。
其中只有第 1 条是「不改就一直错」，其余四条是新增或口径收紧。

---

## 1. task 的成果物与死因现在真的能从事件流恢复了（行为修复）

**改动前**：`TaskView.outputs` 与 `TaskView.error` 在真实事件流上**恒为 `None`**。

原因是数据和读它的人挂在两条不同的事件上 —— `TaskFinished` 的 payload 一直带着
`outputs`，但 reducer 不读它；而 reducer 去读的 `TaskFinalized` 从来不发这两个键。

**改动后**：reducer 改从 task 终态事件读 ——
`TaskFinished.outputs` → `TaskView.outputs`；
`TaskFailed.error_message` / `TaskInterrupted.error_message` → `TaskView.error`。
`TaskFinalized` 只再负责写 `finished_at`。

**host 要做什么**：
- 若你此前因为「投影里 outputs 恒空」而自建了旁路（例如自己缓存 `TaskFinished` 的
  payload），**现在可以撤掉**，投影是可信的了。
- **存量事件流无需迁移**：这个修法之所以选「改读侧」而不是「补发射侧 payload」，
  正是因为存量日志里 `TaskFinished` 本来就带着 `outputs` —— 重放历史事件也能正确还原。

**仍未解决的相邻缺口**（见总账 A8/A9，属独立立项，不在本批次）：
- **非终态**任务的中途产出仍无法从事件流恢复（没有事件承载它）；
- `error_code` 不进投影，故**跨重启后按码分流会退化**成「通用中断」。

---

## 2. `ObserveCompleted.used_llm` 的语义变了

| | 含义 |
|---|---|
| 改动前 | **尝试过** LLM observer（`_should_use_llm` 为真即置 `true`） |
| 改动后 | 判决**真的出自** LLM observer |

差别出现在 LLM 路径**走了但没出判决**的场合 —— 抛异常，或耗尽轮次没调
`report_task_outcome`。这两种情况以前报 `used_llm=true`，而判决其实来自规则；
现在如实报 `false`。

**host 要做什么**：若你用这个字段统计「observer 的 LLM 使用率」或据它判断摘要质量，
口径会变（旧口径偏高）。新口径与 `summary_length` 的关系也更直白了：
`used_llm=false` 时 `act_recap` 恒为空（见第 3 条）。

---

## 3. 非 LLM 路径不再产出机械合成的摘要

**改动前**：没有 observe ROLE 的模板（以及 root task、LLM 失败回落）走「规则观察」，
它会用 transcript 统计拼出一段摘要文本（`"Ran N conversation round(s). Tools used: ..."`）。

**改动后**：这类路径只出**判决**，不再合成任何摘要 ——
`ObserveCompleted.summary_length` 在这些场合**变为 0**。
真正的 recap 改由**后台 observe** 异步产出（`TaskRecapStarted` / `TaskRecapDone` 那一对）。

**判决本身逐字不变**：空 transcript → `fail`；`max_turns`/`context_limit` → `retry`；
`normal`/`actor_done` → `success`。**task 的终态不受影响。**

**host 要做什么**：
- 若你把 `ObserveCompleted.summary_length` 当作「有没有摘要」的信号，
  改为等待后台 recap 的那一对事件。
- ⚠️ **成本模型变了**：没有 observe ROLE 的 agent，每次机械 retry 现在会**多打一次
  后台 observe 的 LLM 调用**（以前是零 LLM 的本地合成）。
  若这类模板在你的负载里占比高且常触发 retry，请重新估算成本。

---

## 4. 新增 `ObserveStarted` 事件

前台 observe 此前有 `ObserveCompleted` 却没有起点事件，而后台链是成对的
（`TaskRecapStarted` / `TaskRecapDone`）。现补齐。

- payload：**只有 `task_id`**。「用不用 LLM」在起点还没定（取决于取消状态与
  是否有 observe ROLE），故不在起点事件里给。
- 位置：`ObserveStep` 的第一条事件，**先于任何分流决策**（含取消 token 的读取）。
- **恒与 `ObserveCompleted` 成对**：`execute` 全程无提前 return，
  取消路径下 observe 也会走完，两条照样成对且顺序恒定。

**host 要做什么**：纯新增，无需动作。想画 observe 阶段耗时的话现在有起点了。

---

## 5. 两处 payload 的口径收紧

### `RunCanceled.payload` 新增 `source`

由 `{run_id}` 变为 `{run_id, source}`，`source` 取 `"token"`（本 runtime 的协作取消）
或 `"external"`（进程 shutdown 等外部 asyncio 取消）。

这两种此前在事件流里**长得完全一样**（都是同一个 `CancelledError`）。

**已知局限**：`RunCanceled` 只在「这次取消真的会改变 task 终局」时才发，
所以熔断内部清场那条路径不会留下来源标签。

**注意**：`TaskCanceled.payload` **保持字面 `{}` 不变** ——
「取消不编造 reason」是一条刻意定下的契约，本次未动。

### `TaskQueueInterrupted.reason` 的兜底改为吐码

该字段**按设计就是码**（host 据此分流，如 `CONTEXT_OVERFLOW` → 提示换更大窗口的模型），
这一点三份契约都写着，本次**没有改变**。

改的是它的**兜底**：取值链原为 `error_code or error or "interrupted"`，
中间那项是**自由文本**，于是 `error_code` 为空时该字段会吐散文，与自身契约相悖。
现删去中间项 → `error_code or "interrupted"`，恒是码。

**什么时候会走到兜底**：`error_code` 为空的主要场合是**跨重启还原的任务** ——
`error_code` 不进投影（总账 A9）。所以重启后这个字段会退化成通用码 `"interrupted"`，
而不是像以前那样混进一段自由文本。

**host 要做什么**：若你此前在这个字段上做过「像码就分流、像散文就展示」的启发式判断，
可以简化成纯按码分流；展示用的自由文本请读 `TaskInterrupted.error_message`。

---

## 不在本批次、但你可能关心的

- **`error_code` 跨重启丢失**（A9）：三份契约说 host 按码分流，但重启后码就没了。
  要修需给 `TaskView` 加字段，属独立立项。
- **非终态任务的中途产出不可恢复**（A8）：需要一条承载它的事件。
- 完整的遗留清单见 `docs/follow-ups/2026-09-03-outstanding-issues.md`。
