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
| `RunStarted` | `taskStatus="ACTIVE"`——**不写 `sessionStatus`**：run 是任务级的，会话状态归 `Session*` 事件（2026-09-02 会话状态所有权重构） |
| `RunFinished` | **无投影副作用**——run 级记账，同上不写 `sessionStatus` |

### Session
| type | 变更 |
|------|------|
| `SessionCreated` | 建 `SessionView`（`user_prompt`（jsonable）/`template_id`/`root_agent_id`/`llm_model`/`llm_account`/`tenant_id`（缺省回落 `event.tenantId`）/`token_budget`/`context_limit`/`reserved_output_tokens`，`created_at=ts`，`status="RUNNING"`）；`sessionStatus="RUNNING"` |
| `SessionResumed` | 该 session 的 `userPrompt`、`status="RUNNING"`；`sessionStatus="RUNNING"` |
| `SessionFinished` **L 档** | `final_status ?? "SUCCEEDED"` 置 `sessionStatus` 与 session.status。已停发，分支保留只为读存量日志 |
| `SessionStatusChanged` **L 档** | 只读存量。若有 `new_status`：`sessionStatus` 与该 session.status 置之。**新流量里不再发出**，但分支必须保留——存量日志靠它才能重建 |
| `SessionPausedHitl` **L 档** | 只读存量。置 `"WAITING"`，**不读 `form`**：旧模型按 form 分 `PAUSED` / `PAUSED_HITL` 两档，新值域里两档合并成单一 `WAITING` |
| `RecognizeIntentToolCall` | 若有 `session_goal`：置该 session.goal |
| `FailureThresholdHit` | **无投影副作用**——它是聚合播报。计数由 `TaskFailed` / `TaskFinished` 折叠（见 Task 段） |

> **新流量里已经没有会话状态事件了。** 2026-09-02 的所有权重构曾把写者收敛成
> `SessionInterrupted` / `SessionWaiting` / `SessionRunning` / `SessionFinished` 四条；
> 前三条于 2026-09-03 停发、2026-09-05 **连枚举一并删除**（`docs/events-v2.md` §5.8
> ——它们生于 09-02 死于 09-03，全程在 `master` 之后的分支内部，`master` 从来没有过
> 这三个类型，存量流里也不会有），`SessionFinished` 停发但留在 L 档。
> 现在写 `sessionStatus` 的只剩 `SessionCreated` / `SessionResumed` 写下的起点 `RUNNING`，
> 外加两条 L 档 setter 用于重放存量日志。会话整体状况由 host 聚合各 agent 的
> `AGENT_*` 状态推导。
>
> 表中凡置 `sessionStatus` 的规则，都**同时**写 run 级标量 `sessionStatus` 与
> `sessions[sessionId].status` 两处（实现里的 `_set_session_status`）。只写一处
> 就是「投影和视图对不上」那类 bug。
>
> `SessionStatus` 值域共 6 个：`RUNNING` / `WAITING` / `INTERRUPTED` /
> `SUCCEEDED` / `FAILED` / `CANCELED`。回放存量日志时旧值 `PAUSED` / `PAUSED_HITL`
> 一律折进 `WAITING`（`QUEUED` / `TIMEOUT` 从未被写出过）。
>
> **历史注（裁定 R3）**：`SessionWaiting` 当初刻意不带「有几个在等」的计数，那个数字
> 停在 `TaskQueueBlocked{count}` 那一层——展示数据不该穿过状态判据。两个类型现均已删除；
> 想显示计数的 host 直接数未决 HITL / `AWAITING_HUMAN` 的 task。
>
> **已过期（2026-09-03 agent-centric 改造）**：`session_state.py` 与它的
> `next_transition` 状态机整体已删；`SessionRegistry`（原 `SessionManager`）现在
> 只做「登记这个 session 里有哪些 agent」，不再 `handle_event` 推导/改写会话状态，
> 也不再发 `SessionFinished`（已转入 L 档，reducer 的这条分支只为重放存量日志保留，
> 不会再被新事件触发；同批的另三条已于 2026-09-05 连枚举删除）。`sessions[sessionId].status` 字段对该改造之后创建的 session 会永远停在
> 初始值——host 若要判断会话整体状况，改聚合各 agent 的状态
> （`CtxWeftRuntime.list_agents`）。详见
> `docs/upgrade/2026-09-03-agent-centric-interaction.md` 第 5 节。

### Task
| type | 变更 |
|------|------|
| `TaskCreated` | 建 `TaskView`（取 `payload.task`，id 缺省回落 `event.taskId`）；若 view.taskId 空则填入 |
| `TaskRequeued` | 该 task：`status="PENDING"`、`outputs=null`；若带 `user_prompt`/`original_user_prompt` 则一并恢复；`taskStatus="PENDING"` |
| `type ∈ TASK_STATUS_BY_EVENT`（且有 taskId） | 该 task.status = 映射值；`TaskStarted` 额外回填 `assigned_agent_id`；`taskStatus` 置之。另折叠 `session.failureCounter`：`TaskFailed` +1（`error_code="TASK_FAILED_BY_THRESHOLD"` 不计）、`TaskFinished` 清零 |
| `TaskFinalized` | 该 task：`outputs=payload.outputs`、`error=payload.error`、`finishedAt=ts` |
| `RecognizeIntentToolCall`（且有 taskId） | 该 task：`title`/`description` 非空则更新（root task 创建时为空、recognize_intent 并发补填；空值不覆盖） |

### Agent
| type | 变更 |
|------|------|
| `AgentInstantiated`（且有 agentId） | 若 `payload.template_id` 非空：置该 agent 槽的 `templateId`。**事件流里唯一记录「该 agent 用哪个模板」的地方**——树形推算（`_rebuild_agents`）得不出模板，授权按模板做策略，漏了则子 agent 会顶着 root 的模板身份 |

### HITL（L 档，只读存量）
| type | 变更 |
|------|------|
| `HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` / `HitlCancelled` | **仅当** `sessionStatus == "WAITING"` 时置回 `"RUNNING"`（不覆盖已到的终态）。五条都不再发射，分支保留只为读存量日志 |

> `HitlOpened` / `HitlResolved`（新模型的两条）**对投影无副作用**——新流量里会话状态不再
> 由任何会话级事件承载（host 按 agent 聚合推导），pending 列表的真相源是 `HitlRegistry`，
> **不在 `RunStateView` 里另存一份**（spec/07 §7）。

### LLM / Context
| type | 变更 |
|------|------|
| `PrepareCompleted` | `assembledPromptTokens = payload.assembled_token_count ?? 0` |
| `ActTurnCompleted` | `transcriptTurns = payload.turn ?? transcriptTurns` |

> **除上表列出的之外**，其它所有事件类型在 reducer 中**无投影副作用**（含全部瞬态、
> Capability、Provider、其余 Agent 子事件、token budget guard、compact 域内部事件等）。
> 注意 HITL 与 Agent 两域**各有例外**，见上面两张表——旧版本的本文档在这里笼统写了
> 「含全部 HITL / Agent 子事件」，是错的。
>
> **注意几个易误解的 no-op**（黄金用例 12 / 13 锁定）：
> - `RunCanceled` 在 reducer 中**无副作用**——task 取消靠 `TaskCanceled`，会话被取消要看
>   `SessionFinished{final_status:"CANCELED"}`（`cancel_all` 自 2026-09-02 起发这一条；
>   从前发的通用 setter `SessionStatusChanged` 已停发）。
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
| `TaskAwaitingHuman` | `AWAITING_HUMAN` |
| `TaskInterrupted` | `INTERRUPTED` |
| `TaskFinished` | `FINISHED` |
| `TaskFailed` | `FAILED` |
| `TaskCanceled` | `CANCELED` |
| `TaskResumed` | `ACTIVE` |
| `TaskRequeued` | `PENDING` |

终态集合：`{ FINISHED, FAILED, CANCELED }`（用于 recover 判定 resumable）。
非终态的三种「停」各有各的类型：`SUSPENDED`（等子任务）/ `AWAITING_HUMAN`（等人）/
`INTERRUPTED`（被外部打断，等 `/resume`）。

> **这九条现在只从 `TaskManager` 发出**（task 状态所有权重构，2026-09-02）：loop
> （`runtime.py` / `finalize.py` / `suspend.py` / `observe.py` / `control_capability.py`）
> 交出 `RunOutcome`（发生了什么），`TaskManager.apply_run_outcome` 据 `disposition_for`
> 那张纯函数表写状态并发这张表里唯一的那条事件——loop 侧不再直接写 `task.status`、
> 不再直接发任何一条 task 状态事件（`tests/unit/test_task_manager_owns_status.py` 按
> AST 扫两条守卫钉住）。这些事件因此不带 `run_id`（`TaskManager` 不属于任何一次
> run，写 `None`）；发射相对 `RunFinished` 的顺序也从「run 内部先发」变成「run 返回
> 之后 `TaskManager` 才发」——host 若靠收到 `RunFinished` 就关 SSE 流，会漏掉紧随其
> 后的 task 事件（见 `docs/upgrade/2026-09-02-task-status-ownership.md`）。

> **`TaskSuspended` 从三义收窄到一义**（2026-09-02）：它现在**只**表示「等子任务完成」。
> 从前用 `TaskSuspended{reason:"hitl_park"}` / `{reason:"run_crash"}` 表达的另两义，
> 各自成了独立类型 `TaskAwaitingHuman{hitl_id}` / `TaskInterrupted{reason, …}`。
> **判据是事件类型，不是 payload 里的 `reason` 字面量。** 三处共用此映射，
> 漏这两行就是三处一起把「等人」和「被打断」的 task 认成 `UNKNOWN`。
>
> **`RunInterrupted` 不在这张表里**（2026-09-02 收尾修正）：它是 run 域的事实
> （「这次执行死了」），reducer 对它**无副作用**。task 停在 `INTERRUPTED` 由
> `TaskInterrupted` 写，且那条只在「挂起等 `/resume`」时发——崩溃后还能重试的
> task 收到的是 `TaskRequeued`（→ `PENDING`）。**把 `RunInterrupted` 留在表里，
> 走重试的 task 会先被打成 `INTERRUPTED` 再翻回 `PENDING`，中间那一下是假的。**

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
