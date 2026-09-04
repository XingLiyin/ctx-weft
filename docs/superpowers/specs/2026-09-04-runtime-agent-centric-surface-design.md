# Runtime 对外面向 agent-centric 对齐 设计

> 前置：`docs/superpowers/specs/2026-09-03-agent-centric-interaction-design.md`（下称「09-03 spec」）。
> 事件体系权威文件：`docs/events-v2.md`。

## 1. 背景与目标

09-03 那次改造把**状态与控制**搬到了 agent 侧：`AgentLifecycleManager` 持五态机与
`parent→children` 索引、5 个 `AGENT_*` 事件、`send_message` / `cancel_agent` /
`pause_agent` / `resume_agent` / `list_agents` / `get_agent` 六个 agent 级入口落地。

但 `CtxWeftRuntime` 的**对外面**只是「新增」了这一套，旧的 session / task 级接口原样留着。
结果是两套并存且互不自洽：

- session 级 API 没有按 09-03 spec §8 退化成广播壳，`pause_session` 甚至与 `pause_agent` 语义相反。
- `send_message` 返回裸 `task_id`，`start_session` 返回 `RunHandle`，同一族入口两种形状。
- `list_agents` 强制按 session 分片，而 `send_message` 的 docstring 明说 `agent_id` 全局唯一、
  `session_id` 不参与路由。
- host 无法只订阅一个 agent 的事件流（`EventFilter` 没有 `agent_id`），也无法只查一个 agent
  的未决 HITL（`list_pending_hitl` 只有 session 维度）。
- 冷启动后 `AgentLifecycleManager` 不被装填，整个 agent 面是瞎的。
- 会话状态机已删，但 `session_status_after_recover` 与向已不再订阅的 `SessionRegistry`
  空播 `TASK_QUEUE_*` 的代理逻辑仍留在对外面上。

**目标**：把对外面收敛到一套。**agent 是唯一寻址单位**；**session 降为成员登记表、事件存储的
分区键与资源边界**；**task 与 run 退回引擎内部**。

**判据三条**：

1. 每一个对外命令的主键是 `agent_id`。`session_id` 只出现在两处：登记表操作（`start_session`
   等）与可选过滤参数。
2. host 能只订阅一个 agent 的事件流、只查一个 agent 的未决 HITL、冷启动后立刻看见全部 agent。
3. 会话级 API 保留，但**执行部分**一律建在 agent 级原语之上，不再直接拍 run token 或改状态。

**兼容策略**：不留 shim，沿用 09-03 spec §12 前例。`src` 与 `tests` 一次改到位。

---

## 2. 目标接口全表

| 现在 | 之后 | 变更性质 |
|---|---|---|
| `start_session(params) -> RunHandle` | `-> TurnHandle` | 返回类型 |
| `send_message(agent_id, content, *, session_id=None) -> str` | `-> TurnHandle` | 返回类型 |
| `run_single_task(...) -> tuple[RunHandle, LoopState]` | `-> tuple[TurnHandle, LoopState]` | 返回类型 |
| `cancel_agent` / `pause_agent` / `resume_agent` / `set_agent_llm` | 不变 | — |
| `compact_session(session_id, *, agent_id=None, task_id="")` | `compact_agent(agent_id, *, task_id="") -> CompactReceipt` | 主键翻转 + 类型化 |
| `pause_session(session_id) -> bool` | 同名同语义，执行部分重建 | 见 §7 |
| `cancel_session(session_id) -> bool` | 同名同语义，执行部分重建 | 见 §7 |
| `set_session_llm(session_id, ...) -> int` | 不变 | — |
| `list_agents(session_id, *, parent_agent_id, include_terminated)` | `list_agents(*, session_id=None, parent_agent_id=None, include_terminated=False)` | session_id 降为可选过滤 |
| `get_agent(agent_id) -> AgentDetail` | 同名，补 `created_at` | 见 §5 |
| `list_pending_hitl(session_id=None)` | `list_pending_hitl(*, session_id=None, agent_id=None)` | 加 agent 过滤 |
| `reply_to_hitl(reply)` | 不变 | — |
| `recover_session(session_id, ...)` | `recover_agent(agent_id, ...)` | 换轴，见 §6 |
| `recover() -> int` | 同名，返回 agent 数、装填 ALM | 见 §6 |
| `rebuild_hitl(session_id)` / `rebuild_all_pending_hitl()` | 不变 | — |
| — | `rebuild_agent(agent_id) -> bool` / `rebuild_all_agents() -> int` | 新增 |
| `session_status_after_recover(session_id) -> str` | **删除** | 见 §6 |
| `pause_task(session_id, task_id) -> bool` | `_pause_task(session_id, task_id)` | 内部化 |
| `event_bus` 属性 | 不变 | — |

**`run_single_task` 保留**：它是 60 处测试在用的单任务便利入口，返回的句柄已带 `agent_id`，
与 agent-centric 不冲突。删它是纯成本、零架构收益。本设计只改它的返回类型与 docstring
（从「Phase 1 compat」改为「单任务测试便利入口」）。

---

## 3. 句柄：`RunHandle` → `TurnHandle`

### 3.1 形状

```python
@dataclass
class TurnHandle:
    session_id: str
    agent_id: str
    task_id: str
    template_id: str
    event_bus: EventBus
    _state: LoopState | None = None

    async def events(self) -> AsyncIterator[Event]: ...
    async def wait_for_finish(self, timeout: float = 300.0) -> LoopState | None: ...
```

四个身份字段**恒非空**。`events()` 按 `EventFilter(agent_id=..., task_id=...)` 订阅
（`agent_id` 维度由 §5 补上）。`wait_for_finish()` 等该 task 的终态事件
——`TaskFinished` / `TaskFailed` / `TaskCanceled` / `TaskFinalized`——而不是 `RunFinished`。

### 3.2 为什么 `run_id` 不在句柄上

`run` 是引擎内部**一轮循环**的相关性 id。句柄的职责是「指着一个外部可寻址的对象」，
agent + task 已经足够定位；`events()` 与 `wait_for_finish()` 都不需要 run_id。host 若要按轮
聚合，每条事件的信封里都带 `run_id`，直接读即可。

把 run_id 放进句柄还会引入一个无法诚实填写的字段：`send_message` 有三条路径，其中
「注入且不重排」（`_suspended_on_live_children`：agent 因 `delegate_task` 处于 `idle`、
当前 task `SUSPENDED` 且仍有未终态子任务）在返回的那一刻**确实还没有新一轮**，它要等
`_try_resume_parent` 在子任务收尾时唤醒。为了填这个字段就得引入「等待派发的 Future +
超时兜底」一整套机制，而该机制的唯一服务对象是一个没有消费者的字段。去掉字段，三条路径
一律立即返回一个诚实的句柄。

### 3.3 `send_message` 三条路径的返回值

| 路径 | 条件 | `task_id` 取值 |
|---|---|---|
| 新建 task | `current_task_id` 已终态或为空 | 新建 task 的 id |
| 注入 + 重排 | 未终态，且非 `_suspended_on_live_children` | 该活 task 的 id |
| 注入不重排 | `_suspended_on_live_children` | 该活 task 的 id |

三条都返回 `TurnHandle`，`agent_id` 恒为被寻址的那个 agent。调用方要判断「消息是开了新一轮
还是并进了现有一轮」，比对 `handle.task_id` 与调用前 `get_agent(agent_id).current_task_id` 即可，
不需要额外的返回字段。

---

## 4. `run_id` 语义：一轮一个

### 4.1 现状与问题

`start_session`（`runtime.py:1187`）预铸一个 `run_id`，作为 `_SessionTaskRunner._default_run_id`
传下去；`assemble` 的非 subagent 分支（`:3237`）把它给**每一条**任务用。而每次 `_execute_task`
都新建 `LoopState`，`sequence_counter` 从 0 起（`driver.py:79`）。

后果：一个 owner-TM 下的所有根 scope 轮次共用一个 run_id、各自从 0 编号，
`(run_id, sequence)` 跨轮撞号，一个 run_id 下出现多对 `RunStarted`/`RunFinished`。

### 4.2 方案

**删除 `_default_run_id`**。`_SessionTaskRunner` 不再持有它，构造参数一并去掉。`run_id` 只在
`assemble` 里铸造（`AgentBinding.run_id = generate_id("run")`），一轮一个，两个分支无例外。
`start_session` 不再预铸 run_id。

由此立起两条不变式：

1. `(run_id, sequence)` 全局唯一。
2. 每个出现在事件里的 `run_id` 恰有一对 `RunStarted` / `RunFinished`。

### 4.3 一并修掉的三处违反（原批次二 Task 5 的 A4 / C5）

- `background_observe` 用 `dataclasses.replace(state)` 做快照：`run_id` 相同但
  `sequence_counter` 是独立副本，与主 run 重号。改为快照另起 `run_id`，
  并补 `RunStarted`/`RunFinished` 配对。
- `recognize_intent`（`recognize_intent.py:44`）自造 run_id 无起止 → 补配对。
- `compact_agent`（原 `compact_session`）已有配对，随主键翻转一并核实保持。

> **与批次二的关系**：`docs/superpowers/plans/2026-09-03-outstanding-issues-batch2.md`
> 的 Task 5 覆盖同一批修复。本设计**吃掉**该 Task；实施时须在批次二文档中把 Task 5 标记为
> 「已并入 2026-09-04 计划」，避免两个进程重复改同一批文件。

---

## 5. agent 维度的读模型

### 5.1 事件订阅

`EventFilter` 新增 `agent_id: str | None = None`：

```python
@dataclass
class EventFilter:
    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    types: list[str] | None = None
```

`Event` 信封本来就带 `agent_id`，过滤实现只是多一个字段比较。内置 `InProcessEventBus` 同步跟上。
host 自实现的 bus 需同步支持——列入破坏性变更清单（§10）。

**协议层是正确的住所**：在 runtime 包一个 `stream_agent()` 便利方法会把过滤推到消费端，
大会话下白跑大量事件；而信封里已有该字段，bus 侧过滤是零额外语义的改动。

### 5.2 HITL 查询

- `HitlRequestView` 新增 `delivery` 字段，如实暴露 `PendingHitl.delivery`。
- `list_pending_hitl(*, session_id=None, agent_id=None)`：两个都是可选过滤，可叠加。
  底层 `HitlRegistry.list_pending` 同步支持 `agent_id`。

### 5.3 消除对 ALM 私有字段的穿透

`runtime` 现有五处直接读 `AgentLifecycleManager._agents`（`list_agents` / `get_agent` /
`send_message` / `pause_agent` / `_start_task_for_agent`）。ALM 补一个只读访问器：

```python
def record_of(self, agent_id: str) -> AgentRecordView | None: ...
```

`AgentRecordView` 是 `_AgentRecord` 的只读投影（`agent_id` / `parent_agent_id` / `status` /
`current_task_id` / `spawn_depth` / `session_id` / `template_id` / `tenant_id` / `created_at`）。
五处穿透全部换掉。

### 5.4 补齐 `created_at`

`AgentSummary.created_at` 与 `AgentDetail.created_at` 已在 `protocols/agent.py` 声明，
但 `list_agents` / `get_agent` 从不填，恒为 `None`。本次填上（数据源为 `_AgentRecord`）。

---

## 6. 恢复：对外以 agent 为轴

### 6.1 换轴的边界

`EventStore` 协议**一个字不动**。它按 session 分区（`read_by_session` /
`list_active_session_ids` / `read_after` / 快照）是存储事实，且事件里 `agent_id` 可空
（`SessionCreated` 等就没有），按 agent 读会漏。

因此换的是**语义层**：session 降为「从哪读」的分区键与资源边界，不再是恢复的语义单位。
per-session 串行锁与 owner-TM 复用保留为实现细节。

### 6.2 入口变更

- `recover_session(session_id, *, user_reply, resumed_task_id, hitl_id)`
  → `recover_agent(agent_id, *, user_reply, resumed_task_id, hitl_id)`。
  `reply_to_hitl` 内部改调它（`PendingHitl.agent_id` 现成可用）。
  内部照旧按 `record_of(agent_id).session_id` 取 per-session 锁与 owner TM。
- `recover() -> int`：**返回恢复的 agent 数**（原来是 session 数）。每个 session 装填后
  逐个 agent 报告。
- 新增 `rebuild_agent(agent_id) -> bool` 与 `rebuild_all_agents() -> int`，与
  `rebuild_hitl` / `rebuild_all_pending_hitl` 一一对称。

**`rebuild_hitl(session_id)` 保持 session 主键**，不换轴：它做的是「扫这个存储分区、把折出来
的未决集合喂进内存」，主键就是分区键，与 §6.1 的定位一致；而且它一次装填的是该分区下**全部
agent** 的未决请求，按 agent 拆反而要么重复扫日志、要么装不全。查询侧的 agent 维度由
`list_pending_hitl(agent_id=...)`（§5.2）提供——**装填按分区，查询按 agent**，两件事。

### 6.3 冷启动装填

`recover()` 现在每个 session 只做 `rebuild_hitl` + `SessionRegistry.register_session`，
**从不调 `AgentLifecycleManager.load()`**（全仓仅 `recover_session` 一处调）。后果是重启后
`list_agents` 返回空、`get_agent` 抛 `AgentNotFound`、`send_message` / `cancel_agent` /
`pause_agent` 全部失败，直到某条 HITL 冷应答或 `/resume` 恰好走过那条路。

修法：`recover()` 每个 session 增加一步 `ALM.load(...)`，与 `rebuild_hitl` 并列。
`rebuild_agent` / `rebuild_all_agents` 提供启动后的按需自愈。

### 6.4 恢复期信号：`TASK_QUEUE_*` 停发，改发 `AGENT_*`

三个 `TASK_QUEUE_*`（`TaskQueueBlocked` / `TaskQueueInterrupted` / `TaskQueueDrained`）
在 core 里**已无消费者**：`SessionRegistry` 自 09-03 起只订阅 `AGENT_INSTANTIATED` /
`AGENT_SPAWNED`（`session_registry.py:98-104`）。两个发射点
（`orchestrator/task/manager.py:1047-1052` 的 `announce_queue_state`、
`runtime.py:2601` 的 `_announce_queue_state_as_tm_proxy`）一并停发，进 L 档。
枚举值与 `reducers._apply` 分支按 `events-v2.md` §5 全部保留。

`_announce_queue_state_as_tm_proxy` 整个删除。它的存在理由是「代 TaskManager 给
SessionRegistry 发那一条它唯一的输入」，而那个消费者已经不存在。

**替代物**：ALM 装填完成后，按折出来的现状为每个 agent 发对应的 `AGENT_WAITING_HUMAN` /
`AGENT_IDLE` / `AGENT_INTERRUPTED`。这本就是 agent 该承担的信号，host 投影因此不会停在
崩溃前的状态。「恢复不是一种状态」的纪律保持——发的是**折出来的现状**，不是新状态，
不引入 `RECOVERING` 之类的值域。

### 6.5 删除 `session_status_after_recover`

它返回的 `"PAUSED"` / `"PAUSED_HITL"` 不是任何一个状态机的值域，算的是「未决 HITL 的
delivery 性质」。会话状态机已删，这个 session 级派生串没有存在理由。
`HitlRequestView.delivery`（§5.2）一暴露，host 直接按原始事实判即可。
`_derive_paused_status` 与 `paused_status_for` 的调用点随之清理。

---

## 7. 会话级 API：语义保留，执行部分重建

09-03 spec §8 原话是「session 级 API 对每个 agent 逐个调用对应的 agent 级接口，不新增独立的
session 级执行逻辑」。实测这句话对 `pause_session` 不成立且不应成立——**本设计修订它**。

### 7.1 `pause_session`

它的语义是**会话级软打断**（对应 UI 上的「停止」）：弃掉全部排队任务、取消除 root 之外的
在途 run、只留 root agent 当前那一轮作为唯一续跑点。这与 `pause_agent`（级联暂停、
不杀任何东西、非 running 静默跳过）语义相反，**不是它的广播**。

保留语义，重建执行部分：

- 排队：`abandon_pending(reason=PAUSE_ABANDON, keep_agent=root)` 不变。
- 在途：对 root agent 调 `_pause_task`，对其余 agent 调 `_cancel_run_token`。两者都是**内部
  task 级原语**，不是 agent 级接口。
- `_pausing` / `_pause_claimed` 闩锁机制不变（它管的是闩锁窗口内新派发 run 的出生状态，
  与寻址粒度无关）。
- docstring 写明它不是 `pause_agent` 的广播。

**这里刻意不用 `cancel_agent`**：`cancel_agent` 会把目标推到 `terminated` 终态，而
`pause_session` 今天对非 root agent 做的是**取消它们的在途 run**——agent 经 `TASK_CANCELED`
→ ALM 的 `AgentInput.SETTLED` 落回 `idle`，仍然活着、仍可被 `send_message` 寻址。改用
`cancel_agent` 会把这些 agent 一并终态化，是语义变更，与本节「语义保留」的前提冲突。

因此 `pause_session` 的收敛落在**具名化**上：把现在内联遍历 `_run_tokens` 的循环体换成
对 `_pause_task` / `_cancel_run_token` 两个具名原语的调用，消除 runtime 直接拍 token 的写法，
而不改变任何一个目标的终局状态。

### 7.2 `cancel_session`

退成三步：清队列（`cancel_all`）→ 对该 session 下每个 agent 调 `cancel_agent` → 回收
session 资源（`_release_session`）。删掉现在那圈自己遍历 `_run_tokens` 拍 cancel 的代码
（`cancel_agent` 对 `running` 目标内部就会调 `_cancel_run_token`，覆盖同一批在途 run），
R23 那段解释补丁存在理由的注释随之消失。

与 `pause_session` 的差别在于终局意图：会话取消要的就是全部 agent 进 `terminated`，
`cancel_agent` 正是唯一的 agent 终态入口，这里用它是语义相符而非语义变更。

两条顺序纪律不变，且都是硬约束：

1. `_cancel_session_hitl` 先于一切副作用（HITL 终局须先于会话终态）。
2. `cancel_agent` 循环必须在 `_release_session` **之前**——后者会把 agent record 从 ALM
   摘掉，届时 `cancel_agent` 查无此 agent，`AgentTerminated` 发不出去。

### 7.3 `compact_session` → `compact_agent`

```python
async def compact_agent(self, agent_id: str, *, task_id: str = "") -> CompactReceipt
```

`session_id` 从 `record_of(agent_id)` 反查。`CompactReceipt` 是取代现有
`dict[str, str]` 返回值的冻结 dataclass（`session_id` / `agent_id` / `task_id`），
与仓内其余 host-facing 视图类型（`AgentSummary` / `HitlRequestView`）同风格。

### 7.4 `set_session_llm`

已经是纯广播（转发给 `ALM.set_session_llm`），不变。

---

## 8. 寻址口径与封装

- `agent_id` 全局唯一，是唯一路由依据。`send_message` 的 `session_id` 保留为可选防呆参数。
- `list_agents` 的 `session_id` 降为可选过滤；不传则跨 session 列出全部登记 agent。
- `compact_agent` 与 `_pause_task` 不再要求调用方提供 session_id。
- `runtime` 不再触碰 `AgentLifecycleManager` 私有字段（§5.3）。
- `pause_task` 从公开面消失，降为 `_pause_task`；`pause_agent` 与 `pause_session` 是它仅有的
  两个调用方。

---

## 9. 事件类型变更汇总

**停发进 L 档（3 种）**：`TASK_QUEUE_BLOCKED`、`TASK_QUEUE_INTERRUPTED`、`TASK_QUEUE_DRAINED`。
L 档由 **17 增至 20**（实测 `protocols/events.py:254` 的 `L_TIER_EVENT_TYPES` 当前是 17 个：
2 个 session 派生 + 6 个 legacy HITL + 4 个 BackgroundObserve + 1 个 RecognizeIntentLLMPrompt
+ 4 个 SESSION_* 运行态。09-03 spec §10 写的「9 增至 18」把改造前的基数记成了 9，实为 8）。
枚举值与 reducer 分支一律保留（`events-v2.md` §5/§6）。

**新增类型**：无。

**新增发射场景**：

- 恢复期 ALM 装填后的 `AGENT_WAITING_HUMAN` / `AGENT_IDLE` / `AGENT_INTERRUPTED` 现状广播。
- `recognize_intent` 与 `background_observe` 快照 run 的 `RunStarted` / `RunFinished` 配对。

**不变式复核**（`events-v2.md` §6）：三个 `TASK_QUEUE_*` 从 S 档移入 L 档，
S/O/L 三集合仍两两不交、并集仍等于 `EventType` 全集；停发后 L 档 ∩ 实际发射集合 = ∅。

---

## 10. 破坏性变更范围（明确接受，不做兼容 shim）

| 变更 | 影响面 |
|---|---|
| `RunHandle` → `TurnHandle`，去掉 `run_id`、`events()`/`wait_for_finish()` 换判据 | 全部持句柄的调用方与测试 |
| `send_message` 返回 `TurnHandle` 而非 `task_id` | host 消息入口 |
| `compact_session` → `compact_agent`，主键翻转、返回 `CompactReceipt` | host 压缩入口 |
| `recover_session` → `recover_agent`；`recover()` 返回值语义变为 agent 数 | host 启动与续跑路径 |
| 删除 `session_status_after_recover` | host 启动后的状态查询 |
| `pause_task` 从公开面消失 | 直接调它的 host / 测试 |
| `list_agents` / `list_pending_hitl` 签名改为全关键字可选参数 | 全部调用方 |
| `EventFilter` 新增 `agent_id` | host 自实现的 `EventBus` 需支持该过滤 |
| `HitlRequestView` 新增 `delivery` | 只增字段，读方兼容 |
| 三个 `TASK_QUEUE_*` 停发 | host 若在消费需改订 `AGENT_*` |
| `run_id` 不再跨轮复用 | host 若用它当「整个会话流」过滤需改用 `session_id` |
| `ctx_weft.__init__` 导出面扩大 | 只增不减 |

`ctx_weft/__init__.py` 当前只导出 `CtxWeftRuntime` / `RunHandle` / `SessionStartParams`，
agent-centric 的类型（`AgentSummary` / `AgentDetail` / `HitlReply` / `HitlRequestView` /
`AgentNotFound` / `AgentNotRunningError` / `TurnHandle` / `CompactReceipt`）一个都不在
SDK 公开面上，host 必须深挖 `ctx_weft.protocols.*`。本次一并补全。

---

## 11. 组件职责对照表（变更前后）

| 组件 | 变更前 | 变更后 |
|---|---|---|
| `CtxWeftRuntime` 对外面 | agent 级与 session/task 级两套并存，互不自洽 | 一套：agent 为主键，session 只做登记表操作与可选过滤 |
| `RunHandle` / `TurnHandle` | 以 run_id 为轴，run_id 跨轮复用 | 以 agent + task 为轴，run 退回引擎内部 |
| `_SessionTaskRunner` | 持 `_default_run_id`，非 subagent 任务共用一个 run_id | 不持 run_id，`assemble` 每轮自铸 |
| `EventFilter` | session / run / task 三维 | 四维，加 `agent_id` |
| `AgentLifecycleManager` | 私有 `_agents` 被 runtime 五处穿透 | 暴露 `record_of()` 只读访问器 |
| `recover()` | 装填 HITL + 登记 session，ALM 不装填 | 同时装填 ALM，按 agent 报告与广播现状 |
| `SessionRegistry` | 只订阅两个 `AGENT_*`；`TASK_QUEUE_*` 向它空播 | 不变；空播源整个删除 |
| `TaskManager` | 发三个 `TASK_QUEUE_*` 无消费者 | 停发，其余不变 |
| `EventStore` | 按 session 分区 | 不变——分区键与语义单位是两件事 |

---

## 12. 已知留待实现阶段处理的细节

- **与批次二 Task 5 的重叠必须先处理**：实施前在
  `docs/superpowers/plans/2026-09-03-outstanding-issues-batch2.md` 中把 Task 5 标记为已并入，
  确认没有另一个进程正在改 `background_observe.py` / `recognize_intent.py`。
- `TurnHandle.wait_for_finish()` 认定终态的事件集合，与 `TERMINAL_TASK_STATUSES` 保持同源，
  实施时确认 `TaskFinalized` 是否需要与 `TaskFinished` 一并计入（避免 finalize 阶段返回过早）。
- `recover()` 里 `ALM.load()` 与 `rebuild_agent()` 的实现关系：后者应当是前者的单 agent 切片，
  而不是另写一遍折叠逻辑。
- 恢复期 `AGENT_*` 现状广播的发射者归属：放在 ALM 的装填路径里（与运行期同一发射点）
  比放在 runtime 更符合「ALM 是 `AGENT_*` 的唯一发射者」的既有纪律。
- `AgentRecordView` 是否需要与 `AgentDetail` 合并——两者字段高度重叠，前者是 core 内部只读投影、
  后者是 host-facing 视图。实施时若确认字段完全一致，应合并为一个类型。
- host 侧 `projection_updater.py` 不共享 core 的 reducer：`TASK_QUEUE_*` 停发与恢复期
  `AGENT_*` 广播需由 host 侧测试补足（`events-v2.md` §6 不变式 3）。
- `ARCHITECTURE.md` 已严重漂移（仍在讲 `AgentRegistry` / `SessionManager` 与已搬走的文件路径）。
  本设计不含文档治理，但实施完成后应另开工单一次性收干净。
