# 01 · 事件类型与结构

> 真相源：`src/ctx_weft/protocols/events.py`

---

## Event 结构

所有事件共享同一结构；`payload` 按 `type` 而异。

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | `evt_<ULID>`，时间可排序、全局唯一 |
| `runId` | string \| null | 一次 loop run 的标识；某些 session 级事件为 null |
| `sequence` | int | 同一 `runId` 内单调递增（来自 `LoopState.sequenceCounter`）；**允许有间隙** |
| `sessionId` | string | 必填 |
| `type` | string | 必须 ∈ 下方 `EVENT_TYPES` |
| `timestamp` | datetime(UTC) | |
| `tenantId` | string | 默认 `"default"` |
| `taskId` | string \| null | |
| `agentId` | string \| null | |
| `payload` | object | 按 type 而异 |
| `metadata` | object | |
| `causationId` | string \| null | 因果链：哪个事件导致了它 |
| `schemaVersion` | int | 默认 1 |

> **`sequence` 允许间隙**：瞬态事件（见下）消费 sequence 但不持久化，故持久化流的 sequence 不连续。
> 排序一律以 `id`（ULID）为准，不依赖 sequence 连续。

## EVENT_TYPES（V1 冻结清单）

未登记类型不得发出（`makeEvent` 对未登记类型直接报错）。按域分组：

### Session / Run / Step
`SessionCreated` `SessionResumed` `SessionFinished`
`SessionInterrupted` `SessionWaiting` `SessionRunning`
`RunStarted` `RunPaused` `RunResumed` `RunCanceled` `RunFinished` `RunInterrupted`
`StepStarted` `StepCompleted` `StepFailed`

> **会话状态只由 `SessionManager` 写，且只经这四条**：`SessionInterrupted`（停着且异常，
> 等 `/resume`）/ `SessionWaiting`（停着但正常，都在等人）/ `SessionRunning`（重新开跑）/
> `SessionFinished`（终态）。它们说「会话到了什么状态」，不带命令语气。
>
> **L 档（只读存量，不得再发射）**：`SessionStatusChanged` `SessionPausedHitl`。
> 前者是事实流里唯一的命令式事件（「把状态写成 X」），6 个发射点混着三类不同的东西，
> 已按上面四条拆解；后者的两档（`PAUSED` / `PAUSED_HITL`）在新值域里合并成单一
> `WAITING`。**reducer 的读分支保留**——存量日志还要回放。详见 `docs/events-v2.md` §5
> 与 `docs/upgrade/2026-09-02-session-status-ownership.md`。
>
> `RunInterrupted{reason, error_code?, error_message?}`：**这次执行**被外部打断。
> **只由 `runtime._run_loop` 发**，两支各发一次、无条件（run 死了就是死了）；
> 它**不写 task 状态**——task 停在哪由 task 域的 `TaskInterrupted` 说。
> 两支的 payload 不同形：run 崩溃带 `{reason:"run_crash", error_code, error_message}`；
> LLM outage 只带 `{reason:"llm_outage", error_message}`。
> **`reason` 只作溯源，判据是事件类型本身。**
>
> `RunFinished{outcome, final_status?, will_retry, total_events, total_turns, error?,
> error_type?}`：一次 run 执行结束，**无论成败必发**——host 靠它关 SSE。`outcome`
> 是权威字段（`RunOutcomeKind` 五值：`completed` / `awaiting_human` /
> `suspended_on_children` / `interrupted` / `canceled`），说的是**这次执行自己**
> 怎么收场，不是 task 状态。`final_status`（装 `task.status`）**已废弃**，保留一个
> 发布周期供旧断言过渡，下个周期删除（2026-09-02 task-status-ownership 重构 Task 3/4）。

### Task
`TaskCreated` `TaskStarted` `TaskSuspended` `TaskAwaitingHuman` `TaskInterrupted`
`TaskResumed` `TaskFinished`
`TaskFailed` `TaskCanceled` `TaskFinalized` `TaskRequeued` `BlackboardPublished`

> `TaskSuspended` 从三义收窄到**一义**：只表示「等子任务完成」（payload 带
> `summary` / `spawn_titles`）。另两义各自成型：等人 → `TaskAwaitingHuman{hitl_id}`
> （task → `AWAITING_HUMAN`），被打断 →
> `TaskInterrupted{reason, error_code, error_message, retry_count}`（task → `INTERRUPTED`）。
> **判据是类型，不是 payload 里的 `reason` 字面量。**
>
> `TaskInterrupted` 发在**重试判定之后**：崩溃后还能原地重试的那一支发的是
> `TaskRequeued`（task → `PENDING`），不是这条。run 层同时发的 `RunInterrupted`
> 说的是另一件事（那次执行死了），不写 task 状态。

### TaskManager 信号（会话状态机的输入）
`TaskQueueBlocked` `TaskQueueInterrupted` `TaskQueueDrained`

> TM 报「队列此刻是什么形状」，`SessionManager` 据此推会话状态。三条都是 O 档：
> reducer 不折叠它们（会话状态由 `Session*` 承载），host **不必**订阅；但它们比会话
> 状态事件更早到达，想做「这一轮跑完了」的提前提示，订 `TaskQueueDrained` 最准。
> payload：`{count}` / `{reason}` / `{final_status}`。

### Agent
`AgentInstantiated` `AgentSpawned` `AgentStatusChanged` `AgentWaiting` `AgentFinalized` `SpawnRejected`

### Context
`ReasonCompleted` `ContextTokensEstimated` `ContextTokensMeasured` `ContextAssembled` `ContextOverflowed`

### LLM
`LLMRequestStarted` `LLMPromptSent` `LLMTokenStreamed` `LLMReasoningStreamed`
`LLMResponseFinished` `LLMRetryTriggered`

### Capability
`CapabilityInvoked` `CapabilityProgress` `CapabilityFinished` `CapabilityFailed` `CapabilityCanceled`

### ActStep / ObserveStep
`ActTurnStarted` `ActTurnCompleted` `MaxTurnsReached` `ObserveCompleted`

### Memory
`MemoryIngested` `CompactTriggered` `CompactDispatched` `MemoryCompactStarted`
`MemoryCompacted` `MemoryCompactFailedFallback` `BlackboardSubscribed`

### HITL
`HitlRequired` `HitlApproved` `HitlAnswered` `HitlRejected` `HitlModified` `HitlTimeout` `HitlCancelled` `HitlOpened` `HitlResolved`

> `HitlOpened` 取代 `HitlRequired` + `SessionPausedHitl`；`HitlResolved` 取代 5 个 resolve 事件（`HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` / `HitlCancelled`）。

### Guard
`TokenBudgetWarning` `TokenBudgetExceeded` `FailureThresholdHit` `MaxConcurrentAgentsExceeded`

### Provider
`MCPServerDisconnected` `MCPServerReconnected` `RemoteSkillSyncCompleted` `RemoteSkillSyncFailed`

### MetadataFiller
`MetadataFillerStarted` `MetadataFillerLLMPrompt` `MetadataFillerCompleted`
`MetadataFillerToolCall` `MetadataFillerSkipped`

### System / 元事件
`EventsDropped` `SnapshotCreated`

---

## 瞬态事件（TRANSIENT_EVENT_TYPES）

高频流式 delta，**仅供实时订阅（SSE）消费，不进任何持久化 / 投影 / 快照路径**。
单一真相由 `LLMResponseFinished`（含完整文本）承载，reducer 不消费这些 delta。

```
TRANSIENT_EVENT_TYPES = { "LLMTokenStreamed", "LLMReasoningStreamed" }
```

三处必须统一引用此集合并跳过：
- EventStore.append（含内存实现）
- 持久化订阅者（EventPersister）
- 投影订阅者（ProjectionUpdater）；快照计数（SnapshotWriter）亦不计入

> SSE 帧另有一套"不持久化 delta"集合（host 侧 `_NO_PERSIST = {text_delta, reasoning_delta}`），
> 与本集合概念一致但作用于 SSE 通道，二者独立维护。

---

## MemoryEventType（记忆事件，独立于 EVENT_TYPES）

core 在关键节点自动 ingest 的记忆事件类型（小写字符串）：

`user_prompt` `llm_response` `tool_invocation` `tool_result`
`observer_summary` `compact_summary` `blackboard_publish`

> 这是 `MemoryProvider.ingest` 的入参类型，**不走 EventBus**，与上面的 `EVENT_TYPES` 是两套体系。
> 写入时机见 ARCHITECTURE.md §记忆写入时机。
