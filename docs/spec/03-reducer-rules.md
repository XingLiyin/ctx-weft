# 03 · Reducer 规则

> 真相源：`core/control/reducers.py`（`reduce_events` / `apply_events` / `_apply` / `_rebuild_agents`）

reducer 把事件序列折叠成 `RunStateView`。三份实现必须逐条对齐本表。

---

## RunStateView 字段

| 字段 | 类型 | 初值 |
|------|------|------|
| `runId` | string | 入参 |
| `sessionId` | string | `""`（首个事件填入） |
| `taskId` | string | `""`（首个带 taskId 的事件填入） |
| `agentId` | string | `""`（首个带 agentId 的事件填入） |
| `currentStep` | string \| null | null |
| `taskStatus` | string | `"UNKNOWN"` |
| `sessionStatus` | string | `"UNKNOWN"` |
| `assembledPromptTokens` | int | 0 |
| `transcriptTurns` | int | 0 |
| `eventsTotal` | int | 0（每事件 +1） |
| `sessions` | map<id, SessionView> | {} |
| `tasks` | map<id, TaskView> | {} |
| `agents` | map<id, AgentView> | {}（最后由 `_rebuild_agents` 推算） |

折叠流程：每个事件 `eventsTotal++`；首次填 `sessionId/taskId/agentId`；调 `apply`；末尾 `rebuildAgents`。

---

## apply 规则（按 event.type）

### Run / Step
| type | 变更 |
|------|------|
| `StepStarted` | `currentStep = payload.step_name` |
| `StepCompleted` | `currentStep = payload.next_step` |
| `RunStarted` | `taskStatus="ACTIVE"`；`sessionStatus="RUNNING"` |
| `RunFinished` | `sessionStatus = payload.final_status ?? "FINISHED"` |

### Session
| type | 变更 |
|------|------|
| `SessionCreated` | 建 `SessionView`（user_prompt/template_id/root_agent_id/llm_*/token_budget/tenant_id/created_at=ts，status=RUNNING）；`sessionStatus="RUNNING"` |
| `SessionResumed` | 该 session 的 `userPrompt`、`status="RUNNING"`；`sessionStatus="RUNNING"` |
| `SessionStatusChanged` | 若有 `new_status`：`sessionStatus` 与该 session.status 置之 |
| `SessionFinished` | `final_status ?? "SUCCEEDED"` 置 `sessionStatus` 与 session.status |
| `RecognizeIntentToolCall` | 若有 `session_goal`：置该 session.goal |
| `FailureThresholdHit` | 该 session.failureCounter += 1 |

### Task
| type | 变更 |
|------|------|
| `TaskCreated` | 建 `TaskView`（取 `payload.task`，id 缺省回落 `event.taskId`）；若 view.taskId 空则填入 |
| `TaskRequeued` | 该 task：`status="PENDING"`、`outputs=null`；若带 `user_prompt`/`original_user_prompt` 则一并恢复；`taskStatus="PENDING"` |
| `type ∈ TASK_STATUS_BY_EVENT`（且有 taskId） | 该 task.status = 映射值；`TaskStarted` 额外回填 `assigned_agent_id`；`taskStatus` 置之 |
| `TaskFinalized` | 该 task：`outputs=payload.outputs`、`error=payload.error`、`finishedAt=ts` |
| `RecognizeIntentToolCall`（且有 taskId） | 该 task：`title`/`description` 非空则更新（root task 创建时为空、recognize_intent 并发补填；空值不覆盖） |

### LLM / Context
| type | 变更 |
|------|------|
| `ReasonCompleted` | `assembledPromptTokens = payload.assembled_token_count ?? 0` |
| `ActTurnCompleted` | `transcriptTurns = payload.turn ?? transcriptTurns` |

> 其它所有事件类型在 reducer 中**无投影副作用**（含全部瞬态、Capability、HITL、Provider、Agent 子事件、
> token budget guard、compact 域内部事件等）。
>
> **注意几个易误解的 no-op**（黄金用例 12 / 13 锁定）：
> - `RunCanceled` 在 reducer 中**无副作用**——task 取消靠 `TaskCanceled`，session 取消靠 `SessionStatusChanged`。
>   （host 的 `ProjectionUpdater` 另会因 `RunCanceled` 把 session 置 CANCELED，但那是投影表通道，非本 reducer。）
> - `TokenBudgetWarning/Exceeded`、`CompactTriggered/Dispatched`、`MemoryCompacted` 等均不改投影；
>   compact 仅以一个 `CompactTaskSettings` 的普通 task 出现在投影中。

---

## TASK_STATUS_BY_EVENT（事件 → task 状态）

reducer、Postgres 投影、前端 SSE 翻译**三处共用**此映射：

| event.type | task.status |
|------------|-------------|
| `TaskStarted` | `ACTIVE` |
| `TaskSuspended` | `SUSPENDED` |
| `TaskFinished` | `FINISHED` |
| `TaskFailed` | `FAILED` |
| `TaskCanceled` | `CANCELED` |
| `TaskResumed` | `ACTIVE` |
| `TaskRequeued` | `PENDING` |

终态集合：`{ FINISHED, FAILED, CANCELED }`（用于 recover 判定 resumable）。

---

## _rebuild_agents（从 session/task 层级推算 agents）

1. 每个 session 的 `rootAgentId` → `AgentView(spawnDepth=0, parent=null)`。
2. 把 tasks 按 `createdAt` 升序遍历（父 task 在事件流中先于子 task）：
   - `aid = task.assignedAgentId`；为空或已存在则跳过。
   - 若 `task.settings.use_subagent` 为真且 `creator` 已在 agents 中：`depth = agents[creator].spawnDepth + 1`，parent = creator。
   - 否则 `depth = 0`，parent = creator 或 null。

---

## 序列化（快照用）

`serializeView` / `deserializeView` 必须可逆，覆盖：顶层标量 + `sessions`/`tasks`/`agents` 三个 map
的全部字段（含 `settings_raw`、`dag_deps`、`original_user_prompt`、`created_at`/`finished_at` 的 ISO 字符串）。
datetime 用 ISO8601 字符串往返。
