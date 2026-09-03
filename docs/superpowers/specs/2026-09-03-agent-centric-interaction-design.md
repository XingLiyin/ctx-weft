# Agent-Centric 交互机制设计

日期：2026-09-03
状态：待评审

## 1. 背景与目标

当前外部与 runtime 的交互统一通过 session 完成：`start_session` 的续跑分支永远把消息打给该 session 的 `root_agent_id`，`SessionManager` 承担一套由 `TaskManager` 聚合信号驱动的会话级运行状态机（`SESSION_RUNNING`/`SESSION_WAITING`/`SESSION_INTERRUPTED`/`SESSION_FINISHED`）。

数据地基其实已经部分具备 agent 粒度：`Agent.parent_agent_id`、`Task.assigned_agent_id`、`PendingHitl.agent_id`、`HitlRequestView.agent_id`、`set_agent_llm` 都已经是 agent 级别；执行引擎本身（`TaskManager.drain()`）也已经用 `asyncio.create_task` 并发调度不同 agent 的 task，只有"同一 agent 不并发"一条限制——`delegate_task`/`delegate_plan` 出去的子任务与父任务本来就是两条独立协程。

**目标**：把"外部交互的对象"从 session 改为 agent——支持一个 session 内同时/分别与多个 agent 对话；把"状态"这个概念整体从 session 挪到 agent 身上；`SessionManager` 降格为会话内 agent 的登记与主从关系维护者，只发容器级事件；`AgentRegistry` 升格为 agent 生命周期管理器，订阅 `TaskManager` 事件、记录并控制 agent 状态、对外发 agent 级事件。

**非目标**：不改变引擎已有的并发调度能力（已经支持不同 agent 并发跑），不改变 `TaskManager` 的 task 生命周期语义（只是消费方多了 `AgentLifecycleManager` 一个订阅者），不要求保留旧 API 的向后兼容（项目处于活跃开发阶段，允许破坏性修改）。

## 2. SessionManager 新职责

- 不再运行状态机，`session_state.py` 的 `next_transition` 及相关状态转移逻辑整体删除。
- 只处理两件事：① `SESSION_CREATED`/`SESSION_RESUMED` 容器生命周期；② 订阅既有的 `AGENT_INSTANTIATED`/`AGENT_SPAWNED` 事件，维护 `session_id → {agent_id}` 扁平成员集合（用于 tenant 归属和 `list_agents` 的 session 过滤）。**不建"父→子"索引**——级联操作（cancel/pause）需要的父子遍历放在 `AgentLifecycleManager` 里，避免两个组件各建一份可变的层级索引。
- `_SessionState` 精简为：`tenant_id: str` + `agent_ids: set[str]`，去掉 `status` 字段。
- 现有 `SESSION_RUNNING`/`SESSION_WAITING`/`SESSION_INTERRUPTED`/`SESSION_FINISHED` 四个事件类型整体废弃（从 `EventType` 枚举中移除）；`SESSION_CREATED`/`SESSION_RESUMED` 保留。会话本身不再有"运行态"这个维度——外部若想知道"这个 session 整体是不是都闲着"，自己聚合该 session 下所有 `AgentSummary.status` 即可，不由系统预先算好广播。

## 3. AgentRegistry → AgentLifecycleManager

### 3.1 状态机

五态：`idle`（初始态，也是每次交互处理完后回到的态）、`running`、`waiting_human`、`interrupted`、`terminated`（终态）。

```
idle --(收到消息/子任务结果)--> running --(处理完)--> idle
running --(触发 ask_user)--> waiting_human --(人工回复)--> running
running --(pause_agent)--> interrupted --(resume_agent)--> running
{任意态} --(cancel_agent，显式触发)--> terminated
```

关键性质：

- **`idle` 不等于"没有活着的 task"**。agent 的 task 是跨多轮持续存在的容器；只要 task 未到终态，`idle` 就是"当前轮次做完了、等下一个输入"的意思，不管输入是用户消息还是子任务完成结果。task 到达终态（`finished`/`failed`/`canceled`）后 agent 依然回到 `idle`（而不是终态），可以接收新消息并开启一个新 task——**`finished` 不是 agent 自己的终态，只是 task 的终态**。
- agent 自己的真正终态 `terminated` **仅由外部显式 `cancel_agent` 触发**，不会被任何 task 生命周期事件自动带入。`cancel_session` 广播到每个 agent，本质是对其下每个 agent 逐个调用 `cancel_agent`，因此不需要额外的"session 结束级联终止 agent"规则。

### 3.2 消费的 TaskManager 事件（既有事件，无需新增）

| 消费的事件 | 状态转移 |
|---|---|
| `TASK_STARTED` | → `running` |
| `TASK_AWAITING_HUMAN` | `running` → `waiting_human` |
| `TASK_HUMAN_RESOLVED` | `waiting_human` → `running` |
| `TASK_INTERRUPTED` | → `interrupted` |
| `TASK_RESUMED` | `interrupted` → `running` |
| `TASK_REQUEUED` | → `idle` |
| `TASK_SUSPENDED`（挂起等子任务完成，即 `suspended_on_children`） | → `idle` |
| `TASK_FINISHED` / `TASK_FAILED` / `TASK_CANCELED` / `TASK_FINALIZED` | → `idle`（**非终态**） |

`TaskManager` 本身不需要改动状态转移逻辑，只需要保证已经在发的这些事件里 `agent_id`（经 `Task.assigned_agent_id`）字段完整——这些字段已经存在。

### 3.3 对外发的新事件（新增 `EventType` 成员，离散风格，与现有 `TASK_*`/`SESSION_*` 粒度对称）

`AGENT_RUNNING` / `AGENT_WAITING_HUMAN` / `AGENT_INTERRUPTED` / `AGENT_IDLE` / `AGENT_TERMINATED`（带 `reason`，用于区分"直接被指定取消"还是"因祖先级联被取消"，如 `reason="cascaded_from:<ancestor_id>"`）。不采用单一 `AGENT_STATE_CHANGED` + old/new 字段的通用事件，理由是与仓库现有的 60 个离散事件类型风格保持一致，消费方（如 SSE 前端）不需要自己拆字段判断转移类型。

### 3.4 内部索引

- `parent_agent_id → {child_agent_id}`：订阅 `AGENT_SPAWNED` 时顺手维护，供 `list_agents(parent_agent_id=...)` 查询和 `cancel_agent`/`pause_agent`/`resume_agent` 的级联遍历使用。
- `agent_id → current_task_id`：每次消费到 `TASK_STARTED`/task 创建事件时更新，供消息路由判断"新建 task 还是复用现有 task"。
- `agent_id → status`：状态机当前值。

### 3.5 "控制"职责：同步守卫

暴露 `assert_can_receive(agent_id) -> None`（`running` 态直接抛错，其余态放行），供外部消息路由 API 在真正投递前调用。这个判断逻辑收敛在 `AgentLifecycleManager` 一处，不散落在 `runtime.py` 里。

## 4. 消息路由

### 4.1 `send_message`（新增，核心入口）

```python
send_message(agent_id: str, content: UserMessage, session_id: str | None = None) -> SendMessageResult
```

- 校验：agent 不存在 / `terminated` → 报错；`running` → 报错"agent 忙"（不排队，调用方自行重试）；`idle`/`waiting_human` → 放行。`session_id` 可选，仅用于提前校验"agent 不属于该 session"这类误用，不参与路由（`agent_id` 本身全局唯一，足以路由）。
- 路由决策（基于 `AgentLifecycleManager.agent_id → current_task_id`）：
  - `current_task_id` 对应 task 已终态 → 新建一个 `Task`（`assigned_agent_id` = 该 agent），走既有 `push_task` 路径。
  - `current_task_id` 对应 task 未终态（比如刚好在等子任务、或处理完一轮但整体还没结束）→ 复用现有的"按 task_id 注入对话轮次"机制（HITL 回复已经在用的 `UserTurnDelivery` 通路），挂到这个 task 上继续。

### 4.2 并发边界情况：用户消息 vs 子任务完成的竞争

若 agent 因 `delegate_task` 处于 `idle`（等子任务），此时外部消息恰好把它打成 `running`，而子任务几乎同时跑完：**子任务结果永远排队**，不会被拒绝（它不是外部主动发起的、无法要求它重试），等 agent 下一次回到 `idle` 时自动注入处理；**用户消息维持"忙碌直接报错、不排队"的规则**，两者不对称处理。

### 4.3 `reply_to_hitl` 微调

路由本身不变（`hitl_id` 全局唯一，天然定位到 `PendingHitl`）。`HitlReply` 新增必填 `agent_id` 字段，服务端收到后与 `PendingHitl.agent_id` 做一次校验，不一致直接拒绝——这是防呆机制而非路由必需：调用方必须显式声明"我以为在回复哪个 agent"，与系统记录不符时报错，而不是静默按 `hitl_id` 走掉。

### 4.4 `start_session` 的新形状

```python
start_session(params: SessionStartParams) -> SessionHandle  # {session_id, root_agent_id}
```

仍然一次性建好 session + root agent（与现有使用习惯一致）。若 `params` 带初始消息，内部直接复用 `send_message` 的注入逻辑打给 `root_agent_id`，不另建一套路径。

## 5. 发现 / 查询接口

```python
list_agents(session_id: str, *, parent_agent_id: str | None = None, include_terminated: bool = False) -> list[AgentSummary]
get_agent(agent_id: str) -> AgentDetail
```

- `AgentSummary`：`agent_id`、`parent_agent_id`、`status`、`current_task_id`、`spawn_depth`、`created_at`。不传 `parent_agent_id` 时返回该 session 全部 agent 的扁平列表（层级关系靠各条记录自己的 `parent_agent_id` 字段还原成树，接口不强行嵌套结构）；传了则只返回其直接子 agent。`include_terminated` 默认 `False`，避免列表随时间无限膨胀。
- `AgentDetail`：在 `AgentSummary` 基础上加 `template_id`/角色信息，以及 `current_task_id` 对应 task 的摘要（是否终态、是否 `waiting_human` 等），避免调用方再单独查一次 `TaskManager`。
- 实时发现：新 agent 出现时，既有的 `AGENT_INSTANTIATED`/`AGENT_SPAWNED` 事件本身已经带 `session_id`/`parent_agent_id`，通过既有事件流/SSE 订阅即可感知，无需新增"agent 加入 session"事件。状态变化通过 3.3 节的新 `AGENT_*` 事件推送。

## 6. `cancel_agent`：终止

```python
cancel_agent(agent_id: str, *, reason: str | None = None) -> None
```

1. 用 `AgentLifecycleManager` 的父→子索引递归展开该 agent 及全部子孙——**级联向下取消全部子孙**，避免孤儿 agent 永远挂着无人管。
2. 对展开出的每个 agent 按当前状态处理：
   - `running`：复用 `TaskManager` 现有的单任务取消机制，作用于其 `current_task_id`，不新造通路。
   - `waiting_human`：先调 `HitlService.cancel()` 终局对应的未决请求——与现有"用户取消会话时一并终局未决 ask_user"同一模式。
   - `idle`：无需额外动作，直接终态化。
3. 每个被处理到的 agent 转 `terminated`，发 `AGENT_TERMINATED`（`reason` 区分直接指定 vs 级联）。
4. 子孙之间的取消互相独立，可并发处理，不需要顺序等待。

## 7. `pause_agent` / `resume_agent`：可恢复的打断

```python
pause_agent(agent_id: str, *, reason: str | None = None) -> None
resume_agent(agent_id: str) -> None
```

- `pause_agent` 只对 `running` 生效；作用于非 `running` 的 agent 直接报错"当前不在运行，无需暂停"。内部复用现有 `TaskManager` interrupt 机制（`TASK_INTERRUPTED` 已在 3.2 节映射到 `running → interrupted`，`AgentLifecycleManager` 不需要新逻辑，纯粹是外部入口 + 级联包装）。**级联暂停所有当前 `running` 的子孙**，与 `cancel_agent` 同样的级联范围，避免父 agent 冻住了但子 agent 继续消耗资源跑下去。
- `resume_agent` 对称地复用现有 `TASK_RESUMED`（已映射 `interrupted → running`），**级联恢复所有当前处于 `interrupted` 的子孙**——不区分这些子孙是否被同一次 `pause_agent` 调用带下去的，只要现在是 `interrupted` 就一并恢复，与 pause 的级联对象保持对称。
- 两者本质都是给 `TaskManager` 已有的单任务级中断/恢复机制包一层 agent 粒度的级联外壳，不重新发明底层执行语义。

## 8. Session 级 API 的定位

`pause_session`/`cancel_session`/`set_session_llm` 保留，作为广播便捷入口：对该 session 下（`SessionManager` 维护的成员集合里）每个 agent 逐个调用对应的 agent 级接口（`pause_agent`/`cancel_agent`/`set_agent_llm`）。不新增独立的 session 级执行逻辑。

## 9. LLM 事件收敛

这是与 agent 中心化并行的一项独立收敛，因同样改动事件类型清单而并入本 spec。

### 9.1 问题

各 step 自己维护了一套 LLM 事件的镜像，覆盖程度参差不齐：

- `recognize_intent` 只发 `RECOGNIZE_INTENT_LLM_PROMPT`（recognize_intent.py:151），**完全不发通用 LLM_\***；且走裸 `stream_llm`（:167），连自愈退避带来的 `LLM_RETRY_TRIGGERED` 也没有。
- `background_observe` 走 `run_observe_react`，靠 `ReactEventTypes`（observe.py:44-67）把事件类型参数化，发 `BACKGROUND_OBSERVE_*` 四种。与 observe 走的 LLM_* 组是**同一段代码、同一份 payload 构造**，仅事件类型不同。
- `compact.py` 的 `summarize_for_compact`（:69-126）调 LLM 但**一个事件都不发**，是唯一完全静默的调用方。
- `LLM_*` 由调用方自己 emit（act.py:218/226/251/256/287、observe.py 的 `run_observe_react`），`llm_gateway.py` 全文只发 `LLM_RETRY_TRIGGERED`（`_emit_retry`@457）。

结果：LLM 调用的可见性取决于调用方是否记得发事件，5 个调用方有 5 种不同程度的覆盖。

### 9.2 方案

**发射点收敛到 gateway。** `stream_llm_resilient`（llm_gateway.py:477）签名扩展为接收 caller，由 gateway 统一 emit 全部 6 种 LLM_* 事件，调用方不再自行发射。这样「调用方」字段天然正确且不可遗漏。

**删除 5 个镜像事件**：`RECOGNIZE_INTENT_LLM_PROMPT`、`BACKGROUND_OBSERVE_REQUEST_STARTED`、`BACKGROUND_OBSERVE_PROMPT_SENT`、`BACKGROUND_OBSERVE_TOKEN_STREAMED`、`BACKGROUND_OBSERVE_RESPONSE_FINISHED`。

**`ReactEventTypes` 整层删除**：连同 `OBSERVE_REACT_EVENTS` / `BACKGROUND_OBSERVE_REACT_EVENTS` 两个常量与 `run_observe_react` 的 `event_types` 形参一并移除，内部硬编码 LLM_*。该间接层存在的唯一目的就是区分这两组事件，目的消失则层消失。

**caller 字段**：全部 6 种 LLM_* 的 payload 新增 `caller`，取值复用 `Purpose`（protocols/capability.py:31），并补第 5 个值 `"background_observe"`——现状 background_observe.py:271 已经在传这个 Literal 之外的值，补齐后类型才自洽。与装配层 `ContextRequest.purpose` 天然对齐，不引入第二套词汇。

调用方全集（core 下共 5 处，均经 gateway）：`act`（act.py:240）、`observe`（observe.py:135）、`background_observe`（复用 `run_observe_react`，background_observe.py:282）、`compact`（compact.py:105）、`recognize_intent`（recognize_intent.py:167）。`finalize.py` / `prepare.py` / `segment_fold.py` / `reconcile.py` 不调 LLM。

### 9.3 顺带修复的三个问题

1. **compact 不再静默**——走统一 gateway 后自动获得全部 LLM_* 事件。
2. **recognize_intent 获得自愈退避**——从裸 `stream_llm` 切到 `stream_llm_resilient`，顺带修掉它缺退避重试的现状。
3. **多模态脱敏统一**——recognize_intent 现用 `content_to_text`（:153），act / `run_observe_react` 用 `redact_content_for_event`（act.py:225、observe.py:124）。收敛后统一到 `redact_content_for_event`。

### 9.4 必须一并处理的字段冲突

`turn` vs `round`：**同一个 `LLM_PROMPT_SENT` 事件类型，act.py 发的带 `turn`，`run_observe_react` 发的带 `round`**——这是现存的不一致，收敛到单一发射点后必须统一。取 `turn`。

### 9.5 信息不丢失的保证

- **background_observe 的 4 个事件：无损。** payload 逐字节等价，唯一丢失的「这是后台调用」一位信息由 `caller="background_observe"` 补回。
- **recognize_intent：净增。** 删除镜像事件的同时它开始发通用 LLM_*，可见性从「只有 prompt」提升到完整 6 种。

`request_id` 的前缀约定（`req_` / `obs_` / `bgobs_`）原本是识别调用方的非正式手段，有了显式 `caller` 后不再承担该职责，可保留作可读性。compact 与 recognize_intent 现无 request_id，收敛后由 gateway 统一生成。

## 10. 事件类型变更汇总

**移除（共 11 种）**：

- session 运行态 6 种：`SESSION_RUNNING`、`SESSION_WAITING`、`SESSION_INTERRUPTED`、`SESSION_FINISHED`、legacy `SESSION_STATUS_CHANGED`、legacy `SESSION_PAUSED_HITL`（后两个原本就标注为 legacy/待退役）。
- LLM 镜像 5 种（见 §9）：`RECOGNIZE_INTENT_LLM_PROMPT`、`BACKGROUND_OBSERVE_REQUEST_STARTED`、`BACKGROUND_OBSERVE_PROMPT_SENT`、`BACKGROUND_OBSERVE_TOKEN_STREAMED`、`BACKGROUND_OBSERVE_RESPONSE_FINISHED`。BackgroundObserve 这个域整体消失（4 个事件全是 LLM 镜像）。

**新增**：`AGENT_RUNNING`、`AGENT_WAITING_HUMAN`、`AGENT_INTERRUPTED`、`AGENT_IDLE`、`AGENT_TERMINATED`。

**payload 变更**：全部 6 种 `LLM_*` 新增 `caller` 字段；`LLM_PROMPT_SENT` 等事件的 `round` 统一为 `turn`。

**保留不变**：`SESSION_CREATED`、`SESSION_RESUMED`、全部 `TASK_*`、`AGENT_INSTANTIATED`/`AGENT_SPAWNED`/`AGENT_LLM_CHANGED`/`SPAWN_REJECTED`、全部 HITL 相关事件、`TASK_RECAP_*`、其余域事件。`RECOGNIZE_INTENT_*` 除被删的那一个外全部保留。

## 11. 组件职责对照表（变更前后）

| 组件 | 变更前 | 变更后 |
|---|---|---|
| `SessionManager` | 会话运行状态机（订阅 TM 4 条聚合信号），持有 `status` | 会话内 agent 登记表（订阅 `AGENT_INSTANTIATED`/`AGENT_SPAWNED`），持有 `agent_ids` 集合，不再有 `status` |
| `AgentRegistry` | 被动登记表，无状态字段，session→agent/parent→child 查询靠全表扫 | `AgentLifecycleManager`：订阅 TM 事件维护 `status`/`current_task_id`，维护 `parent→children` 索引，暴露 `assert_can_receive` 守卫，发 `AGENT_*` 事件 |
| `TaskManager` | 持有 `_running_agents`/`busy_agents`（"忙闲"这个概念错位地记在这里） | 不变，继续是 task/队列状态唯一住所；`AgentLifecycleManager` 只是新增的订阅者，不需要 TM 反过来感知它 |
| `HitlRegistry`/`HitlService` | HITL 未决状态唯一住所，`PendingHitl` 已含三级 id | 不变，`cancel_agent` 复用其 `cancel()` |
| `EventBus` / `EventStore` | **协议留给 host 实现**（`protocols/events.py`），内置实现在 `providers/` 下 | 不变。注意「同步 drain」是内置 `InProcessEventBus` 的特性而非契约保证，见 §13 |

## 12. 破坏性变更范围（明确接受，不做兼容 shim）

- `SessionStartParams`/`start_session` 返回值形状变化（新增 `root_agent_id`）。
- `HitlReply` 新增必填 `agent_id`。
- `EventType` 枚举移除 11 个类型（6 个 session 运行态 + 5 个 LLM 镜像）、新增 5 个 agent 状态类型。
- `stream_llm_resilient` 签名扩展为接收 caller；`Purpose` Literal 补 `"background_observe"`；`ReactEventTypes` 及其两个常量、`run_observe_react` 的 `event_types` 形参整体删除。
- 依赖 `BACKGROUND_OBSERVE_*` 或 `RECOGNIZE_INTENT_LLM_PROMPT` 做前端渲染分流的 host 侧消费方，需改为按 `LLM_*` 的 `caller` 字段分流。
- 原本按 session 续跑打到 root agent 的隐式路径被 `send_message(agent_id, ...)` 显式替代。
- 依赖 `SessionManager` 状态字段的既有测试/消费方需要同步改为读取 `AgentLifecycleManager`/`AgentSummary.status`。

## 13. 已知留待实现阶段处理的细节（非架构性，不阻塞本设计）

- `RunStateView`/`control/reducers.py` 的 `_apply` 需要扩展以折叠新的 `AGENT_*` 事件，使冷启动重建（`rebuild_view`）后的视图与运行时内存态一致。
- `AgentLifecycleManager` 的内部索引（`parent→children`、`current_task_id`）在多 session 场景下的具体数据结构（分 session 分片 vs 全局字典 + session 过滤）留给实现阶段按现有 `AgentRegistry` 已有模式决定。
- 现有测试（`test_runtime_hitl_wiring.py`、`test_runtime_pause_wiring.py` 等）需要的改动范围，留给实现阶段的 writing-plans 环节梳理。
- `TASK_QUEUE_BLOCKED`/`TASK_QUEUE_INTERRUPTED`/`TASK_QUEUE_DRAINED` 原本是 `SessionManager` 唯一的输入。SM 不再消费后，建议保留发射作为外部可观测信号，但需确认没有其他消费者依赖它们。
- `reducers._apply` 除了新增 5 个 `AGENT_*` 的折叠分支，还需处理被移除的 11 个类型：旧事件日志重放要保留读取兼容（与 legacy HITL 6 种事件同样的处理方式），不能直接删分支。
- `RECOGNIZE_INTENT_COMPLETED` 的 `usage` 字段在 recognize_intent 开始发 `LLM_RESPONSE_FINISHED` 后成为冗余（后者已带 usage）。可选清理，本设计不强制。
- **异步 bus 下 `AgentLifecycleManager` 的状态是最终一致的。** `EventBus` 由 host 实现，「同步 drain」只是内置 `InProcessEventBus`（bus.py:45-81）的特性，不是契约保证。若 host 换用异步/远程 bus（README 明说可换 Redis Streams），ALM 的 status 更新相对 `TaskManager` 的内存态转移存在延迟，`assert_can_receive` 可能读到陈旧状态而放行本该拒绝的消息。需确认该守卫是否需要额外的同步保证——`HitlService` 已有先例：它刻意不靠订阅驱动续跑，而用 `reply_to_hitl` 返回值里的 `claimed` 分流，正是为了避开这类背压问题。
