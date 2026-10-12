# 升级须知 · agent-centric 交互改造（2026-09-03）· **破坏性**

对应计划 `.superpowers/sdd/2026-09-03-agent-centric-interaction/`（22 个任务，
全部完成并逐个审查通过；本文是最终全分支审查后的交付清单）。核心变化：外部消息与
控制命令从此按 **agent** 显式寻址（`send_message`/`pause_agent`/`resume_agent`/
`cancel_agent`），不再隐式绑定「会话唯一活跃 task」；相应地，session 层的旧状态机
（`session_state.py`）整体退役，事件信封新增 `origin`，`EventStore` 的 SQL schema
随之变化。

**只有第 1 条会让宿主起不来，其余是语义变更 / 口径收紧，不做也能跑，但会有静默的
行为偏差或投影过期。**

---

## 🔴 1. SQL EventStore schema 变更：`events` 表新增 `origin` 列

`EventModel`（`src/ctx_weft/providers/events/store/sql/models.py`）新增：

```python
origin: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

`create_all` **不会**修改已存在的表——只对全新建表生效。存量 SQLite/PG 库若不手动
补这一列，**写入直接报错**（INSERT 缺列/多列不匹配，取决于你的建表/迁移工具）。

**必须执行**：

```sql
ALTER TABLE events ADD COLUMN origin VARCHAR(64);
```

`store/sql/models.py` 行内注释写着「存量行没有这一列 → 读侧回落 `""`，零迁移」——
这句话**只对读成立**：`_row_to_event` 确实会把 `NULL` 回落成空串（`row.origin if
row.origin is not None else ""`），历史行不需要回填。但对**写**不成立，新事件的
`INSERT` 必须能找到这一列。全新部署（`create_all` 建一张干净的表）不受影响。

---

## 🟡 2. 新增四个领域异常，`AGENT_NOT_FOUND` 现在真的会抛了

`ctx_weft.core.errors` 新增：

```
AgentBusyError        code = "AGENT_BUSY"
AgentTerminatedError  code = "AGENT_TERMINATED"
AgentNotRunningError  code = "AGENT_NOT_RUNNING"
```

`AgentNotFound`（`code = "AGENT_NOT_FOUND"`）此前已经声明，但**零调用点**——从未
被真正 `raise` 过；现在 `get_agent` / `send_message` / `pause_agent` /
`resume_agent` / `cancel_agent` 等一批方法在 agent 不存在时会真的抛它。

若 host 的错误分流是按 `CtxWeftError.code` 做的 switch/映射表，这四个码要么已经
在表里能命中一个合理的兜底分支，要么需要显式加分支——否则会落进「未知错误码」的
兜底路径（不一定是 crash，但用户提示文案大概率不对）。

对应的方法级行为：
- `send_message` 到不存在 / `terminated` / `running` 的 agent：分别抛
  `AgentNotFound` / `AgentTerminatedError` / `AgentBusyError`，**不排队**，调用方
  自行重试或先 pause/cancel。
- `pause_agent` 到非 `running` 的 agent：抛 `AgentNotRunningError`。

---

## 🟡 3. `HitlReply` 新增必填字段 `agent_id`

```python
@dataclass
class HitlReply:
    hitl_id: str
    outcome: HitlOutcome
    agent_id: str  # 新增，必填
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None
```

这是一条**防呆**校验，不参与路由（路由仍全靠 `hitl_id`，全局唯一）：调用方必须
显式声明「我以为在回复哪个 agent」，与 `PendingHitl.agent_id` **严格相等**——
**空串也须对上**（若该请求记录的 `agent_id` 恰好是空串，`HitlReply.agent_id` 也
必须传空串，传别的值一样会被拒）。不符会被 `reply_to_hitl` 拒绝，不再是「按
`hitl_id` 静默走掉」。

host 侧所有构造 `HitlReply(...)` 的地方都要补上这个字段——旧代码不传会在类型/
运行期直接报错（dataclass 必填字段缺失）。

---

## 🟡 4. `Event` 信封新增 `origin`；不要再读 `payload["caller"]`

```python
@dataclass
class Event:
    ...
    origin: str = ""  # 哪个组件发出的，见 docs/events-v2.md §4
```

若 host 按早期设计文档准备读 `payload["caller"]` 来判断「这条事件是谁发的」——
那个键**不存在**，会读到空。改读 `Event.origin`（顶层字段，不在 `payload` 里）。
存量事件（写入时还没有这一列）读出空串，不是 `None`、不报错。

---

## 🟡 5. 9 个事件类型停发进 L 档，新增 5 个 `AGENT_*`

**停发（转入 `EventType.L_TIER_EVENT_TYPES`，reducer 仍读它们重放存量日志，但
不会再有新事件）**：

```
BackgroundObserveRequestStarted   BackgroundObservePromptSent
BackgroundObserveTokenStreamed    BackgroundObserveResponseFinished
RecognizeIntentLLMPrompt
SessionRunning   SessionWaiting   SessionInterrupted   SessionFinished
```

前 4 个原因：`run_observe_react` 统一改发通用 `LLM_*` 事件（靠 `origin` 区分
前台/后台），不再发这层 step 专属镜像事件。第 5 个原因：`recognize_intent.py`
切到 `stream_llm_resilient` 后，通用 `LLM_PROMPT_SENT`（`origin=
loop.recognize_intent`）取代了它。后 4 个原因：会话状态机（`session_state.py`）
整体退役，没有组件再发它们。

若 host 依赖 `BackgroundObserve*` / `RecognizeIntentLLMPrompt` 做前端分流（比如
「这条 token 是不是后台观察产生的」），**必须改按 `origin` 前缀匹配**——具体前缀
见 `docs/events-v2.md` §4 与各发射点的 `make_event`/`ctx.event_bus.emit` 调用。

**新增（agent 五态机的状态转移事实，`AgentLifecycleManager.apply_input` 唯一入口
发出）**：

```
AgentRunning   AgentIdle   AgentWaitingHuman   AgentInterrupted   AgentTerminated
```

（`AgentInstantiated` / `AgentSpawned` / `AgentLlmChanged` 是更早一批任务
（agent-llm-ownership，见 `docs/upgrade/2026-09-02-agent-llm-ownership.md`）已经
上线的，不在本次新增范围内。）

**host 侧 `projection_updater.py` 须自行补这 5 个 `AGENT_*` 的折叠分支**——core
的 reducer（`control/reducers.py`）不与 host 的投影更新器共享代码，两边各自维护
一份折叠表。缺哪个分支，host 投影上该 agent 的状态就会在那类事实到达后**安静地**
过期，不报错。

---

## 🟡 6. `RunSnapshot` 的 agent 条目新增 `status` / `current_task_id`

`serialize_view` 序列化 `state_blob["agents"]` 时新增这两个字段（`reducers.py`
第 211-212 行一带）。**向后兼容**：旧快照读回时走 `.get("current_task_id")` 这类
带默认值的取法，不会因为缺字段报错——只是那些字段在老快照上是 `None`/空，直到
下一次基于新代码打的快照覆盖。host 若自己也解析 `state_blob` 结构，可以选择性地
读这两个新字段，不读也不影响原有行为。

---

## 🟡 7. `AgentSummary.created_at` / `AgentDetail.created_at` 恒为 `None`

`list_agents` / `get_agent` 构造这两个视图对象时都没有传 `created_at`——底层
`_AgentRecord` 本就不携带这个字段，构造点也没有补。**host 不要依赖它排序或展示
"agent 创建时间"**，它现在只是 dataclass 默认值。若确实需要这个信息，改读
`AgentInstantiated`/`AgentSpawned` 事件自己的 `timestamp`。

---

## 🟡 8. `RunHandle.agent_id` 就是 root agent id；**没有**新增 `root_agent_id` 字段

早期设计文档提过要给 `RunHandle` 加一个独立的 `root_agent_id` 字段，实际未做
（裁定见 `RunHandle` 自身 docstring）。`start_session` 返回的 `handle.agent_id`
本身就是该 session 的 root agent（`parent_agent_id is None`），非空、可直接传给
`send_message(agent_id, ...)` / `get_agent(agent_id)`。若 host 代码在等一个
`handle.root_agent_id`，改读 `handle.agent_id`。

---

## 🟡 9. 已知行为差异：`send_message` 到暂停窗口内的子 agent，消息会静默丢失

`cancel_session`/`pause_session` 级联收尾时有一个短暂的 `_pausing` 窗口
（`CtxWeftRuntime._pausing`，`_register_run_tokens` 按 root/非 root 分流出生
信号：非 root run **出生即协作取消**，M-1）。若这个窗口内对某个**非 root**
子 agent 调用 `send_message` 且路由到「新建 task」分支（`_start_task_for_agent`
→ `push_task`），返回的 `task_id` 对应的 run 会在真正开跑前被 born-cancel——
调用方拿到一个 `task_id`，但这条消息实际上**不会被处理**，也不会有显式的失败
事实告诉调用方。host 若需要强一致的送达确认，暂时不要在 `pause_session`/
`cancel_session` 收尾窗口内对子 agent 发消息，或自行加超时轮询兜底。

---

## 内部变更（host 无需动作）

以下几条曾在本文档草稿里按「破坏性变更」列出，复核后发现判据用错了：判断标准是
「是否出现在 `CtxWeftRuntime` 公开方法签名里，或 host 是否必须构造/捕获它」，而
不是「这个类/模块是不是 core 内部的组织重心」。以下几条都够不到这条线——host 除了
`CtxWeftRuntime`（`ctx_weft/__init__.py` 唯一导出的运行时入口）之外拿不到、也传不
进任何一个内部对象，因此对 host 完全无感，记账用，不要求 host 做任何改动。

### `session_state.py` 整体删除

会话运行态状态机文件已删，两个此前从那里 import 的常量搬到了
`ctx_weft.core.state.models`：

```python
# 旧（从未是公开 API 的一部分）
from ctx_weft.core.session_state import TERMINAL_SESSION_STATUSES, WAITING
# 新
from ctx_weft.core.state.models import TERMINAL_SESSION_STATUSES, WAITING
```

`TERMINAL_SESSION_STATUSES` / `WAITING` 从未从 `ctx_weft` 顶层 `__init__.py` 导出，
也不出现在 `CtxWeftRuntime` 任何公开方法的签名里——host 需要的等价信息（会话是否
仍在等待）一直是通过 `session_status_after_recover(session_id) -> str` 这个只读
入口拿字符串，不需要（也从未被支持）直接 import 这两个内部常量。除非 host 代码
越过公开契约直接伸手进 `ctx_weft.core.session_state`（不受支持的用法），否则这条
改动不可见。

### `SessionManager`（现 `SessionRegistry`）删除三个方法：`status_of` / `is_terminal` / `cancel`

这三者原先是 session 状态机时代的读/写入口；agent-centric 改造后被删除。但
`SessionManager`/`SessionRegistry` 本身从未是 `ctx_weft` 的公开导出（`core/
orchestrator/__init__.py` 的 `__all__` 只是包内组织边界，不是对外 API 边界），host
拿不到这个类的实例，也就无从调用它的任何方法——这三个方法的删除对 host 不可能
造成任何影响。等价能力（会话是否终结、如何取消）host 一直是通过
`CtxWeftRuntime.get_agent(root_agent_id)` / `CtxWeftRuntime.cancel_session
(session_id)` 这类公开方法拿到的，删除前后这条路径没有变化。

### 类改名：`AgentRegistry` → `AgentLifecycleManager`，`SessionManager` → `SessionRegistry`

这两个类改了名字（连带模块文件 `agent_registry.py` → `agent_lifecycle_manager.py`、
`session_manager.py` → `session_registry.py`），理由是名字要跟上改造后的实际职责：
`AgentRegistry` 早已从被动登记表变成持五态机、发 `AGENT_*` 事件的生命周期管理者；
`SessionManager` 的状态机整体删除后只剩会话登记这一件事。两个类都不出现在
`ctx_weft/__init__.py` 的导出列表里，也都不出现在 `CtxWeftRuntime` 任何公开方法的
参数或返回类型里——纯 core 内部重构，host 不需要改任何代码，也不会在运行期观察到
任何差异。

---

## 已知遗留（不阻塞本次上线，记账用）

### (a) `SnapshotWriter` 的会话收尾快照分支已死；`_since_snapshot` 因此无界增长

`SnapshotWriter.on_event`（`src/ctx_weft/providers/events/snapshot.py`）靠
`event.type == "SessionFinished"` 触发「会话终态补一张快照」的分支——该事件已随
第 5 节的 L 档化停止发射，这个分支永久不可达（不抛错、不误写，直接掉进下面的
周期性快照计数分支）。

**周期性快照不受影响**（`RunFinished` 每累计 50 条非瞬态事件一张，逻辑独立）。
实际损失只是「会话收尾那一张」，属轻微性能退化——下次崩溃恢复会多重放最多 50 条
事件，不是正确性问题。

但那条死分支里的 `self._since_snapshot.pop(session_id, None)` 是这个
`dict[str, int]` **唯一的清理点**。随它一起失效意味着：**长驻进程里，每个跑过的
session 会在 `_since_snapshot` 里永久留一条 `str → int` 记录，不再被回收**——
一个随会话数线性增长、永不缩小的内存占用。对短生命周期进程（每次重启即清空）不
构成问题；对长驻宿主，量级取决于会话吞吐，需要自行评估是否值得现在就修。后续把
触发信号换成 `TaskQueueDrained`（真正的会话级完成信号）时须一并处理这个清理点。

### (b) 三处 LLM 调用异常路径会孤儿化 `LLM_REQUEST_STARTED`

`act.py` / `observe.py` / `compact.py` 三处：`LLM_REQUEST_STARTED`/
`LLM_PROMPT_SENT` 由 gateway 在调用**之前**发出，收尾的 `LLM_RESPONSE_FINISHED`
由这三处各自在流式响应正常结束后补发。若 LLM 调用本身抛异常（网络错误、超时、
provider 侧错误），流式循环提前退出，`LLM_RESPONSE_FINISHED` 补发那行代码不会
被执行到——留下一条「有始无终」的孤儿事件。

`recognize_intent.py` 已经修过同一个问题（`except` 分支里显式补发
`LLM_RESPONSE_FINISHED`，见该文件 209-236 行一带的内联注释），可以直接作为另外
三处的修复模板。

后果**只是可观测性瑕疵**：host 若靠这对事件配对渲染「LLM 是否还在等」的 UI 状态，
异常路径下会一直显示"请求中"，直到会话别的信号纠正过来；不影响 task/session 的
正确性推进（错误处理走的是各自 step 的既有异常/重试/失败路径，与这对事件配对
无关）。

---

## 判断你是否受影响

搜这些字符串，命中即需要检查：

```
HitlReply(                  payload["caller"]
BackgroundObserveRequestStarted   RecognizeIntentLLMPrompt
SessionRunning              SessionWaiting           SessionFinished
root_agent_id               created_at
```

- 命中 `HitlReply(` 但没传 `agent_id` → 见第 3 节，补上这个必填字段。
- 命中 `payload["caller"]` → 见第 4 节，改读 `Event.origin`。
- 命中 `BackgroundObserveRequestStarted` / `RecognizeIntentLLMPrompt` /
  `SessionRunning` 等 → 见第 5 节，这些事件已停发，改按 `origin` 分流或对接新的
  `AGENT_*`。
- 命中 `handle.root_agent_id` → 见第 8 节，没有这个字段，改用 `handle.agent_id`。
- 命中对 `AgentSummary`/`AgentDetail.created_at` 的排序/展示逻辑 → 见第 7 节。
- SQL 部署（非全新建库）→ 必须执行第 1 节的 `ALTER TABLE`，这是唯一会让宿主
  启动即报错的一条。

以下字符串即使命中也**不需要 host 改代码**（详见「内部变更（host 无需动作）」
一节）：`session_state`、`session_manager.status_of(`、`.is_terminal(`、
`session_manager.cancel(`、`AgentRegistry`、`SessionManager`——除非你的代码确实
越界直接 import 了 core 内部模块（不受支持的用法），否则这些改动对走公开 API 的
host 不可见。
