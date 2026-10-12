# 从 `master` 迁移到 `feat/multimodal` · 接口对照手册

> **这份文档回答一个问题**：宿主在 `master` 上用旧接口实现的每一件事，在当前分支上该怎么写。
>
> 它是**接口对照**，不是行为说明。每条迁移后面链着 `docs/upgrade/` 里那份讲「为什么变、
> 行为差在哪」的文档——真要动手时两份一起读。
>
> **没有兼容 shim，没有 deprecation 期。** 旧名字一律直接 `AttributeError` /
> `ImportError` / `TypeError`。这是刻意的（三份计划文档各自都写了同一条裁定）。

分支跨度：`master`（`3c2cd8a`）→ `feat/multimodal`。中间落地的六批改造各有专文：

| 批次 | 专文 | 本文对应章节 |
|---|---|---|
| 多模态 + providers 目录重组 + 双 blob store | `docs/host-migration-to-sql-memory.md` | §3、§9 |
| HITL 机制重做 | `docs/upgrade/2026-09-01-hitl-redesign.md` | §6、§10 |
| agent 域 + LLM 归属 | `docs/upgrade/2026-09-02-agent-llm-ownership.md` | §7 |
| 会话状态所有权 | `docs/upgrade/2026-09-02-session-status-ownership.md` | §8 |
| task 状态所有权 | `docs/upgrade/2026-09-02-task-status-ownership.md` | §8 |
| agent-centric 交互 | `docs/upgrade/2026-09-03-agent-centric-interaction.md` | §4、§8 |
| 遗留问题批次一 | `docs/upgrade/2026-09-03-outstanding-issues-batch1.md` | §8 |
| runtime 对外面对齐（**无专文，见设计**） | `docs/superpowers/specs/2026-09-04-runtime-agent-centric-surface-design.md` | §4、§5、§7 |

---

## 0. 一句话说清变了什么

`master` 的对外面是**「以 session/run 为中心的单轮执行器」**：你开一个 session，拿一个
`RunHandle`，等 `RunFinished`，要续跑就 `recover_session`。

当前分支是**「以 agent 为中心、可多轮对话、可枚举可干预的 SDK」**：

- **`agent_id` 是唯一的寻址主键**，全局唯一。
- **`session_id` 降为三件事**：成员登记表、事件存储的分区键、资源边界。它不再是语义单位。
- **`task` 与 `run` 退回引擎内部**。句柄上没有 `run_id`，`pause_task` 从公开面消失。

宿主代码里每一处「拿 session_id 去驱动执行」的地方，都要改成「拿 agent_id 去驱动」。
拿不到 agent_id 的地方，用 `list_agents(session_id=...)` 换。

---

## 1. 全量接口对照表（先看这张，再看细节）

### 1.1 执行入口

| `master` | 当前分支 | 性质 |
|---|---|---|
| `start_session(params) -> RunHandle` | `start_session(params) -> TurnHandle` | 返回类型 |
| `run_single_task(...) -> tuple[RunHandle, LoopState]` | `... -> tuple[TurnHandle, LoopState]` | 返回类型 |
| **（无）** | `send_message(agent_id, content, *, session_id=None, unattended=False) -> TurnHandle` | **新增**：多轮对话入口（`unattended` 见 §5.6） |
| `recover_session(session_id, *, user_reply, llm_account, llm_model, resumed_task_id)` | `recover_agent(agent_id, *, user_reply, resumed_task_id, hitl_id, keep_alive)` | 换主键 + 去掉模型参数 |
| `compact_session(session_id, *, agent_id=None, task_id="") -> dict[str, str]` | `compact_agent(agent_id, *, task_id="") -> CompactReceipt` | 主键翻转 + 类型化 |

### 1.2 控制

| `master` | 当前分支 | 性质 |
|---|---|---|
| `pause_session(session_id) -> bool` | 同名同签名同语义 | 执行部分内部重建，宿主无感 |
| `cancel_session(session_id) -> bool` | 同名同签名同语义 | 同上 |
| `pause_task(session_id, task_id) -> bool` | **删除**（降为 `_pause_task`） | 见 §5.3 |
| **（无）** | `pause_agent(agent_id, *, reason=None) -> list[str]` | **新增** |
| **（无）** | `resume_agent(agent_id) -> list[str]` | **新增** |
| **（无）** | `cancel_agent(agent_id, *, reason=None) -> list[str]` | **新增** |
| `recover_session(..., llm_account=, llm_model=)` | `set_agent_llm(agent_id, *, llm_account, llm_model, reason)` | **换模型从续跑里拆出来** |
| 直接改 session 记录的 `llm_model` | `set_session_llm(session_id, *, llm_account, llm_model, reason) -> int` | **新增** |

### 1.3 只读查询

| `master` | 当前分支 | 性质 |
|---|---|---|
| **（无）** | `list_agents(*, session_id=None, parent_agent_id=None, include_terminated=False) -> list[AgentSummary]` | **新增** |
| **（无）** | `get_agent(agent_id) -> AgentDetail` | **新增** |
| `runtime.hitl_manager.list_pending(session_id)` | `list_pending_hitl(*, session_id=None, agent_id=None) -> list[HitlRequestView]` | 换住所 + 换返回类型 |
| `runtime.hitl_manager.approve/answer/reject(...)` | `reply_to_hitl(HitlReply(...)) -> HitlRequestView \| None` | 三合一 |
| `runtime.event_bus` | 不变 | — |

### 1.4 恢复 / 重建

| `master` | 当前分支 | 性质 |
|---|---|---|
| `recover() -> int`（返回 **session 数**） | `recover() -> int`（返回 **agent 数**，且装填 ALM） | 返回值语义变化 |
| `rebuild_hitl(session_id) -> int` | 不变 | — |
| `rebuild_all_pending_hitl() -> int` | 不变 | — |
| **（无）** | `rebuild_agent(agent_id) -> bool` | **新增** |
| **（无）** | `rebuild_all_agents() -> int` | **新增** |

### 1.5 已删除、且**没有**同名替代的

| 删掉的 | 现在怎么办 |
|---|---|
| `RunHandle` | `TurnHandle`（少一个 `run_id` 字段，`events()`/`wait_for_finish()` 判据换了，见 §4） |
| `RunHandle.run_id` | 不需要。要按轮聚合就读事件信封里的 `run_id` |
| `pause_task(session_id, task_id)` | `pause_agent(agent_id)`；要精确到 task 说明你在用引擎内部粒度，重新想一下 |
| `runtime.hitl_manager` 及其全部方法 | `list_pending_hitl` / `reply_to_hitl`（§6） |
| `HitlManager` / `HitlRequest` / `AuthorizationDecision.defer` | §6、§10 |
| `session_status_after_recover(session_id)` | 读 `HitlRequestView.delivery` 自己判（§5.4） |
| `InMemoryEventStore(event_bus=...)` / `.detach()` | `InMemoryEventStore()` + 构造 runtime 时传 `event_store=`（§3） |
| `Authorizer.filter(...)` | 无替代，零调用点且会真的向人求批；要装配期过滤请另行设计（§10） |
| `FilesystemBlobStore` | `ctx_weft.providers.blob.FsBlobStore`（§9） |

---

## 2. 导入路径迁移表

整个 `core/` 做了分层重组：**契约进 `protocols/`，实现进 `providers/`，core 只剩引擎**。
宿主代码里的 `from ctx_weft.core.xxx import` 基本都要改。

| `master` | 当前分支 | 顶层 `ctx_weft` 是否导出 |
|---|---|---|
| `core.runtime.ProviderRegistry` | `core.registry.ProviderRegistry` | ✅ |
| `core.runtime.RunHandle` | `core.runtime.TurnHandle` | ✅ |
| `core.runtime.{CtxWeftRuntime, SessionStartParams}` | 不变 | ✅ |
| `core.state.event_store.InMemoryEventStore` | `providers.events.InMemoryEventStore` | ✅ |
| `core.state.event_store.EventStore`（协议） | `protocols.events.EventStore` | ✅ |
| `core.state.models.{TaskSettings, NormalTaskSettings, CompactTaskSettings, MetadataFillerTaskSettings}` | `core.models.task` | ✅ |
| `core.state.models.{SessionStatus, TaskStatus, TERMINAL_SESSION_STATUSES, WAITING}` | `core.models.status` | ❌（core 内部） |
| `core.errors.*` | `core.models.errors` | 五个 agent/session 异常 ✅，其余 ❌ |
| `core.config.RuntimeConfig` | `core.models.config.RuntimeConfig` | ✅（构造 runtime 要传它，必须在受支持面上） |
| `core.events.types.{Event, EventFilter, EventType, RunSnapshot}` | `protocols.events` | ❌（从 `ctx_weft.protocols` 取） |
| `core.events.bus.InProcessEventBus` | `providers.events.InProcessEventBus` | ❌（从 `ctx_weft.providers.events` 取） |
| `core.utils.{generate_id,now_utc,content_to_text}` | `core.utils.{ids,clock,content}` | ❌ —— `core/utils` 从模块变成包且**不做 re-export**（「一个符号只有一条 import 路径」）。宿主本就不该跨库拿这两个工具，自己实现即可 |
| `core.auth.authorizer.{Authorizer, AuthorizationDecision}` | `protocols.capability`（`ctx_weft.protocols` 导出） | ❌ |
| `core.auth.authorizer.{AllowAllAuthorizer, AllowListAuthorizer, HumanConfirmationAuthorizer}` | `providers.authorizer` | ❌ |
| `providers.memory_blackboard.InMemoryMemoryProvider` | `providers.memory.InMemoryMemoryProvider` | ❌ |
| **（无）** | `providers.memory.sql.SqlMemoryProvider` | ❌，需 `pip install ctx-weft[sql]` |
| **（无）** | `providers.blob.FsBlobStore` | ❌ |
| **（无）** | `providers.events.{EventPersister, SnapshotWriter, attach_persistence}` | ❌ |
| `core.orchestrator.HitlManager` | **删除** | — |

**HITL / agent 视图类型全部走 `ctx_weft.protocols`**（也从顶层 `ctx_weft` 导出）：
`HitlAsk` / `HitlDecision` / `HitlReply` / `HitlRequestView` / `Delivery` 三成员 /
`HITL_FORM_*` / `HITL_OUTCOME_*` / `AgentSummary` / `AgentDetail` / `CompactReceipt`。

**顶层导出面从 5 个扩到 15 个**（只增不减）：

```python
from ctx_weft import (
    CtxWeftRuntime, ProviderRegistry, SessionStartParams, TurnHandle,   # 运行时与入参
    AgentDetail, AgentSummary, CompactReceipt,                          # agent 发现与回执
    HitlReply, HitlRequestView,                                         # HITL
    AgentNotFound, AgentNotRunningError, AgentBusyError,                # 错误
    AgentTerminatedError, SessionBusyError,
    EventStore, InMemoryEventStore,                                     # 事件
    TaskSettings, NormalTaskSettings, CompactTaskSettings,              # task 配置
    MetadataFillerTaskSettings,
)
```

宿主应当**只从顶层和 `ctx_weft.protocols` 取名字**。深挖 `ctx_weft.core.*` 是不受支持的
用法——这次重组里被搬走的每一个内部模块，都是靠这条纪律才没有变成破坏性变更。

---

## 3. 接线：构造 runtime

### 3.1 `master` 的写法

```python
from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry
from ctx_weft.core.state.event_store import InMemoryEventStore
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider

providers = ProviderRegistry()
providers.register_memory(InMemoryMemoryProvider())
providers.register_llm_provider(my_resolver)

hitl = HitlManager(timeout_sec=300)
runtime = CtxWeftRuntime(providers, llm=None, hitl_manager=hitl)

# 常见模式：先建 runtime，DB 就绪后再换掉 event_store
runtime.event_store = MyPostgresEventStore(...)
```

### 3.2 当前分支的写法

```python
from pathlib import Path

from ctx_weft import CtxWeftRuntime, ProviderRegistry
from ctx_weft.providers.memory import InMemoryMemoryProvider
from ctx_weft.providers.blob import FsBlobStore

providers = ProviderRegistry()
providers.register_memory(InMemoryMemoryProvider())
providers.register_llm_provider(my_resolver)

# 多模态：字节必须显式接。不接就是"不接 blob"，携图会话在入口被拒（§9）
blobs = FsBlobStore(Path("/var/lib/app/blobs"))   # 可选 grace_period=timedelta(hours=24)
providers.register_memory_blob_store(blobs)
providers.register_event_blob_store(blobs)      # 同一实例注册两次 = 显式声明"共用"

event_store = MyPostgresEventStore(...)          # ⚠️ 必须在构造 runtime 之前建好

runtime = CtxWeftRuntime(                        # ⚠️ 全部 keyword-only
    providers=providers,
    event_store=event_store,
    snapshot_every_n=50,                         # 0（默认）= 不接 SnapshotWriter
)
```

四条硬性变化：

1. **构造函数改成 keyword-only**。`CtxWeftRuntime(providers, llm)` 这种位置传参直接
   `TypeError`。
2. **`hitl_manager=` 参数删除**。HITL 现在是构造期一次性接线的 `HitlRegistry` +
   `HitlService`，**没有 setter、没有半成品窗口**——裸构造即生产形态。想调超时改
   `RuntimeConfig.hitl_timeout_sec`。
3. **`event_store` 必须在构造时传入，不能事后替换**。runtime 在 `__init__` 里自己调
   `attach_persistence(bus, event_store, snapshot_every_n=...)`，且 `recover()` /
   `rebuild_view` / `read_session_events_of_types` **所有读路径**读的都是
   `self.event_store`。晚接的 store 只会被写、从不会被读——重启后 `recover()` 从空的
   内存 store 恢复，"崩溃后存活"这个工作流根本不工作，还会让每条事件存两遍。
   （`InMemoryEventStore` 也因此不再收 `event_bus=`，`detach()` 一并删除。）
4. **新增可选 `event_bus=`**：宿主可以传自己的 `EventBus` 实现。传了就要支持
   `EventFilter.agent_id` 过滤（§8.1）。

---

## 4. 句柄：`RunHandle` → `TurnHandle`

```python
# master
@dataclass
class RunHandle:
    run_id: str; session_id: str; task_id: str; agent_id: str
    template_id: str; event_bus: EventBus

# 当前分支 —— 少了 run_id
@dataclass
class TurnHandle:
    session_id: str; agent_id: str; task_id: str
    template_id: str; event_bus: EventBus
```

### 4.1 `run_id` 没了

`run` 是引擎内部**一轮循环**的相关性 id，句柄不需要它。要按轮聚合就读事件信封里的
`run_id`（每条事件都带）。

顺带一条：**`run_id` 不再跨轮复用**。`master` 上 `start_session` 预铸一个 run_id，一个
owner-TM 下所有根 scope 轮次共用它、各自从 `sequence=0` 起编号——`(run_id, sequence)`
跨轮撞号，一个 run_id 下出现多对 `RunStarted`/`RunFinished`。现在一轮一个 run_id，
两条不变式立起来了：`(run_id, sequence)` 全局唯一；每个 run_id 恰有一对 `RunStarted`/
`RunFinished`。**宿主若拿 run_id 当"整个会话流"的过滤键，改用 `session_id`。**

### 4.2 `events()` 的订阅轴换了

```python
# master：按 run_id 订阅
EventFilter(run_id=handle.run_id)

# 当前分支：按 agent + task 订阅
EventFilter(agent_id=handle.agent_id, task_id=handle.task_id)
```

要只看某个 agent 的完整事件流（跨多轮），直接用新的 agent 维度：

```python
async for ev in runtime.event_bus.stream(EventFilter(agent_id=aid)):
    ...
```

### 4.3 `wait_for_finish()` 的判据换了，契约也强了

`master` 等的是 `RunFinished`。当前分支等的是**该 task 的终态事件**
（`TaskFinished` / `TaskFailed` / `TaskCanceled`），**并且**等 close 边界那次后台
observe 的段折叠/胶囊化落地。

这条不是措辞讲究，是宿主典型用法的正确性前提：`send_message` → `await
wait_for_finish()` → 从 memory 读回对话渲染给用户。折叠没落地时读到的是还没胶囊化的
原始 raw 记录，渲染出来的对话是半成品。

**这是新确立的保证，不是旧行为的复原**——`master` 的 `RunFinished`-based 等待同样不
保证这一点，只是没人写下来。

### 4.4 `handle.root_agent_id` 不存在

`start_session(...)` 返回的 `handle.agent_id` **就是**该 session 的 root agent id
（`parent_agent_id is None`），恒非空，可直接传给 `send_message` / `get_agent`。
早期设计文档提过要加一个独立的 `root_agent_id` 字段，**没做**。

---

## 5. 执行入口逐条迁移

### 5.1 开一个会话：签名没变，返回类型变了

```python
params = SessionStartParams.create(
    template_id="assistant",
    user_prompt="你好",              # 也可以是 list[ContentPart]，见 §9
    context_limit=200_000,
)
handle = await runtime.start_session(params)   # -> TurnHandle
await handle.wait_for_finish()
root_agent_id = handle.agent_id                # ← 记下它，后续多轮全靠它
```

`SessionStartParams` 字段一个没删，只有 `user_prompt` 的类型从 `str` 放宽成
`str | list[ContentPart]`；另**新增一个可选字段** `unattended: bool = False`，见 §5.6。

### 5.2 第二轮及以后：`send_message`，不是 `recover_session`

这是 `master` **完全没有**的能力。旧代码里凡是"用户又说了一句话"的处理——不管是靠
`recover_session(user_reply=...)` 硬塞，还是每轮开一个新 session——都应该改成：

```python
handle = await runtime.send_message(root_agent_id, "再帮我查一下 X")
await handle.wait_for_finish()
```

三条路径都返回 `TurnHandle`，`agent_id` 恒为被寻址的那个 agent：

| 路径 | 条件 | `handle.task_id` |
|---|---|---|
| 新建 task | 当前 task 已终态或为空 | 新建 task 的 id |
| 注入 + 重排 | 当前 task 未终态 | 该活 task 的 id |
| 注入不重排 | 该 agent 因 `delegate_task` 挂起、仍有活子任务 | 该活 task 的 id |

想知道"这条消息是开了新一轮还是并进了现有一轮"：调用前记下
`get_agent(agent_id).current_task_id`，与返回的 `handle.task_id` 比对。

**错误分流**（旧代码没有这些分支，要补）：

| 目标 agent 状态 | 抛什么 |
|---|---|
| 不存在 | `AgentNotFound` |
| `terminated` | `AgentTerminatedError` |
| `running`（正在跑） | `AgentBusyError` |

**不排队**——调用方自行重试，或先 `pause_agent` / `cancel_agent`。

> ⚠️ 已知行为差异：`pause_session` / `cancel_session` 的收尾窗口内对**非 root 子 agent**
> 调 `send_message` 且走"新建 task"分支时，返回的 `task_id` 对应的 run 会出生即被取消，
> 消息静默丢失、没有显式失败事实。要强一致送达确认，暂时避开这个窗口。

### 5.3 `pause_task` 消失了

`master` 的 `pause_task(session_id, task_id)` 降为内部 `_pause_task`，只有
`pause_agent` 与 `pause_session` 两个调用方。

宿主 UI 上"停止这一条"应当改成 `pause_agent(agent_id)`：级联暂停该 agent 及其子树，
不杀任何东西，目标非 `running` 时抛 `AgentNotRunningError`。

三个 agent 级控制入口都返回**受影响的 agent id 列表**：

```python
paused    = await runtime.pause_agent(aid, reason="user_clicked_stop")
resumed   = await runtime.resume_agent(aid)
cancelled = await runtime.cancel_agent(aid, reason="user_discarded")
```

`cancel_agent` 是**唯一**的 agent 终态入口（推向 `terminated`）；`pause_agent` 不是。

### 5.4 `pause_session` / `cancel_session`：签名语义都没变

两者的**对外契约逐字未变**，内部执行部分重建了（不再直接拍 run token）。宿主不用改。

需要知道的一条口径：**`pause_session` 不是 `pause_agent` 的广播**，两者语义相反。
`pause_session` 是会话级软打断（UI 上的"停止"）：弃掉全部排队任务、取消 root 之外的
在途 run、只留 root agent 当前那一轮作为唯一续跑点。被取消的非 root agent **仍然活着、
仍可被 `send_message` 寻址**（它们落回 `idle`，不是 `terminated`）。

### 5.5 压缩：`compact_session` → `compact_agent`

```python
# master
result: dict[str, str] = await runtime.compact_session(sid, agent_id=aid, task_id=tid)

# 当前分支
receipt: CompactReceipt = await runtime.compact_agent(aid, task_id=tid)
receipt.session_id; receipt.agent_id; receipt.task_id; receipt.task_id_is_transient
```

`session_id` 由 `agent_id` 反查，不用传。返回值从裸 dict 变成冻结 dataclass。

### 5.6 无人值守：`unattended`（新增）

`master` 没有这个概念。后台跑的自治作业没有人可问——agent 调 `ask_user`、某个工具触发
人工授权、或纯文本回合想让位给用户，都会 park 到死（没有人会来应答）。把这类作业**在
入口处**标出来：

```python
# 起一个后台自治会话
handle = await runtime.start_session(SessionStartParams.create(
    template_id="assistant", user_prompt="每晚跑一遍回归", context_limit=200_000,
    unattended=True,
))

# 或者：给一个已有 agent 投喂一条后台消息（只影响这条消息**新开**的 task）
handle = await runtime.send_message(agent_id, "再跑一次", unattended=True)
```

| 面 | 变化 |
|---|---|
| `SessionStartParams` | 新增字段 `unattended: bool = False`（`create()` 同名形参），落到 root task |
| `send_message` | 新增 keyword `unattended: bool = False`，只作用于它**新建**的那个 task |
| `TaskCreated` 事件 | payload 的 `task` 对象新增 `unattended` 布尔（存量事件无此键 → `False`） |

语义与两条相邻旋钮**不重合**：`interaction_mode` 答「纯文本回合要不要停」、
`token_budget` 答「允许花多少」，`unattended` 答的是**有没有人在**。宿主只需要设置它，
其余是引擎的事：

- 标记挂在 **task** 上（`Task.unattended`），委派出去的子任务**继承**它（LLM 没有这个
  旋钮，`delegate_task` / `delegate_plan` 的 schema 里不存在该参数）。
- 入口处强制不变式 `unattended ⟹ interaction_mode == "auto"`：没有人会发下一条消息，
  interactive 的纯文本 park 就是永久挂起。
- 任何 HITL 在唯一的登记入口（`HitlService.open()`）被挡下，**不会**留下 pending、
  **不会**让 task 落 `AWAITING_HUMAN`。宿主侧的观感是：需要审批的工具调用当场被拒
  （`[Blocked by human: …]` 回灌模型），`ask_user` 当场收到「没有人可答，自己决定或
  `control__finish_task` 说清卡在哪」，作业继续往终态走。

---

## 6. 恢复与 HITL

这是改动最大的一块，因为 `master` 把两件事糅在了一个入口里：`recover_session` 既是
"崩溃后续跑"，又是"人答复了，接着跑"，还是"顺便换个模型"。现在拆成三条独立命令。

### 6.1 崩溃后续跑：`recover_session(sid)` → `recover_agent(aid)`

```python
# master
await runtime.recover_session(session_id)

# 当前分支
await runtime.recover_agent(agent_id)
```

宿主手上通常只有 session_id（`/resume` 端点的路径参数）。换法：

```python
await runtime.recover()                                   # 启动时跑一次，装填 ALM
agents = runtime.list_agents(session_id=session_id)       # 现在能列出来了
root = next(a for a in agents if a.parent_agent_id is None)
await runtime.recover_agent(root.agent_id)
```

`recover_agent` 对未登记的 agent_id 会**先自愈再抛**（内部调一次 `rebuild_agent`），
所以偶尔漏装填不会摔在地上——但那条自愈路径是 O(active session 数) 次事件日志读取，
不要当常态路径用。

**`recover_agent` 不收 `llm_account` / `llm_model`。** 续跑路径一概不碰模型，见 §7。

### 6.2 `recover()` 的返回值语义变了，且现在真的装填 agent

| | `master` | 当前分支 |
|---|---|---|
| 返回值 | 恢复的 **session** 数 | 恢复的 **agent** 数 |
| 是否装填 `AgentLifecycleManager` | **否** | **是** |

`master` 的 `recover()` 每个 session 只做 `rebuild_hitl` + 登记 session，从不装填 agent
记录。这在 `master` 上无所谓（没有 agent 面），但在当前分支上**必须**：不装填的话重启后
`list_agents` 返回空、`get_agent` 抛 `AgentNotFound`、`send_message` / `cancel_agent` /
`pause_agent` 全部失败。

**宿主启动序列必须调一次 `recover()`**，并且不要再拿返回值当 session 数用（做告警阈值
或日志的地方会读出不同量级的数）。

装填完成后 ALM 会按折出来的现状为每个 agent 广播
`AgentWaitingHuman` / `AgentIdle` / `AgentInterrupted`——宿主投影因此不会停在崩溃前的
状态。**这不引入 `RECOVERING` 之类的新值域**，发的是现状，不是新状态。

按需自愈的两个新入口：`rebuild_agent(agent_id) -> bool` / `rebuild_all_agents() -> int`。

### 6.3 `rebuild_hitl` / `rebuild_all_pending_hitl`：不变

**刻意保持 session 主键**。它做的是"扫这个存储分区、把折出来的未决集合喂进内存"，
主键就是分区键。**装填按分区，查询按 agent**——查询侧的 agent 维度由
`list_pending_hitl(agent_id=...)` 提供。

### 6.4 `session_status_after_recover` 删除了

它返回的 `"PAUSED"` / `"PAUSED_HITL"` 不是任何一个状态机的值域，算的是"未决 HITL 的
delivery 性质"。会话状态机已删，这个派生串没有存在理由。

现在直接读原始事实：

```python
for req in runtime.list_pending_hitl(session_id=sid):
    if isinstance(req.delivery, UserTurnDelivery):
        ...   # 等用户说话：出输入框，无审批面板
    else:     # ToolResultDelivery / NoResumeDelivery
        ...   # 出审批面板
```

### 6.5 HITL 应答：`hitl_manager.*` → `reply_to_hitl`

`runtime.hitl_manager` **整个删除**，`HitlManager` / `HitlRequest` 类不存在了。

| `master` | 当前分支 |
|---|---|
| `hitl_manager.list_pending(sid)` → `list[HitlRequest]` | `runtime.list_pending_hitl(session_id=..., agent_id=...)` → `list[HitlRequestView]` |
| `hitl.approve(id, message, modified_arguments)` | `reply_to_hitl(HitlReply(hitl_id=…, agent_id=…, outcome="accepted", modified_arguments=…))` |
| `hitl.answer(id, text)` | `reply_to_hitl(HitlReply(hitl_id=…, agent_id=…, outcome="accepted", message=text))` |
| `hitl.reject(id, message)` | `reply_to_hitl(HitlReply(hitl_id=…, agent_id=…, outcome="rejected", message=…))` |
| 应答后自己判 `was_hot` 再调 `recover_session` | **不再需要**：`reply_to_hitl` 内部按 `claimed` 分流并驱动冷续跑 |
| 换模型：`reply_to_hitl(..., resume_hint=...)` | 拆两步：先 `set_agent_llm`，再 `reply_to_hitl`（顺序不能反） |

`HitlReply.agent_id` 是**必填防呆字段**：调用方必须声明"我以为在回复哪个 agent"，与
记录里的 `PendingHitl.agent_id` **严格相等**——**空串也须对上**。不符会被拒绝，不再
"按 hitl_id 静默走掉"。

`reply_to_hitl` 对已终局的请求返回 `None`（幂等，不重复续跑）。

> `PendingHitl` **不出 core**。宿主只见 `HitlRequestView`——它带 `delivery` 字段，
> 前端据此决定出输入框还是审批面板（§6.4）。

---

## 7. 模型选择：从续跑参数拆成独立命令

**LLM 的真相源从 session 移到了 agent record**（`AgentLifecycleManager` 持有）。
`session.llm_model` 不再是权威值，只在没有更精确来源时兜底。

```python
# master：混在续跑里
await runtime.recover_session(sid, llm_account="acc", llm_model="claude-x")

# 当前分支：两条独立命令
await runtime.set_agent_llm(agent_id, llm_account="acc", llm_model="claude-x")
n = await runtime.set_session_llm(session_id, llm_model="claude-x")   # 返回真正改动的 agent 数
```

- 宿主若有"整个会话切模型"的入口，改调 `set_session_llm`，**不要再直接写 session
  记录的 `llm_model` 字段**——那条路径已不是权威写入点。
- 想展示"当前会话用的什么模型"，读某个 agent（通常是 root）的 `llm_model`。
- 两者都发 `AgentLlmChanged`，宿主投影要加这个分支（§8.2）。

> ⚠️ `set_session_llm` 只对 registry **当前持有的** record 生效。跨进程重启后若还没
> `recover()` 过，它会在空候选集上遍历——**返回 `0`、不发事件、不报错**。返回 `0` 而你
> 预期 `> 0` 时，那是"registry 还没装填"，不是"没有 agent 需要改"。

---

## 8. 事件流

宿主的 `projection_updater.py`（或等价的投影更新器）与 core 的 reducer **不共享代码**，
两边各自维护一份折叠表。**缺哪个分支，那类事实就在投影上安静地过期，不报错。**

### 8.1 `EventFilter` 新增 `agent_id`

```python
@dataclass
class EventFilter:
    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None      # ← 新增
    types: list[str] | None = None
```

**宿主自实现的 `EventBus` 必须支持这个过滤维度。** 事件 `agent_id` 为 `None` 时不匹配
任何具体 `agent_id`——"没有归属"不等于"属于你要的那个"。

### 8.2 停发的事件（宿主订了就会永远等不到）

| 停发的 | 为什么 | 改订什么 |
|---|---|---|
| `SessionStatusChanged` | 会话状态机退役 | 会话状态由 `AGENT_*` / task 事件推导 |
| `SessionPausedHitl` | 同上 | `HitlOpened` |
| `SessionFinished` | 同上 | 见下 |
| `HitlRequired` / `HitlApproved` / `HitlModified` / `HitlAnswered` / `HitlRejected` / `HitlCancelled` | HITL 8 个事件收敛到 2 个 | `HitlOpened` + `HitlResolved`（按 `outcome` 分支） |
| `BackgroundObserve*`（4 个） | 统一发通用 `LLM_*`，靠 `origin` 区分前台/后台 | 按 `Event.origin` 前缀匹配 |
| `RecognizeIntentLLMPrompt` | 同上 | `LLM_PROMPT_SENT` + `origin=loop.recognize_intent` |

**🔴 `SessionFinished` 停发是 `master` 用户最容易踩的一条**：`master` 上它是关 SSE 流的
信号。现在它不再发出，靠它关流的宿主会**永远不关**。

关流判据改成：**本轮 task 的终态事件**（`TaskFinished` / `TaskFailed` / `TaskCanceled`）
——也就是 `TurnHandle.wait_for_finish()` 用的那套判据。最省事的做法是直接用句柄：

```python
await handle.wait_for_finish()   # 返回即代表这一轮真的处理完了（含后台折叠落地）
close_stream()
```

**不要靠 `RunFinished` 单独判关流**：task 状态事件现在**在 `RunFinished` 之后**到达
（`TaskManager` 要等 `execute()` 交回 `RunOutcome` 才处置），收到 `RunFinished` 就关流
会漏掉紧随其后的 task 终态、error、retry_count。`RunFinished.will_retry` 在熔断竞态下
也会说谎，同样不能当唯一判据。

L 档事件的**枚举值与 reducer 读分支一律保留**（重放存量日志要用），宿主的投影更新器
应当同样保留旧分支——只是新流量里不会再有它们。

> **不必关心的 6 个**：`SessionRunning` / `SessionWaiting` / `SessionInterrupted` /
> `TaskQueueBlocked` / `TaskQueueInterrupted` / `TaskQueueDrained` 曾在 2026-09-02 上线、
> 09-03/09-04 退役，全程落在 `master` 之后的分支内部，已于 2026-09-05 连枚举一并删除。
> **`master` 的 `EventType` 里从来没有这 6 个名字**，你的库里不可能有它们——既不用加
> 订阅，也不用为它们保留任何旧分支。

### 8.3 新增的事件（宿主投影要加分支）

| 事件 | payload | 折叠成 |
|---|---|---|
| `AgentInstantiated` / `AgentSpawned` | — | agent 出生 |
| `AgentRunning` / `AgentIdle` / `AgentWaitingHuman` / `AgentInterrupted` / `AgentTerminated` | — | agent 五态机（`AgentLifecycleManager` 是唯一发射者） |
| `AgentLlmChanged` | `{llm_account, llm_model, reason}` | 纯赋值到 agent，不碰 task/session |
| `HitlOpened` | `{hitl_id, form, delivery, stage, tool_call_id, invocation_key, resume_state, reply_as_result, prompt, detail, fields, proposal, subject_id}` | 未决 HITL |
| `HitlResolved` | `{hitl_id, outcome, claimed}` + 可选 `message` / `modified_arguments` | HITL 终局 |
| `TaskAwaitingHuman` | `{hitl_id}` | task → `AWAITING_HUMAN` |
| `TaskHumanResolved` | `{hitl_id}` | task → `PENDING`，且清 `outputs` |
| `TaskInterrupted` | `{reason, error_code?, error_message?, retry_count}` | task → `INTERRUPTED` |
| `RunInterrupted` | `{reason, error_code?, error_message?}` | **不写 task 状态** |
| `ObserveStarted` | `{task_id}` | 纯新增，画 observe 耗时用 |

### 8.4 事件信封与口径变化

- **`Event` 新增顶层字段 `origin`**（哪个组件发的）。不要读 `payload["caller"]`——那个键
  不存在。存量事件读出空串，不是 `None`。
  - **SQL 部署必须先 `ALTER TABLE events ADD COLUMN origin VARCHAR(64);`**——
    `create_all` 只对全新建表生效，存量库不补这一列**写入直接报错**。这是唯一一条会让
    宿主启动即失败的变更。
- **task 状态事件的 `run_id` 变成 `None`、`sequence` 一律 `0`**。它们现在只从
  `TaskManager` 发出，而 TM 不属于任何一次 run。按 `run_id` 归组 task 事件的改成按
  `session_id` + 到达顺序；按 `sequence` 排序/去重的改成按到达顺序或 `timestamp`。
- **`TaskSuspended.payload` 不再带 `task_id`**（envelope 里本来就有）。
- **`RunFinished` 新增 `outcome`**（`completed` / `awaiting_human` /
  `suspended_on_children` / `interrupted` / `canceled`）；旧键 `final_status` 已废弃。
- **`RunCanceled.payload` 新增 `source`**（`"token"` / `"external"`）。
- **`TaskSuspended` 从三义收窄到一义**：`hitl_park` → `TaskAwaitingHuman`，`run_crash` →
  `TaskInterrupted`，只剩"等子任务"这一义。**按 `reason` 字面量分流的改成按类型分流。**
- **`TaskStatus` 新增 `AWAITING_HUMAN` / `INTERRUPTED`**，删除死值 `TO_BE_OBSERVED`。
  完整值域：`PENDING` / `ACTIVE` / `SUSPENDED` / `AWAITING_HUMAN` / `INTERRUPTED` /
  `FINISHED` / `FAILED` / `CANCELED`。
- **`SessionStatus` 收敛到 6 个**：`RUNNING` / `WAITING` / `INTERRUPTED` / `SUCCEEDED` /
  `FAILED` / `CANCELED`。删除 `QUEUED` / `TIMEOUT`（死值），`PAUSED` / `PAUSED_HITL`
  合并进 `WAITING`。**DB 里的存量行读侧要把两个旧值折进 `WAITING`。**
- **`TaskView.outputs` / `TaskView.error` 从"恒为 `None`"修好了**——旧的旁路缓存可以撤掉。

---

## 9. 多模态（分支名的由来）

### 9.1 内容类型放宽

`user_prompt` / `content` / HITL `message` 的类型从 `str` 放宽成 `str | list[ContentPart]`：

```python
from ctx_weft.protocols import ImagePart, TextPart

handle = await runtime.send_message(agent_id, [
    TextPart(text="这张图里是什么？"),
    ImagePart(data=b64_png, media_type="image/png"),   # source_type 默认 "base64"
])
```

涉及的入口：`SessionStartParams.create(user_prompt=...)`、`run_single_task(user_prompt=...)`、
`send_message(content=...)`、`HitlReply.message`、`HitlDecision.message`、
`AuthorizationDecision.message`。

**纯文本路径逐字节不变**，零 IO 直通。旧代码传 `str` 一律照旧工作。

### 9.2 携图会话要求宿主接 blob store

两个独立的 blob 契约，**都不自动解析**、都必须显式注册（§3.2）：

| 契约 | 注册方法 | 不注册的后果 |
|---|---|---|
| `MemoryBlobStore` | `providers.register_memory_blob_store(store)` | 回落 `NullMemoryBlobStore`，图片不外部化（行为等同不接 blob） |
| `EventBlobStore` | `providers.register_event_blob_store(store)` | **携图内容在入口被 `BlobStoreRequiredError` 拒绝** |

两侧的 ref 是**两个独立的命名空间**，core 从不比较、从不拿一侧的 ref 去另一侧解。
要共用就把同一个实例注册两次——意图写在接线代码里。仓内自带 `FsBlobStore` 同时实现了
两个契约，可直接用。

**`SqlMemoryProvider` 不实现 blob 契约。** 注册它**不会**顺带打开图片外部化。
（`FilesystemBlobStore` 已删除。）

### 9.3 两个新异常

| 异常 | `code` | 何时抛 |
|---|---|---|
| `InvalidContentError` | `INVALID_CONTENT` | 未知 `media_type` / base64 畸形 / 单图超限。入口即拒，不落库 |
| `BlobStoreRequiredError` | `BLOB_STORE_REQUIRED` | 携图但没注册 `EventBlobStore` |

宿主的错误分流表要加这两个码。

### 9.4 memory / event store 换实现

`providers/memory_blackboard` → `providers/memory`，并新增 `SqlMemoryProvider`
（`pip install ctx-weft[sql]`）与 `SqlEventStore`。

切换步骤（DDL、blob 接线、两步 mark-sweep 回收、七个坑、验收清单）在
**`docs/host-migration-to-sql-memory.md`**，那份文档是给人照着执行的，本文不重复。
一条要点先摆在这里：**blob 回收变成标准 mark-sweep 两步，必须由宿主自己定时调**，
且共用一个 blob store 实例时**必须把两侧的 mark 都喂进去**，只喂 memory 侧会把事件流
仍需要的字节当孤儿删掉。

---

## 10. Provider / Authorizer 侧的迁移

### 10.1 `Authorizer.authorize` 签名变了（每个自定义 authorizer 都要改）

```python
# master
async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id=""): ...

# 当前分支 —— 去掉 agent 与 task 两个位置参数
async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""): ...
```

契约层不再依赖 core 的状态对象。原先从 `agent` / `task` 上取的信息，从
`ProviderContext` 取：它带 `session_id` / `tenant_id` / `task_id` / `agent_id` /
**`agent_template_id`**（新增，按模板维度做策略用）/ `trace_id` / `invocation_id` 等。

`Authorizer.filter(...)` **已删除**，无替代：它零调用点，且对
`HumanConfirmationAuthorizer` 会真的发出 HITL 请求并等人——把"列一下有哪些工具可见"
变成"向人类逐个求批"。真需要装配期可见性过滤时应另行设计，届时必须显式排除会挂起的
authorizer。

### 10.2 要问人的 authorizer：从"自己等"改成"声明需要人"

`AuthorizationDecision.defer` 字段**删除**，换成 `needs_human: HitlAsk | None`。

```python
class MyAuthorizer(Authorizer):                        # 不再需要构造参数
    async def authorize(self, cap, ctx, arguments=None, *, tool_call_id=""):
        return AuthorizationDecision(allowed=False, needs_human=HitlAsk(
            form=HITL_FORM_APPROVAL,
            delivery=ToolResultDelivery(tool_call_id=tool_call_id),
            subject_id=cap.id,
            prompt=f"Allow {cap.name}?",
            proposal=arguments,
        ))

    # 可选接口 HumanGatedAuthorizer：gateway 拿到人的决定后喂回来给你解释
    async def on_decision(self, cap, ctx, arguments, tool_call_id, decision):
        return AuthorizationDecision(
            allowed=decision.outcome == HITL_OUTCOME_ACCEPTED,
            message=decision.message,
            modified_arguments=decision.modified_arguments,
        )
```

- `needs_human` 非 `None` 时 `allowed` **必须**为 `False`。
- 声明了 `needs_human` 却没有 `on_decision` = 契约违例：gateway 出一条错误 tool result，
  **绝不**放行、也不静默降级。
- 登记、等待、驱逐时抛 `HitlPark`——全部归 gateway。你不做，也不能做。

**只返回 `allowed=True/False` 的 authorizer 只需改签名**（去掉 `agent` / `task`），
其余一个字不用动。

### 10.3 会问人的工具 provider

`invoke` 是异步生成器，让出是 **yield 一个事件**：

```python
class DeployTool(ToolCapabilityProvider, HumanResumable):
    async def invoke(self, cap_id, args, ctx):
        plan = await self.compute_plan(args)          # ⚠️ 见下
        yield CapabilityEvent("needs_human", {"ask": HitlAsk(
            form="question",
            delivery=ToolResultDelivery(tool_call_id=ctx.extra["tool_call_id"]),
            prompt=f"确认部署 {plan.summary}？",
            resume_state=plan.to_dict(),
        )})
        # gateway 见 needs_human 即停止消费并关闭本流，其后 yield 的不可见

    async def resume(self, ask_id, decision, resume_state, ctx):
        plan = Plan.from_dict(resume_state)
        yield CapabilityEvent("result", {"content": await self.apply(plan, decision)})
```

> ⚠️ **`resume_state` 只省掉热重入。** 热窗口被驱逐或进程崩了之后走冷路径：
> `ReconcileStep` 重新调 `invoke`，`compute_plan(args)` **会再跑一遍**。`needs_human`
> 之前的工作必须**幂等或便宜**；不可重复的副作用（扣款、发工单、真正的部署动作）只能
> 放进 `resume()`。

答复直接就是结果（`ask_user` 那一类）时置 `reply_as_result=True`，重入不发生，
也不需要实现 `HumanResumable`。

### 10.4 不受影响的 provider

`CapabilityProvider` / `ToolCapabilityProvider` / `SkillCapabilityProvider` /
`AgentCapabilityProvider` / `MemoryProvider` / `KnowledgeProvider` / `LLMClient` /
`LLMClientResolver` / `EventStore` 的方法集**一个字没变**——只有导入路径变了（§2）。

从不问人的工具 provider、只返回 bool 的 authorizer，同样看不到任何 HITL 概念。这是
刻意的设计。

### 10.5 工具可以返图了（MCP 的行为变化，宿主要知情）

`CapabilityEvent(kind="result")` 的 `payload["content"]` 从 `str` 放宽成
**`str | list[ContentPart]`**——与 `user_prompt` / `send_message(content=)` /
`HitlReply.message` / `AuthorizationDecision.message` 同一个联合类型（§9.1）。要返图就
直接放进去：

```python
yield CapabilityEvent(kind="result", payload={"content": [
    TextPart(text="这是刚才那个页面的截图"),
    ImagePart(data=b64, media_type="image/png"),
]})
```

文本侧的既有加工（spill 落盘截断、`[Human note: …]` 前缀、事件 payload 脱敏）只作用于
content 里的文本 part——gateway 收到就拆开、加工完再拼回去，provider 不需要知道。

交 **inline base64 是允许的**：校验（media_type 白名单 / 单图 5 MiB）与外部化（写进
`MemoryBlobStore`、换成 `blob:<sha>`）由 gateway 统一做，provider 不必认识 blob store。
不合格的那张换成占位 `[image dropped: …]`，其余照走，**gateway 恒不抛**。

**对宿主的实际变化在 MCP 那条**：`MCPCapabilityProvider` 现在会把 MCP server 回的
`ImageContent` 带进上下文（此前是**静默丢弃**）。于是：

| 影响 | 说明 |
|---|---|
| token 账单 | 截图类 MCP 工具的返回值此前恒为 0 图；现在按 `image_tokens` 计（体积 ÷128，地板 1600）。装配期的预算与 compact 会跟着变。 |
| 模型 | 纯文本 adapter（`style="anthropic"` / `"openai"`）会把它们降级成 `[image {media_type}]` 并告警；要真看图得注册 `*-multimodal` adapter（§3.2）。 |
| blob | 没注册 `MemoryBlobStore` 时图以 inline base64 留在 memory 记录里（与不接 blob 的既有口径一致）；注册了则自动落 blob、记录里只留 ref。 |

不想要这个行为就在自己的 authorizer 里拦那个工具——core 侧没有单独的「工具返图」开关
（授权审查是本条明确未做的部分）。

**破坏性**：曾经短暂存在过一条 `metadata["content_parts"]` 侧信道（常量
`CONTENT_PARTS_KEY`），要求 provider 把文本与 part 分两处交。**已删除**，没有兼容期——
它是 `feat/multimodal` 分支内生死的内部机制，`master` 上不存在，故对从 `master` 迁移的
宿主没有影响。若你 fork 过分支中途的版本并写了用它的 provider：把 parts 挪进 `content`
即可，gateway 的组装顺序与结果一字未变。

---

## 11. 可执行的 grep 清单

按顺序在宿主仓库里搜，命中即按对应章节改。

**🔴 一定要改，不改起不来：**

```
CtxWeftRuntime(              # 位置传参 / hitl_manager= → §3
InMemoryEventStore(          # event_bus= 参数没了 → §3
runtime.event_store =        # 事后替换 event_store 不再工作 → §3
from ctx_weft.core.state     # 模块整体搬走 → §2
from ctx_weft.core.errors    # → core.models.errors → §2
from ctx_weft.core.events    # → protocols.events / providers.events → §2
from ctx_weft.core.auth      # → protocols.capability / providers.authorizer → §2
memory_blackboard            # → providers.memory → §2
RunHandle                    # → TurnHandle → §4
recover_session              # → recover_agent → §6.1
compact_session              # → compact_agent → §5.5
pause_task                   # 从公开面消失 → §5.3
hitl_manager                 # 整个删除 → §6.5
HitlManager  HitlRequest     # 类不存在了 → §6.5
def authorize(               # 签名去掉 agent/task → §10.1
.filter(                     # Authorizer.filter 删除 → §10.1
defer=True                   # → needs_human=HitlAsk(...) → §10.2
session_status_after_recover # 删除 → §6.4
ALTER TABLE events           # SQL 部署必须加 origin 列 → §8.4
```

**🟡 不改也能跑，但会静默错：**

```
SessionFinished              # 停发，靠它关流会永不关 → §8.2
RunFinished                  # 不能单独当关流判据 → §8.2
SessionStatusChanged         # 停发 → §8.2
BackgroundObserve            # 停发，改按 origin → §8.2
RecognizeIntentLLMPrompt     # 停发 → §8.2
HitlRequired  HitlApproved  HitlAnswered  HitlRejected  HitlModified  HitlCancelled
                             # 6 个旧 HITL 终态事件停发 → §8.2
payload["caller"]            # 键不存在，改读 Event.origin → §8.4
TaskSuspended                # 三义收窄到一义，按类型分流 → §8.4
final_status                 # RunFinished 上已废弃 → §8.4
run_id                       # task 事件上恒 None；不再跨轮复用 → §4.1 §8.4
sequence                     # task 事件上恒 0 → §8.4
"PAUSED"  "PAUSED_HITL"  "QUEUED"  "TIMEOUT"    # 值域收敛 → §8.4
llm_model                    # 真相源移到 agent record → §7
resume_hint  ResumeHint      # 删除，拆两条命令 → §7
HitlReply(                   # agent_id 成必填 → §6.5
root_agent_id                # 句柄上没有这个字段 → §4.4
created_at                   # AgentSummary/AgentDetail 现在有值了 → §8.3
```

---

## 12. 建议的迁移顺序

改动之间有依赖，按这个顺序做返工最少：

1. **导入路径**（§2）——纯机械替换，先让代码 import 得起来。
2. **接线**（§3）——构造函数 keyword-only、`event_store` 提前建好、注册 blob store。
   这一步做完宿主能起来。
3. **SQL DDL**（§8.4 的 `ALTER TABLE`）——不做的话第一条事件写入就报错。
4. **投影更新器**（§8.2 / §8.3）——加新分支、**保留**旧分支。这一步不做后面没法验证。
5. **关流判据**（§8.2）——`SessionFinished` → `wait_for_finish()` / task 终态事件。
   不做的话所有 SSE 连接泄漏，症状比编译错误难查。
6. **执行入口**（§4、§5）——句柄、`send_message`、`recover_agent`、`compact_agent`。
7. **HITL**（§6.5、§10.2、§10.3）——authorizer 与工具 provider 的重写。
8. **模型选择**（§7）——把换模型从续跑里拆出来。
9. **多模态**（§9）——纯增量，可以最后做；不做也不影响纯文本工作流。

---

## 13. 已知缺口（迁移时别踩，也别为它们写兜底）

这些是**当前分支已知未修**的，各自在专文里有账：

- **`TaskAwaitingHuman` / `TaskHumanResolved` 不是严格 1:1 配对**（两处漏发解除事件）。
  **不要拿这个配对做"这个 task 是否还卡着"的告警**，会假阳性。
- **`error_code` 不进投影**：跨重启后按码分流会退化成通用 `"interrupted"`。
- **非终态任务的中途产出不可恢复**：没有事件承载它。
- **`RunFinished.will_retry` 在熔断竞态下会说谎**：不要当唯一的关流判据。
- **`pause_session` / `cancel_session` 收尾窗口内对子 agent `send_message` 会静默丢消息**
  （§5.2）。
- **`SnapshotWriter` 的会话收尾快照分支已死**（靠 `SessionFinished` 触发）：周期性快照
  不受影响，但长驻进程里 `_since_snapshot` 会随会话数线性增长、不再回收。
- **三处 LLM 调用异常路径会孤儿化 `LLM_REQUEST_STARTED`**（`act` / `observe` /
  `compact`）：靠这对事件配对渲染"LLM 是否还在等"的 UI，异常路径下会一直显示请求中。

完整清单见 `docs/follow-ups/2026-09-03-outstanding-issues.md`。
