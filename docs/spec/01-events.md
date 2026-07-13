# 01 · 事件类型与结构

> 真相源：`src/ctx_weft/core/events/types.py`

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
`SessionCreated` `SessionResumed` `SessionStatusChanged` `SessionFinished` `SessionPausedHitl`
`RunStarted` `RunPaused` `RunResumed` `RunCanceled` `RunFinished`
`StepStarted` `StepCompleted` `StepFailed`

### Task
`TaskCreated` `TaskStarted` `TaskSuspended` `TaskResumed` `TaskFinished` `TaskFailed`
`TaskCanceled` `TaskFinalized` `TaskRequeued` `BlackboardPublished`

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
`HitlRequired` `HitlApproved` `HitlAnswered` `HitlRejected` `HitlModified` `HitlTimeout` `HitlCancelled`

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
