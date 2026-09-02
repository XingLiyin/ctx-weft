# 02 · Step 状态机与恢复语义

> 真相源：`core/loop/driver.py`、`runtime._resolve` / `_build_step_driver`、`control/reducers.rebuild_view`

---

## initial_step 选择

一个 task 从哪个 Step 起步，由 `task.settings` 决定：

| `task.settings` | initial_step | agent 来源 |
|-----------------|--------------|-----------|
| `NormalTaskSettings`（默认） | `reason` | 默认 / 复用 root agent |
| `NormalTaskSettings(useSubagent=true)` | `reason` | 实例化子 agent（继承父） |
| `CompactTaskSettings` | `compact` | 模板 X / 默认 agent |
| `MetadataFillerTaskSettings` | `metadataFiller` | metadata_filler 模板（失败回落默认） |

## Step 跳转表

每个 Step 返回 `next_step`（null = 本次 loop run 结束 → `RunFinished`）。

| Step | 作用 | next_step |
|------|------|-----------|
| `reason` | 装配 prompt；必要时派发 compact 子任务 | `act`；若已派发 compact → `null` |
| `act` | ReAct 循环：调 LLM → 执行 tool_call → 写 memory | `observe`；若 task 变 `SUSPENDED` → `suspend` |
| `observe` | Observer 评估，产出 Verdict | `finalize` |
| `finalize` | 写 OBSERVER_SUMMARY、置 task 终态、发 TaskFinished、（success 时）blackboard 发布 | `null` |
| `suspend` | 当前 task 挂起等子任务 | `null`（子任务完成后 TaskManager 重新入队，从 `reason` 再起步） |
| `compact` | 记忆压缩：产出 `[Context so far]`，调 applyCompact | `null` |
| `metadataFiller` | 守护任务：回填 title/description/session goal | `null` |

典型链路：`reason → act → observe → finalize → null`。

## Driver 循环不变式（每个实现必须遵守）

`StepDriver.run`：

1. **起步先持久化 user_prompt**：若 `task.userPrompt` 未入库，先 ingest 一条 `user_prompt`
   （拼 `## Current Task` + `## Current Message`），置 `userPromptInMemory = true`。保证 resume 可重建对话。
2. **建立 blackboard 订阅**（幂等，每次 run 都执行）：前序（tracking_task_ids→`predecessor`）、
   子任务（children_of→`subtask`）。详见 [04](./04-blackboard.md)。
3. 从 `initialStep` 起循环：
   - 每轮先检查取消信号（已取消则抛出 → 上层转 `CANCELED`）。
   - 发 `StepStarted` → `step.execute` → 应用 `outcome.statePatch` → 发 `outcome.events` → 发 `StepCompleted`。
   - `step.execute` 抛异常 → 发 `StepFailed` 再 raise。
   - `nextStep = outcome.nextStep`；null 时结束。

## run 生命周期与错误语义（`_run_loop`）

- 开始发 `RunStarted`（带 `runId` + `initialStep`）。
- 取消（CancelledError）→ task 置 `CANCELED`，发 `RunCanceled` + `TaskCanceled`。
- 其它异常 → task 置 `FAILED` + `task.error`；按 `retriable` 决定是否重试。
- finally：清能力缓存；计算 `willRetry`（有错 + `retryCount < maxRetries` + retriable）；
  发 `RunFinished`（含 `outcome`（run 词表五值，权威字段）/ `finalStatus`（已废弃，
  保留一个发布周期供旧断言过渡）/ `willRetry` / `totalEvents` / `totalTurns` / `error` /
  `errorType`）。

---

## 恢复语义（rebuildView）

```
rebuildView(store, sessionId):
    snap = store.loadLatestSnapshot(sessionId)        # 可选；未实现 → null
    if snap:
        view = deserializeView(snap.stateBlob)
        delta = store.readAfter(sessionId, snap.lastEventId)   # id > lastEventId
        return applyEvents(delta, view)               # O(delta)
    else:
        return reduceEvents(store.readBySession(sessionId))    # O(全量)
```

**一致性要求**：对任意事件序列与任意切点 k，
`reduceEvents(events)` 必须等于 `applyEvents(events[k:], reduceEvents(events[:k]))`。
即"快照(前缀) + 增量"== 全量回放。黄金用例用此断言（见 [golden](./golden/)）。

## 崩溃恢复编排（recoverSession）

1. `rebuildView` 得投影；无 `templateId` 或 session 不存在 → 报错。
2. 区分终态任务（`FINISHED` / `FAILED` / `CANCELED`）与 resumable；无 resumable → 报错。
3. `TaskManager.restore(allTasks, terminalIds)` 重建队列，返回需重启的 daemon。
4. 用投影里的 agent 视图预建 `preResolvedAgents`（保留 spawnDepth / parent）。
5. `setRunner` + `registerAndDrain` 续跑。

## 活跃 session 判定（listActiveSessionIds）

有 `SessionCreated` 但**无终态事件**的 session 视为活跃（需恢复）。
终态判定：`SessionFinished`，或 `SessionStatusChanged.new_status ∈ {SUCCEEDED, FAILED, CANCELED, INTERRUPTED}`。
