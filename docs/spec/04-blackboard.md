# 04 · Blackboard / Topic 语义

> 真相源：`ARCHITECTURE.md §Blackboard`、`providers/memory/in_memory/provider.py`、
> `core/loop/driver._ensure_blackboard_subscriptions`、`assembler/sources/blackboard.py`

**核心认知**：没有独立的 blackboard 存储。blackboard / 短期记忆 / 长期记忆是同一个 `MemoryProvider`
的不同用法。所谓 blackboard = 带 `topic` 标签的 memory 事件 + 订阅。

---

## 数据模型

- **MemoryEvent**：`type / scope / content / timestamp / role / topic / metadata`。
- **Subscription**：`sessionId / taskId / topic / cursor / intent`（`taskId=""` 表示 session 级）。
- **intent**：
  - `subtask` —— 自己派生的子任务结果，可 review / reopen
  - `predecessor` —— 同 plan 前序结果，只读
  - `long_term_background` —— → system
  - `long_term_project_log` —— → messages

## 两条索引轴

每条 ingest 同时拿两个序号：

| 轴 | key | 序号 | 用途 |
|----|-----|------|------|
| scope 轴 | `tenant\|session\|agent`（**忽略 task_id**） | `seqNo` | `recallRecent`——agent 历史 |
| topic 轴 | `topic` 字符串（全局，跨 scope/session） | `topicSeqNo` | `recallTopic`——跨 task 通信 |

## 三种召回

- `recallRecent(scope, types, limit)`：按 scope key + 类型，`seqNo` 倒序，**跳过 superseded**。
- `recallTopic(topic, since)`：按 topic + `topicSeqNo > since`，正序，返回 `(records, newCursor)`，**跳过 superseded**。
- `recallSemantic`：V1 返空。

## 发布（唯一发布点：FinalizeStep，仅 task success）

```
ingest(MemoryEvent(
  type = blackboard_publish,
  topic = task.id,
  content = outputs + process_report,
  metadata = { task_id, title, outcome, parent_task_id }))
```

**覆盖语义**：写入 `blackboard_publish` 时，把同 `topic` 之前未 superseded 的 `blackboard_publish`
标记为 superseded —— 同一 task 的结果 topic **只保留最新一条**，reopen 重跑后自动覆盖旧结果。

随后另发一个 EventBus 的 `BlackboardPublished`（投影/SSE 用，非 memory）。

## 订阅（subscribeTopic，幂等）

`subscribeTopic(sessionId, topic, intent, taskId="")`：键 `(sessionId, taskId, topic)`，
**幂等**（已存在则保留 cursor）。

在 driver 起步钩子里按需建立（持有 memory + task_manager），每次 run 幂等执行：
- **前序订阅**：`task.trackingTaskIds`（同 plan 全部前序）→ `intent=predecessor`，`taskId=task.id`。
- **子任务订阅**：`taskManager.childrenOf(task.id)` → `intent=subtask`，`taskId=task.id`。
- 同一 topic 同时是前序又是子任务 → 按前序（只读）处理，避免重复订阅。

## 消费与渲染

`BlackboardSource.fetch`：`listSubscriptions(sessionId, taskId=本task)`（只取本 task + session 级订阅）
→ 逐个 `recallTopic(topic, since=cursor)` → 产出 ContextBlock，按 intent 落位：
`subtask`/`predecessor`/`project_log` → messages，`background` → system。

Composer 在 observe 阶段按 intent **分段渲染**（每行 `- {title} [{outcome}]: {content}`，标题取自 metadata）：
- `subtask` → **"Your sub-task results (you may confirm / reopen these)"**
- `predecessor` → **"Upstream task results (read-only context)"**

## Review / Reopen 权限与级联

observe 的 `submit_task_assessment` 可携带 `task_reviews`：

- **权限范围**：只能 review 当前 task 自己派生的子任务（`childrenOf(self)`）。越权 → 拒绝并把越权标题反馈给 LLM。
- **级联 reopen**：reopen 一个 plan 步骤触发 `reopenChain(head, reason)`——head + 其同 plan 后续
  （`trackingTaskIds` 含 head、FINISHED、按 `createdAt` 排序）一并重排，重建 `blockedBy` 链，
  使每个后续等前驱重跑完（重新 publish 覆盖旧结果）后再跑。
- **prompt 改写**：head 用 `## Revision required\n{reason}`；后续用 `## Upstream task revised`。
  改写均基于 `originalUserPrompt`，重复 reopen 不累积。
- 当前任务（parent）不是其子任务的后续，**不会被卷入级联**。

## 与 OBSERVER_SUMMARY 通道的关系

父 agent 另有一条独立通道看到子结果：finalize 把 `observer_summary` 写进**父 scope**
（`agentId=creatorAgentId`），父 agent 用 `recallRecent`（scope key 忽略 task_id）即可召回。
blackboard 的 topic 通道是"按需、按标题、可覆盖"的补充，二者并存。

## Provider 对齐要点（三份实现 + 各 backend 都须一致）

- `subscribeTopic` 幂等 + `taskId` 维度
- `ingest` 对 `blackboard_publish` 的 topic 覆盖
- `recallTopic` / `recallRecent` 跳过 superseded
- `_toRecord` 透传 metadata
- 持久化 backend（如 postgres）的订阅表需含 `task_id` 列
