# 06 · 分层 Memory / 上下文装配 / 两类 Compact

> **状态：Python（`ctx-weft` + host）已落地（§11 step 1-6 全完成，124 core + 21 host 测试绿）；
> TS `ctx_weft-ts` / Java `loomej` 待对齐。** 本文是该机制的唯一真相源；与各端现状冲突之处以本文为准。
>
> 已知遗留（Python）：`reason._check_should_compact` 的 token-ratio 触发仍按整 prompt 估算、
> 会派发 agent compact（任务层膨胀时由 active 路径的 task compact 兜底，非 bug，待精化）。
>
> 取代/修改的现状：
> - `MemoryScope` 的 scope key 由 `tenant|session|agent` 改为**按层**（见下），[04-blackboard](./04-blackboard.md)
>   "两条索引轴"表中"scope 轴忽略 task_id"一行随之失效。
> - [04 §与 OBSERVER_SUMMARY 通道的关系] 描述的"父 scope OBSERVER_SUMMARY 旁路报告"**被本文的
>   `TASK_DISPATCH_RESULT` 配对回填取代**。blackboard 的 topic 通道（`recall_topic` / 订阅 / predecessor）保留。
> - observe 的 outcome 集合由 `success|failed|active|needs_user_input` 改为 `active|retry|success|fail|ask_human`。
>
> 真相源（待改造）：`protocols/memory.py`、`providers/memory_blackboard/in_memory.py`、
> `host/providers/memory/postgres.py`、`core/assembler/sources/*`、`core/loop/steps/{reason,act,observe,finalize,compact}.py`、
> `core/loop/capability_gateway.py`、`core/orchestrator/control_capability.py`。

---

## 1. 核心认知

Memory 分**三层**，按"谁能召回、装什么"严格隔离；上下文由两条 source 重建并按时间归并。

```
session  ── 命名空间（key 前缀，无独立事件），区分 agent 归属
  ├── task 层    一次 task 的私有执行对话（推理 + 真实能力工具）
  └── agent 层   该 agent 的派发日志（submit_task 调用 + child 结果），跨 task 持久
```

**可见性铁律**：task 层对该 task 的执行者私有；**跨 task 只以 `submit_task → output+report` 黑盒暴露**，
child 的执行细节（推理 / 真实工具调用）永不进入 parent 的上下文。

## 2. 分层模型

| 层 | scope key | 装什么 | 召回者 |
|----|-----------|--------|--------|
| session | `tenant\|session` | 仅命名空间（blackboard topic 另走 topic 轴，见 04） | — |
| task | `tenant\|session\|task\|<task_id>` | `USER_PROMPT` `LLM_RESPONSE` `TOOL_INVOCATION` `TOOL_RESULT` `TASK_COMPACT_SUMMARY` | 仅执行该 task 的 agent |
| agent | `tenant\|session\|agent\|<agent_id>` | `TASK_DISPATCH` `TASK_DISPATCH_RESULT` `AGENT_COMPACT_SUMMARY` | 该 agent（跨其所有 task） |

`seq_no` **每层各自**单调递增（按 scope key 计数）。跨层排序用 `timestamp`（见 §5）。

## 3. 事件类型与层映射

| 事件类型 | 层 | 写入点 | 关键 metadata |
|---------|----|--------|--------------|
| `USER_PROMPT` | task | driver 起步 / suspend / **retry 追加** | task_id |
| `LLM_RESPONSE` | task | ActStep 每轮 | turn, **tool_calls=[{id,name,input}]**（无损重建用）, usage |
| `TOOL_INVOCATION` | task | gateway（**仅真实能力工具**） | tool_call_id(=tc.id), tool_name |
| `TOOL_RESULT` | task | gateway（**仅真实能力工具**） | tool_call_id(=tc.id), tool_name, is_error |
| `TASK_COMPACT_SUMMARY` | task | task compact（observe active） | keep_last, archived_count |
| `TASK_DISPATCH` | agent | gateway（**submit_task/submit_plan/replan**），result 暂挂 | tool_call_id, title, prompt, child_task_id |
| `TASK_DISPATCH_RESULT` | agent | **child finalize 写入 parent agent 层** | tool_call_id(=origin), child_task_id, outcome |
| `AGENT_COMPACT_SUMMARY` | agent | agent compact（CompactStep） | keep_last_pairs, archived_pairs |
| `BLACKBOARD_PUBLISH` | session/topic | finalize success（见 04，保留） | task_id, title, outcome |

> `MemoryEventType.OBSERVER_SUMMARY`（现状）与现状 `COMPACT_SUMMARY` 被上表取代/拆分；
> `layer_for_types(types)` 要求一次召回的 types 同层，混层抛错。

**层由事件类型唯一决定**（`EVENT_LAYER: dict[type, layer]`），`ingest` 用它选 scope key 与 seq 分区。
唯一的"按工具名分流"在 gateway：dispatch 控制工具 → `TASK_DISPATCH`（agent 层）；其余工具 → `TOOL_INVOCATION/RESULT`（task 层）。

## 4. 上下文装配：两条 source

任意 agent 装配 = **task_conversation**（当前 task 层）+ **agent_experience**（agent 层）按 `timestamp` inline 归并。
二者都产出**结构化 LLMMessage 回合**（非扁平 text block）。

### 4.1 task_conversation（当前 task 层 → 自己的执行对话）

召回 task 层 `[USER_PROMPT, LLM_RESPONSE, TOOL_RESULT, TASK_COMPACT_SUMMARY]`（`TOOL_INVOCATION` 仅审计，重建跳过），按 `seq_no` 升序重建：

| 事件 | → 回合 |
|------|--------|
| `USER_PROMPT` | `user(content)` |
| `LLM_RESPONSE` | `assistant(content=text, tool_calls=metadata.tool_calls)` |
| `TOOL_RESULT` | `tool(content, tool_call_id=metadata.tool_call_id)` |
| `TASK_COMPACT_SUMMARY` | 折叠边界的 `assistant("[Context so far] …")` |

### 4.2 agent_experience（agent 层 → 派发日志，零合成）

agent 层每个派发都是**真实**事件对，重建为 `submit_task` 配对回合：

| 事件 | → 回合 |
|------|--------|
| `TASK_DISPATCH` | `assistant(tool_calls=[submit_task/submit_plan(id=tool_call_id, title, prompt)])` |
| `TASK_DISPATCH_RESULT` | `tool(content=output+"\n\nProcess Report: "+report, tool_call_id)` |
| `AGENT_COMPACT_SUMMARY` | `assistant("[既往派发摘要] …")` |

- **零合成**：agent 对一个 task 有经验 ⟺ 是它**派发**的（真实 `submit_task`）。它被指派、亲手执行的
  task 是它的**当前 task_conversation**（做完即归档），并出现在**派发它的 parent** 的派发日志里，不在自己这里。
- `submit_task` 的 tool_call 落 **agent 层**（持久成经验），故当前任务期间发起的派发也按 timestamp
  穿插进当前 task 对话；真实能力工具落 task 层。

### 4.3 配对与隐去

- `TASK_DISPATCH` 与 `TASK_DISPATCH_RESULT` 按 `tool_call_id` 配对。
- **未配对的 `TASK_DISPATCH`（child 未回填）→ 整条 tool_call 隐去不渲染**，避免悬空 tool_call。
  （`submit_*` 终结本轮并 SUSPEND，parent 待全部子任务完成才恢复，故恢复时必已配对。）

### 4.4 不变式

> **当前任务** = `[user]` prompt + 自己执行的 task_conversation。
> **其他一切任务**（既往派发 + 当前派发）= `submit_task(tool_call) → tool_result(output+report)` 配对。
> child 执行细节永不外泄；全部按 timestamp inline。

## 5. 委派生命周期（submit_task / submit_plan）

```
1. parent actor 调 submit_task/submit_plan
   gateway 透传当前 tc.id → 写 TASK_DISPATCH(tool_call_id=tc.id, title, prompt, child_task_id)
                            到 parent 的 agent 层；不写即时 tool result（暂挂）
   child 任务带 origin_tool_call_id = tc.id 开启；parent SUSPENDED
2. child 跑自己的 task 层对话（黑盒）
3. child finalize（success/fail）：写 TASK_DISPATCH_RESULT(tool_call_id=origin, content=output+report)
   到 **parent 的 agent 层**
4. parent 恢复 → agent_experience 配对出 submit_task → tool_result
```

- `Task` 模型加字段 `origin_tool_call_id`（plan 每个 child 各一）。
- `ControlContext` 透传当前 `tool_call_id`（gateway 已计划透传 `tc.id`）。
- 取代现状"finalize 写 parent scope 的 OBSERVER_SUMMARY 旁路报告"。

## 6. observe 五态机

ActStep 退出后按 `act_exit_reason` 门控 observe 裁决：

| act_exit_reason | 允许 outcome | 动作 |
|-----------------|-------------|------|
| `max_turns` / `context_limit`（机械退出） | **`active`** | LLM/control 产出**详细执行记录** → **task compact** |
| `normal` / `actor_done`（纯文本退出） | `success` / `retry` / `fail` / `ask_human` | 见下 |

finalize 按 outcome 分派：

| outcome | process report | 终态 | memory 动作 |
|---------|---------------|------|-----------|
| `active` | 执行记录（= verdict.summary） | requeue PENDING | `apply_compact(task层)` 折叠覆盖；**不写 agent 层** |
| `retry` | 不足分析 + Next Step Hint | requeue PENDING | **不注入 user message**（原始任务消息一开始就在）；observe 分析作为 `process_report` → 下一轮 `## Current Progress`。`retry_count≥max_retries` → 降级 `fail` |
| `success` | 成功经验 + 重要过程 | FINISHED | 父若存在：`TASK_DISPATCH_RESULT` 回填 parent agent 层；blackboard publish（04） |
| `fail` | 无法完成原因 | FAILED（不重试） | 父若存在：`TASK_DISPATCH_RESULT(outcome=fail)` 回填 parent agent 层 |
| `ask_human` | 问题 | requeue PENDING | observe 内 park HITL 取回回复 → **直接结束 observe**；回复作为 `USER_PROMPT` 追加回 task 层，task 继续重入 act loop |

> **`ask_human` 流程**：observe 的 LLM 调 `submit_task_assessment(ask_human)` → 该调用内同步 park HITL
> 并取回用户回复 → observe **立即返回**（不进入下一轮 observe）→ finalize 把回复作为 user message 追加进
> task 层并 requeue PENDING → 回到 act loop。
>
> 现状 `submit_task_assessment` 的 `task_status` 枚举与护栏（`success && !outputs → active`）随之改写：
> 该护栏改为 **→ `retry`**（追加要求补终稿的 user message），不再走 compact。

**`active` vs `retry` 的本质对比**：`active` = 添加总结、**覆盖**之前的 task memory；`retry` = **不动 task
memory**，仅以 observe 分析作为 `process_report`（→ Current Progress）后重试。

## 7. 两类 Compact

两类 compact 各管一层，触发分工避免错配。

| | task compact | agent compact |
|---|---|---|
| 作用层 | task 层 | agent 层（派发日志） |
| 本质 | = observe（`active` 路径） | = 现有 CompactStep |
| 触发 | act 内**机械退出**（max_turns/context_limit）反应式 | ReasonStep 按 **agent 层增长**主动式（actor 跑前先压） |
| 摘要来源 | 复用 verdict.summary | 专门 LLM 总结 |
| 产物 | `TASK_COMPACT_SUMMARY`（→ `[Context so far]`） | `AGENT_COMPACT_SUMMARY`（→ `[既往派发摘要]`） |

**触发分工要点**：装配 prompt = 两层之和，token 压力可能来自任一层。
- agent 层膨胀（派发太多）→ ReasonStep 在 actor 跑前主动派 agent compact。
- task 层膨胀（当前任务回合太多）→ act 内 `context_limit_hit` / `max_turns` 触发 task compact。

**折叠约束**：
- **agent compact 必须成对折叠**：`TASK_DISPATCH` + 对应 `TASK_DISPATCH_RESULT` 一起折进摘要，
  **绝不留悬空 tool_call**；`apply_compact(AGENT)` 的 `keep_last` 按**完整派发对**计。
- task compact 只动 task 层，不碰穿插其间的派发对（在 agent 层）。

## 8. Provider 接口变更

```
enum MemoryLayer { TASK, AGENT, SESSION }
EVENT_LAYER: dict[MemoryEventType, MemoryLayer]   # §3
layer_for_types(types) -> MemoryLayer             # 同层校验，混层抛错

scope_key(scope, tenant, layer):
  TASK   -> f"{tenant}|{scope.session_id}|task|{scope.task_id}"
  AGENT  -> f"{tenant}|{scope.session_id}|agent|{scope.agent_id}"
  SESSION-> f"{tenant}|{scope.session_id}"
```

- `ingest`：`layer = EVENT_LAYER[event.type]`；seq_no 按该 layer 的 scope key 分区。
- `recall_recent(scope, types, limit)`：`layer = layer_for_types(types)`，按该层 scope key 过滤。
- `count_recent` 同上。
- `apply_compact(scope, layer, summary, keep_last)`：**新增 `layer` 参数**；TASK 写 `TASK_COMPACT_SUMMARY`、
  AGENT 写 `AGENT_COMPACT_SUMMARY`（成对 keep_last）。
- `recall_topic` / `subscribe_topic` / blackboard 不变（04）。

## 9. DB 迁移（postgres backend）

```sql
ALTER TABLE memory_events ADD COLUMN task_id VARCHAR(64);
ALTER TABLE memory_events ADD COLUMN layer   VARCHAR(16) NOT NULL DEFAULT 'task';
-- 历史回填：观察摘要类 → agent 层
UPDATE memory_events SET layer='agent' WHERE type IN ('observer_summary','compact_summary');
UPDATE memory_events SET type='agent_compact_summary' WHERE type='compact_summary';
CREATE INDEX ix_memory_task  ON memory_events(session_id, layer, task_id,  type);
CREATE INDEX ix_memory_agent ON memory_events(session_id, layer, agent_id, type);
DROP INDEX ix_memory_scope_type;
```

> 顺带修：现状 `postgres.apply_compact` 给 `MemoryEventModel(...)` 传了不存在的 `tenant_id`/`task_id`
> kwargs（潜在 TypeError），加列后修复。旧 task 层转录无 `task_id` 只能留空，不影响新逻辑。

## 10. 参考：装配出的 context（核对样例）

**① 编排者 R**（既往真实派发 `p0=整理大纲`，当前 `T0`，派发 `c2/c3`）：

```text
[assistant] tool_calls=[ submit_task(id=p0,"整理大纲") ]            # 既往派发(agent层)
[tool p0]   "<output>\n\nProcess Report: <报告>"
[user]      ## Current Task: T0 ## 研究 X 并写摘要                  # 当前任务(task层)
[assistant] "先搜索 X。" tool_calls=[ search(id=c1) ]               # 自己真实工具(task层)
[tool c1]   "<搜索结果>"
[assistant] tool_calls=[ submit_task(c2,"收集"), submit_task(c3,"初稿") ]  # 当前派发(agent层)
[tool c2]   "<T1 output+report>"
[tool c3]   "<T2 output+report>"
```

**② 叶子 A1**（执行 `T1`，既往派发 `q0=预研`）：A1 只见自己 task 层 + 自己 agent 经验，看不到 R / 兄弟 T2。

**③ task compact 后**：task 层早期真实工具回合 → `[Context so far]`，保留最近 K 回合；派发对不受影响。

**④ retry 后**：task 对话全保留，末尾多一条 `[user]` report+hint。

**⑤ 两 compact 都生效**：agent 层老派发对 → `[既往派发摘要]` + 近期派发对结构化保留；task 层 → `[Context so far]` + 最近 K；两层按 timestamp 交织。

## 11. 分阶段实现顺序 + 测试

1. **协议层**：`MemoryLayer` / `EVENT_LAYER` / `layer_for_types` / 新事件类型 / `apply_compact(layer)`。
2. **两 provider**（in_memory + postgres）+ DB 迁移：layer 化 ingest/recall/count/compact；单测绿。
   - 用例：task A/B 转录隔离；agent 层跨 task 累积；`apply_compact(TASK)` 不动 agent 层；
     `apply_compact(AGENT)` 成对折叠；`layer_for_types` 混层抛错。
3. **observe 五态 + finalize 分派 + control_capability** 枚举/护栏改写。
4. **委派回填**：`Task.origin_tool_call_id`、gateway 透传 `tc.id`、dispatch 工具 → `TASK_DISPATCH`、
   child finalize → `TASK_DISPATCH_RESULT`。
5. **装配两 source**：`task_conversation`（无损重建）+ `agent_experience`（配对回合）+ `ContextBlock`
   结构化扩展（携带 `tool_calls` / `tool_call_id`）+ timestamp 归并 + 未配对隐去。
6. **两类 compact 触发**：reason 按 agent 层派 agent compact；observe/finalize 走 task compact。
7. **TS / Java 三端对齐** + 黄金用例（`事件序列 → 期望 messages 重建`）。

## 12. 待定/风险

- `_seed_child_memory`（runtime）现复制 parent 混层事件给 child；新模型下 child 是黑盒、自带独立
  task 层，应**取消跨 task 转录复制**；child 起步上下文仅靠其 user_prompt + 自身 agent 经验。
- blackboard topic 通道（04 predecessor/订阅）与 agent 层派发日志并存，二者不重叠（topic=跨 plan 只读前序；
  agent 层=自己派发的子任务），实现时勿混。
- `ContextBlock` → `prompt.messages` 的结构化回合支持是唯一需要动装配器核心的点，先确认转换点。
