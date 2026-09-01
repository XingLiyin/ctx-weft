# HITL 重设计 · 段 1（纯新增）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成新 HITL 子系统的全部**离线部件**（契约类型、内存状态机、事实发布、双读折叠），不接入任何执行路径——跑完本计划，仓库行为逐字节不变。

**Architecture:** 新增 `core/hitl/` 自足子系统（Registry 纯同步状态机 + Service 发事实 + ReplyIntake 校验前置），新契约类型追加进 `protocols/hitl.py` 与既有 legacy 类型并存，双读折叠函数放进既有的 `core/control/reducers.py`。旧 `HitlManager` 一行不动，段 2 才替换。

**Tech Stack:** Python 3.11+、dataclasses、pytest（`asyncio_mode = "auto"`，`async def test_*` 无需 marker）、ruff（line-length 100）。不引入任何新依赖。

**Spec:** `docs/superpowers/specs/2026-09-01-hitl-redesign-design.md`

## Global Constraints

- **本段不得改变任何现有行为。** 旧 `HitlManager`、`capability_gateway`、`act`、`runtime` 一行不改。全部现存测试必须继续通过。
- **`protocols/` 零 core 依赖**：新类型只能 import 标准库与 `protocols` 内部模块；`ContentPart` 用 `TYPE_CHECKING` 前置引用（沿用该文件既有写法）。
- **`core/hitl/` 不得 import `core.loop` 或 `core.runtime`**，包括函数体内的延迟 import。这是本设计的核心不变式之一，段 1 就要立住。
- **`HitlRegistry` 全同步、无 `await`。** 单线程 asyncio 下无 await 即原子，因此**不需要锁**。所有 I/O 归 `HitlService`。
- ruff line-length = 100；`from __future__ import annotations` 置顶（沿用全仓写法）。
- 新事件类型必须登记进 `EventType` 枚举（`EVENT_TYPES` 由 `frozenset(EventType)` 自动派生）。
- 命名锁定：spec 中对外只读视图一词落实为 **`HitlRequestView`**——`HitlRequest` 这个名字在段 2 删除 legacy 类型之前被占用，且新名字本就更准确。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `src/ctx_weft/protocols/hitl.py`（修改） | **追加**新契约类型；legacy `HitlRequest` 等原样保留 |
| `src/ctx_weft/protocols/events.py`（修改） | 追加 `HITL_OPENED` / `HITL_RESOLVED` 两个事件类型 |
| `src/ctx_weft/core/hitl/__init__.py`（新建） | 子系统导出面 |
| `src/ctx_weft/core/hitl/registry.py`（新建） | `PendingHitl` / `WaitSlot` / `HitlRegistry`——纯同步内存状态机 |
| `src/ctx_weft/core/hitl/reply_intake.py`（新建） | `ReplyIntake`——应答内容校验 + 双侧外部化，构造期注入 |
| `src/ctx_weft/core/hitl/service.py`（新建） | `HitlService`——open / resolve / cancel，唯一漏斗，发事实 |
| `src/ctx_weft/core/hitl/snapshot.py`（新建） | `HitlSnapshot` 数据类型 |
| `src/ctx_weft/core/control/reducers.py`（修改） | 追加 `fold_hitl_snapshot`——双读折叠（新旧两套事件） |
| `tests/unit/test_hitl_contract_types.py`（新建） | Task 1 |
| `tests/unit/test_hitl_registry.py`（新建） | Task 3 |
| `tests/unit/test_hitl_service.py`（新建） | Task 4 |
| `tests/unit/test_hitl_fold_snapshot.py`（新建） | Task 5 |
| `tests/unit/test_hitl_registry_load.py`（新建） | Task 6 |

---

## Task 1: 新契约类型

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py`（在文件末尾追加，不改动既有内容）
- Test: `tests/unit/test_hitl_contract_types.py`

**Interfaces:**
- Consumes: 无
- Produces: `Delivery`（= `ToolResultDelivery | UserTurnDelivery | NoResumeDelivery`）、`PREFACE_NORMAL/PREFACE_AFTER_INTERRUPT/PREFACE_AFTER_INTERRUPT_EDIT`、`HitlAsk`、`HitlDecision`、`ResumeHint`、`HitlReply`、`HitlRequestView`

- [ ] **Step 1: 写失败的测试**

创建 `tests/unit/test_hitl_contract_types.py`：

```python
"""新 HITL 契约类型（段 1 · 纯新增）。"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.protocols.hitl import (
    PREFACE_AFTER_INTERRUPT_EDIT,
    HitlAsk,
    HitlDecision,
    HitlReply,
    HitlRequestView,
    NoResumeDelivery,
    ResumeHint,
    ToolResultDelivery,
    UserTurnDelivery,
)


def test_delivery_variants_are_frozen_and_carry_their_target():
    tr = ToolResultDelivery(tool_call_id="call_1")
    ut = UserTurnDelivery(task_id="tsk_1", preface=PREFACE_AFTER_INTERRUPT_EDIT)
    nr = NoResumeDelivery()
    assert tr.tool_call_id == "call_1"
    assert (ut.task_id, ut.preface) == ("tsk_1", "interrupt_edit")
    assert nr == NoResumeDelivery()          # 无字段，值相等


def test_user_turn_preface_defaults_to_normal():
    assert UserTurnDelivery(task_id="tsk_1").preface == "normal"


def test_ask_defaults_keep_optional_slots_empty():
    ask = HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id="call_1"))
    assert ask.prompt == "" and ask.detail == "" and ask.subject_id == ""
    assert ask.fields == [] and ask.proposal is None
    assert ask.resume_state is None
    assert ask.reply_as_result is False


def test_ask_fields_are_not_shared_between_instances():
    a, b = HitlAsk(form="q", delivery=NoResumeDelivery()), HitlAsk(
        form="q", delivery=NoResumeDelivery())
    a.fields.append({"name": "x"})
    assert b.fields == []


def test_decision_defaults_to_empty_message_and_no_modified_arguments():
    d = HitlDecision(outcome="accepted")
    assert d.message == "" and d.modified_arguments is None


def test_reply_carries_resume_hint_separate_from_the_request():
    r = HitlReply(hitl_id="hit_1", outcome="accepted",
                  resume_hint=ResumeHint(llm_account="acc", llm_model="m"))
    assert (r.resume_hint.llm_account, r.resume_hint.llm_model) == ("acc", "m")


def test_reply_resume_hint_defaults_to_empty_hint():
    r = HitlReply(hitl_id="hit_1", outcome="accepted")
    assert r.resume_hint.llm_account is None and r.resume_hint.llm_model is None


def test_view_resolved_is_derived_from_outcome():
    now = datetime.now(UTC)
    pending = HitlRequestView(id="hit_1", form="approval", session_id="s1",
                              task_id="t1", created_at=now)
    done = HitlRequestView(id="hit_2", form="approval", session_id="s1",
                           task_id="t1", created_at=now, outcome="accepted")
    assert pending.resolved is False
    assert done.resolved is True


def test_view_has_no_tool_call_id_field():
    """tool_call_id 是 core 的幂等键，不进对外视图（spec §4）。"""
    assert not hasattr(
        HitlRequestView(id="hit_1", form="approval", session_id="s1", task_id="t1",
                        created_at=datetime.now(UTC)),
        "tool_call_id",
    )
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_contract_types.py -v`
Expected: FAIL —— `ImportError: cannot import name 'HitlAsk' from 'ctx_weft.protocols.hitl'`

- [ ] **Step 3: 追加实现**

在 `src/ctx_weft/protocols/hitl.py` **末尾追加**（既有内容一行不改）：

```python
# ══════════════════════════════════════════════════════════════════════════════
# 新契约（2026-09-01 重设计 · 段 1）。与上方 legacy 类型并存，段 2 删除 legacy。
# 设计：docs/superpowers/specs/2026-09-01-hitl-redesign-design.md §4 / §5
# ══════════════════════════════════════════════════════════════════════════════

#: `UserTurnDelivery.preface`：注入用户回合时的续接修饰。取代 legacy 的
#: `context` 字符串 sniffing（"plain_text" / "interrupt" / "interrupt:edit"）。
PREFACE_NORMAL = "normal"
PREFACE_AFTER_INTERRUPT = "interrupt"
PREFACE_AFTER_INTERRUPT_EDIT = "interrupt_edit"


@dataclass(frozen=True)
class ToolResultDelivery:
    """决定作为该 tool_call 的结果送达 → 热路径就地重入 / 冷路径 reconcile 精确重入。"""

    tool_call_id: str


@dataclass(frozen=True)
class UserTurnDelivery:
    """决定作为一条 user 消息注入任务对话 → 置 PENDING 重排。"""

    task_id: str
    preface: str = PREFACE_NORMAL


@dataclass(frozen=True)
class NoResumeDelivery:
    """纯通知 / 取消，不续跑。"""


#: **封闭值域**——与开放的 `form` 正交（spec §5）。host 可以定义新的等待形态，
#: 但不能定义新的回灌方式；因此续跑路由的每个取值 core 都认识、都有确定行为。
Delivery = ToolResultDelivery | UserTurnDelivery | NoResumeDelivery


@dataclass
class HitlAsk:
    """「我需要一个人的决定」——provider 产出的纯意图。provider 唯一需要构造的类型。

    展示槽位（prompt / detail / fields / proposal）是**通用**的：因为 form 是开放值域，
    不可能做穷举的 tagged union，host 自定义 form 复用同一组槽位。私有语义不得混进来。
    """

    form: str
    delivery: Delivery
    prompt: str = ""                                  # 给人看的主问题
    detail: str = ""                                  # 展示用补充说明
    fields: list[dict[str, Any]] = field(default_factory=list)   # 结构化提问
    proposal: dict[str, Any] | None = None            # 被门控的参数（approval 用）
    subject_id: str = ""                              # 被门控的能力 id（展示与审计）
    #: 不透明续跑载荷：core 原样保存、重入时原样回传，**永不解读**。必须可序列化。
    resume_state: dict[str, Any] | None = None
    #: True = 人的答复直接作工具结果，重入不发生（`ask_user` 走这条）。
    reply_as_result: bool = False


@dataclass
class HitlDecision:
    """「人给了什么」——core 喂给发起方的结果。无 id、无时间、无 session。"""

    outcome: HitlOutcome
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResumeHint:
    """应答时携带的当前所选模型。属于**这一次应答**，不属于这个请求——故不入事件、不入状态。"""

    llm_account: str | None = None
    llm_model: str | None = None


@dataclass
class HitlReply:
    """host → core 的一次应答命令。"""

    hitl_id: str
    outcome: HitlOutcome
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None
    resume_hint: ResumeHint = field(default_factory=ResumeHint)


@dataclass
class HitlRequestView:
    """core → host 的只读视图：渲染 UI 与 pending 列表用。

    刻意**不含** `tool_call_id`——那是 core 的幂等键，host 不需要（spec §4）。
    """

    id: str
    form: HitlForm
    session_id: str
    task_id: str
    created_at: datetime
    agent_id: str = ""
    subject_id: str = ""
    prompt: str = ""
    detail: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    outcome: HitlOutcome = ""
    resolved_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        """推导而非存储——存两份就有一条要维护的不变量，而漏维护是静默的。"""
        return bool(self.outcome)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_contract_types.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 确认零回归**

Run: `uv run pytest tests/unit -q && uv run ruff check src/ctx_weft/protocols/hitl.py`
Expected: 全部通过，ruff 无告警

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/hitl.py tests/unit/test_hitl_contract_types.py
git commit -m "feat(hitl): 新契约类型（Ask/Decision/Reply/View/Delivery），与 legacy 并存"
```

---

## Task 2: 登记两个新事件类型

**Files:**
- Modify: `src/ctx_weft/protocols/events.py:140-147`（HITL 域枚举块）
- Modify: `docs/spec/01-events.md`（冻结清单需同步，见 Step 3）
- Test: `tests/unit/test_hitl_contract_types.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `EventType.HITL_OPENED`（值 `"HitlOpened"`）、`EventType.HITL_RESOLVED`（值 `"HitlResolved"`）

- [ ] **Step 1: 写失败的测试**

在 `tests/unit/test_hitl_contract_types.py` 末尾追加：

```python
def test_new_hitl_event_types_are_registered():
    from ctx_weft.protocols.events import EVENT_TYPES, EventType

    assert EventType.HITL_OPENED == "HitlOpened"
    assert EventType.HITL_RESOLVED == "HitlResolved"
    # EVENT_TYPES 由 frozenset(EventType) 派生，登记即自动生效
    assert "HitlOpened" in EVENT_TYPES and "HitlResolved" in EVENT_TYPES


def test_legacy_hitl_event_types_still_registered():
    """段 1 不删旧事件——双读折叠仍要认它们（spec §12.3）。"""
    from ctx_weft.protocols.events import EventType

    for name in ("HitlRequired", "HitlApproved", "HitlModified",
                 "HitlAnswered", "HitlRejected", "HitlCancelled"):
        assert name in {e.value for e in EventType}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_contract_types.py -k event_types -v`
Expected: FAIL —— `AttributeError: HITL_OPENED`

- [ ] **Step 3: 追加事件类型**

在 `src/ctx_weft/protocols/events.py` 的 HITL 域枚举块（`HITL_CANCELLED` 那行之后）追加：

```python
    # ── HITL v2（2026-09-01 重设计）──
    # outcome 是事实本身，不再由事件类型编码结局：approved vs modified 由
    # payload 有无 modified_arguments 推出，其余由 outcome 推出。host 自定义
    # outcome 因此无需新增事件类型。上方 6 个 legacy HITL 事件在段 3 才退役。
    HITL_OPENED = "HitlOpened"
    HITL_RESOLVED = "HitlResolved"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_contract_types.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 同步冻结清单文档**

在 `docs/spec/01-events.md` 的事件清单里追加两行（照该文件既有表格格式），并注明：

```
| HitlOpened   | HITL 请求登记（取代 HitlRequired + SessionPausedHitl） | 2026-09-01 |
| HitlResolved | HITL 请求终局（取代 5 个 resolve 事件）                | 2026-09-01 |
```

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/events.py docs/spec/01-events.md tests/unit/test_hitl_contract_types.py
git commit -m "feat(hitl): 登记 HitlOpened / HitlResolved 两个事件类型"
```

---

## Task 3: HitlRegistry —— 纯同步内存状态机

**Files:**
- Create: `src/ctx_weft/core/hitl/__init__.py`
- Create: `src/ctx_weft/core/hitl/registry.py`
- Test: `tests/unit/test_hitl_registry.py`

**Interfaces:**
- Consumes: Task 1 的 `HitlAsk` / `HitlDecision` / `HitlRequestView` / `Delivery`
- Produces:
  - `WaitSlot`（Protocol，单方法 `deliver(decision: HitlDecision) -> bool`）
  - `PendingHitl`（dataclass；`.resolved` 属性；`.to_view() -> HitlRequestView`）
  - `HitlRegistry`：
    - `open(ask, *, hitl_id, session_id, task_id, agent_id, tool_call_id, created_at) -> PendingHitl`
    - `get(hitl_id) -> PendingHitl | None`
    - `find_for_tool_call(tool_call_id) -> PendingHitl | None`
    - `decision_for(tool_call_id) -> tuple[HitlDecision, dict | None] | None`
    - `list_pending(session_id=None) -> list[PendingHitl]`
    - `attach_slot(hitl_id, slot) -> None` / `detach_slot(hitl_id) -> None`
    - `resolve(hitl_id, decision, resolved_at) -> tuple[PendingHitl, WaitSlot | None] | None`
    - `gc() -> None`

- [ ] **Step 1: 写失败的测试**

创建 `tests/unit/test_hitl_registry.py`：

```python
"""HitlRegistry：纯同步内存状态机（段 1）。

全同步、无 await —— 单线程 asyncio 下无 await 即原子，故不需要锁。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.protocols.hitl import (
    HitlAsk,
    HitlDecision,
    ToolResultDelivery,
    UserTurnDelivery,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class FakeSlot:
    """WaitSlot 测试替身：记录是否被投递过。"""

    def __init__(self, accepts: bool = True) -> None:
        self.accepts = accepts
        self.delivered: HitlDecision | None = None

    def deliver(self, decision: HitlDecision) -> bool:
        self.delivered = decision
        return self.accepts


def _ask(tool_call_id: str = "call_1") -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id=tool_call_id),
                   prompt="Allow bash?", subject_id="fs:bash_exec",
                   proposal={"command": "ls"})


def _open(reg: HitlRegistry, hitl_id: str = "hit_1", tool_call_id: str = "call_1",
          at: datetime = T0) -> PendingHitl:
    return reg.open(_ask(tool_call_id), hitl_id=hitl_id, session_id="s1", task_id="t1",
                    agent_id="a1", tool_call_id=tool_call_id, created_at=at)


def test_open_registers_a_pending_request():
    reg = HitlRegistry()
    req = _open(reg)
    assert req.id == "hit_1" and req.resolved is False
    assert reg.get("hit_1") is req
    assert [r.id for r in reg.list_pending()] == ["hit_1"]


def test_open_is_idempotent_by_tool_call_id():
    """同 tool_call_id 再次 open 复用既有请求，不新建（spec §10）。"""
    reg = HitlRegistry()
    first = _open(reg, hitl_id="hit_1")
    again = _open(reg, hitl_id="hit_2")          # 不同 id，同 tool_call
    assert again is first
    assert reg.get("hit_2") is None


def test_open_with_empty_tool_call_id_always_creates_a_new_request():
    """空 tool_call_id 不是幂等键（UserTurn 的 park 就没有 tool_call）。"""
    reg = HitlRegistry()
    a = reg.open(HitlAsk(form="wait", delivery=UserTurnDelivery(task_id="t1")),
                 hitl_id="hit_1", session_id="s1", task_id="t1", agent_id="a1",
                 tool_call_id="", created_at=T0)
    b = reg.open(HitlAsk(form="wait", delivery=UserTurnDelivery(task_id="t1")),
                 hitl_id="hit_2", session_id="s1", task_id="t1", agent_id="a1",
                 tool_call_id="", created_at=T0)
    assert a is not b


def test_resolve_transitions_and_returns_the_slot():
    reg = HitlRegistry()
    _open(reg)
    slot = FakeSlot()
    reg.attach_slot("hit_1", slot)
    result = reg.resolve("hit_1", HitlDecision(outcome="accepted", message="ok"), T0)
    assert result is not None
    req, taken = result
    assert req.resolved is True and req.decision.message == "ok"
    assert taken is slot
    assert reg.get("hit_1").slot is None          # 槽被取走，不可二次投递


def test_resolve_is_idempotent_and_returns_none_the_second_time():
    """已终局再 resolve = no-op，不二次转移、调用方据此不重发事实（spec §10）。"""
    reg = HitlRegistry()
    _open(reg)
    assert reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0) is not None
    assert reg.resolve("hit_1", HitlDecision(outcome="rejected"), T0) is None
    assert reg.get("hit_1").decision.outcome == "accepted"


def test_resolve_unknown_id_returns_none():
    assert HitlRegistry().resolve("nope", HitlDecision(outcome="accepted"), T0) is None


def test_resolved_request_leaves_the_pending_list():
    reg = HitlRegistry()
    _open(reg)
    reg.resolve("hit_1", HitlDecision(outcome="accepted"), T0)
    assert reg.list_pending() == []


def test_list_pending_filters_by_session():
    reg = HitlRegistry()
    _open(reg, hitl_id="hit_1", tool_call_id="call_1")
    reg.open(_ask("call_2"), hitl_id="hit_2", session_id="s2", task_id="t2",
             agent_id="a1", tool_call_id="call_2", created_at=T0)
    assert [r.id for r in reg.list_pending(session_id="s2")] == ["hit_2"]


def test_decision_for_returns_decision_and_resume_state_as_a_pair():
    """冷路径重入调 resume(decision, resume_state)——只给决定就得重做让出前的工作。"""
    reg = HitlRegistry()
    ask = HitlAsk(form="question", delivery=ToolResultDelivery(tool_call_id="call_1"),
                  resume_state={"plan": "deploy-7"})
    reg.open(ask, hitl_id="hit_1", session_id="s1", task_id="t1", agent_id="a1",
             tool_call_id="call_1", created_at=T0)
    reg.resolve("hit_1", HitlDecision(outcome="accepted", message="go"), T0)
    got = reg.decision_for("call_1")
    assert got is not None
    decision, resume_state = got
    assert decision.message == "go" and resume_state == {"plan": "deploy-7"}


def test_decision_for_returns_none_while_still_pending():
    """内存 pending = 活的等待，不得被当成「已答过」。"""
    reg = HitlRegistry()
    _open(reg)
    assert reg.decision_for("call_1") is None


def test_decision_for_empty_or_unknown_tool_call_id_is_none():
    reg = HitlRegistry()
    _open(reg)
    assert reg.decision_for("") is None
    assert reg.decision_for("other") is None


def test_gc_trims_oldest_resolved_and_never_touches_pending():
    reg = HitlRegistry(max_resolved=1)
    for i in (1, 2):
        reg.open(_ask(f"call_{i}"), hitl_id=f"hit_{i}", session_id="s1", task_id="t1",
                 agent_id="a1", tool_call_id=f"call_{i}", created_at=T0)
        reg.resolve(f"hit_{i}", HitlDecision(outcome="accepted"),
                    T0 + timedelta(seconds=i))
    reg.open(_ask("call_3"), hitl_id="hit_3", session_id="s1", task_id="t1",
             agent_id="a1", tool_call_id="call_3", created_at=T0)
    reg.gc()
    assert reg.get("hit_1") is None               # 最旧的已终局项被裁剪
    assert reg.get("hit_2") is not None
    assert reg.get("hit_3") is not None           # pending 永不裁剪


def test_to_view_projects_the_host_facing_fields():
    reg = HitlRegistry()
    req = _open(reg)
    view = req.to_view()
    assert (view.id, view.form, view.session_id, view.task_id) == (
        "hit_1", "approval", "s1", "t1")
    assert view.subject_id == "fs:bash_exec" and view.proposal == {"command": "ls"}
    assert view.outcome == "" and view.resolved is False


def test_to_view_reflects_the_decision_after_resolve():
    reg = HitlRegistry()
    _open(reg)
    reg.resolve("hit_1", HitlDecision(outcome="rejected"), T0)
    view = reg.get("hit_1").to_view()
    assert view.outcome == "rejected" and view.resolved is True and view.resolved_at == T0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_registry.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ctx_weft.core.hitl'`

- [ ] **Step 3: 实现**

创建 `src/ctx_weft/core/hitl/__init__.py`：

```python
"""core/hitl：HITL 的自足子系统。

**不 import `core.loop`、不 import `core.runtime`**，包括函数体内的延迟 import——
这是本设计的核心不变式：编排层不认识协程栈，park 只属于 loop（spec §3）。
"""

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl, WaitSlot

__all__ = ["HitlRegistry", "PendingHitl", "WaitSlot"]
```

创建 `src/ctx_weft/core/hitl/registry.py`：

```python
"""HitlRegistry：HITL 的纯内存状态机。

**全同步、无 await。** 单线程 asyncio 下，一段没有 await 的代码原子执行，因此
「状态转移 + 取走等待槽」天然互斥，不需要锁——热投递与冷续跑的单一权威转移由
此保证（spec §6）。所有 I/O（发事实、外部化内容）归 `HitlService`。

**完备即构造**：本类的一切查询只读自己内存，绝不回落去查存储。恢复期的完备性
由装填（`load_snapshot`，Task 6）承担（spec §3.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ctx_weft.protocols.hitl import (
    Delivery,
    HitlAsk,
    HitlDecision,
    HitlRequestView,
)


class WaitSlot(Protocol):
    """热等待的会合槽——**只有一个操作**的不透明句柄。

    刻意保持最小：registry 因此只接触一个并发原语，不接触任何 loop 类型，箭头
    仍然朝下（spec §3.2）。一旦往槽里塞更丰富的 loop 对象，反向依赖就回来了。

    返回 True = 已被热投递消费（`claimed`）；False = 投递未被接受（等待方已放弃）。
    """

    def deliver(self, decision: HitlDecision) -> bool: ...


@dataclass
class PendingHitl:
    """core 内部的活记录。**不出 core**——对外只经 `to_view()` 投影。"""

    id: str
    form: str
    session_id: str
    task_id: str
    agent_id: str
    delivery: Delivery
    created_at: datetime
    subject_id: str = ""
    prompt: str = ""
    detail: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] | None = None
    tool_call_id: str = ""                       # 幂等键 + 决定缓存键
    resume_state: dict[str, Any] | None = None   # 不透明，core 永不解读
    reply_as_result: bool = False
    #: 终局决定。**唯一的结局存储**——`resolved` 由它推导，不存第二份。
    decision: HitlDecision | None = None
    resolved_at: datetime | None = None
    slot: WaitSlot | None = None

    @property
    def resolved(self) -> bool:
        return self.decision is not None

    def to_view(self) -> HitlRequestView:
        return HitlRequestView(
            id=self.id, form=self.form, session_id=self.session_id, task_id=self.task_id,
            created_at=self.created_at, agent_id=self.agent_id, subject_id=self.subject_id,
            prompt=self.prompt, detail=self.detail, fields=list(self.fields),
            proposal=self.proposal,
            outcome=self.decision.outcome if self.decision else "",
            resolved_at=self.resolved_at,
        )


class HitlRegistry:
    """登记 / 幂等 / 决定缓存 / 等待槽 / GC。"""

    def __init__(self, max_resolved: int = 1000) -> None:
        #: 已终局项的保留上限：决定缓存只需近期的，超限裁剪最旧者，防止长跑进程无界增长。
        #: pending 永不裁剪。
        self._max_resolved = max_resolved
        self._requests: dict[str, PendingHitl] = {}

    # ── 写 ────────────────────────────────────────────────────────────────────

    def open(
        self,
        ask: HitlAsk,
        *,
        hitl_id: str,
        session_id: str,
        task_id: str,
        agent_id: str = "",
        tool_call_id: str = "",
        created_at: datetime,
    ) -> PendingHitl:
        """登记一个请求。同 `tool_call_id` 已有记录 → **复用**，不新建（幂等，spec §10）。

        空 `tool_call_id` 不作幂等键——`UserTurn` 的冷 park 本就没有 tool_call。
        """
        existing = self.find_for_tool_call(tool_call_id)
        if existing is not None:
            return existing
        req = PendingHitl(
            id=hitl_id, form=ask.form, session_id=session_id, task_id=task_id,
            agent_id=agent_id, delivery=ask.delivery, created_at=created_at,
            subject_id=ask.subject_id, prompt=ask.prompt, detail=ask.detail,
            fields=list(ask.fields), proposal=ask.proposal, tool_call_id=tool_call_id,
            resume_state=ask.resume_state, reply_as_result=ask.reply_as_result,
        )
        self._requests[hitl_id] = req
        return req

    def attach_slot(self, hitl_id: str, slot: WaitSlot) -> None:
        req = self._requests.get(hitl_id)
        if req is not None:
            req.slot = slot

    def detach_slot(self, hitl_id: str) -> None:
        """驱逐等待槽（热→冷降级）。**驱逐本身永不触发续跑**，唯有应答才触发。"""
        req = self._requests.get(hitl_id)
        if req is not None:
            req.slot = None

    def resolve(
        self, hitl_id: str, decision: HitlDecision, resolved_at: datetime,
    ) -> tuple[PendingHitl, WaitSlot | None] | None:
        """终局转移 + **原子地**取走等待槽。

        返回 `(req, slot)`；已终局或未知 id → `None`（调用方据此保持幂等：不重发事实）。
        整段无 await，故转移与取槽不可能被别的协程插入。
        """
        req = self._requests.get(hitl_id)
        if req is None or req.resolved:
            return None
        req.decision = decision
        req.resolved_at = resolved_at
        slot, req.slot = req.slot, None
        return req, slot

    def gc(self) -> None:
        """裁剪已终局项，pending 永不裁剪。"""
        resolved = [r for r in self._requests.values() if r.resolved]
        if len(resolved) <= self._max_resolved:
            return
        resolved.sort(key=lambda r: r.resolved_at or r.created_at)
        for r in resolved[: len(resolved) - self._max_resolved]:
            self._requests.pop(r.id, None)

    # ── 读 ────────────────────────────────────────────────────────────────────

    def get(self, hitl_id: str) -> PendingHitl | None:
        return self._requests.get(hitl_id)

    def find_for_tool_call(self, tool_call_id: str) -> PendingHitl | None:
        """按 tool_call_id 取最近一条记录；空 id → None。"""
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values() if r.tool_call_id == tool_call_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)

    def decision_for(self, tool_call_id: str) -> tuple[HitlDecision, dict[str, Any] | None] | None:
        """决定缓存查询：`(decision, resume_state)` 成对返回。

        成对是硬要求——冷路径重入调的是 `resume(ask_id, decision, resume_state, ctx)`，
        丢掉 `resume_state` 就等于要求 provider 重做让出前的工作（spec §7.2）。

        仍 pending（活的等待）→ None：不得把它当成「已答过」。
        """
        req = self.find_for_tool_call(tool_call_id)
        if req is None or req.decision is None:
            return None
        return req.decision, req.resume_state

    def list_pending(self, session_id: str | None = None) -> list[PendingHitl]:
        return [
            r for r in self._requests.values()
            if not r.resolved and (session_id is None or r.session_id == session_id)
        ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_registry.py -v`
Expected: PASS（14 passed）

- [ ] **Step 5: 校验分层不变式**

Run: `grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/`
Expected: 无任何输出。有输出即违反 Global Constraints，必须修掉。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/hitl/ tests/unit/test_hitl_registry.py
git commit -m "feat(hitl): HitlRegistry 纯同步内存状态机（幂等 open / 原子取槽 / 决定缓存）"
```

---

## Task 4: ReplyIntake + HitlService

**Files:**
- Create: `src/ctx_weft/core/hitl/reply_intake.py`
- Create: `src/ctx_weft/core/hitl/service.py`
- Modify: `src/ctx_weft/core/hitl/__init__.py`
- Test: `tests/unit/test_hitl_service.py`

**Interfaces:**
- Consumes: Task 1 类型、Task 2 事件类型、Task 3 的 `HitlRegistry` / `PendingHitl` / `WaitSlot`
- Produces:
  - `ContentNormalizer`（Protocol，`async (content, session_id) -> (memory_content, event_payload)`）
  - `ReplyIntake(normalizer)`，方法 `async normalize(content, req) -> tuple[content, event_payload]`
  - `HitlService(registry, event_bus, reply_intake, *, id_factory=..., clock=...)`：
    - `async open(ask, *, session_id, task_id, agent_id="", tool_call_id="") -> PendingHitl`
    - `async resolve(reply) -> PendingHitl | None`
    - `async cancel(hitl_id, *, message="") -> PendingHitl | None`

- [ ] **Step 1: 写失败的测试**

创建 `tests/unit/test_hitl_service.py`：

```python
"""HitlService：唯一漏斗——open / resolve / cancel，只发事实、不认识 Runtime。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.core.hitl.registry import HitlRegistry
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.hitl.service import HitlService
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import (
    HitlAsk,
    HitlDecision,
    HitlReply,
    ToolResultDelivery,
    UserTurnDelivery,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def payload_of(self, event_type: str) -> dict:
        return next(e.payload for e in self.events if e.type == event_type)


class PassthroughNormalizer:
    """内容原样透传的测试替身；记录被调用次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, content, session_id):
        self.calls += 1
        return content, content


class RejectingNormalizer:
    async def __call__(self, content, session_id):
        raise ValueError("unsupported media type")


class FakeSlot:
    def __init__(self, accepts: bool = True) -> None:
        self.accepts = accepts
        self.delivered = None

    def deliver(self, decision: HitlDecision) -> bool:
        self.delivered = decision
        return self.accepts


def _service(bus: RecordingBus, normalizer=None) -> HitlService:
    ids = iter(f"hit_{i}" for i in range(1, 100))
    return HitlService(
        registry=HitlRegistry(),
        event_bus=bus,
        reply_intake=ReplyIntake(normalizer or PassthroughNormalizer()),
        id_factory=lambda: next(ids),
        clock=lambda: T0,
    )


def _ask(tool_call_id: str = "call_1") -> HitlAsk:
    return HitlAsk(form="approval", delivery=ToolResultDelivery(tool_call_id=tool_call_id),
                   prompt="Allow bash?", subject_id="fs:bash_exec",
                   proposal={"command": "ls"}, resume_state={"plan": "p1"})


async def test_open_emits_exactly_one_hitl_opened():
    bus = RecordingBus()
    svc = _service(bus)
    req = await svc.open(_ask(), session_id="s1", task_id="t1", agent_id="a1",
                         tool_call_id="call_1")
    assert req.id == "hit_1"
    assert bus.types() == [EventType.HITL_OPENED]   # 不再发 SessionPausedHitl


async def test_hitl_opened_payload_carries_delivery_and_resume_state():
    bus = RecordingBus()
    await _service(bus).open(_ask(), session_id="s1", task_id="t1", agent_id="a1",
                             tool_call_id="call_1")
    p = bus.payload_of(EventType.HITL_OPENED)
    assert p["form"] == "approval"
    assert p["delivery"] == {"kind": "tool_result", "tool_call_id": "call_1"}
    assert p["resume_state"] == {"plan": "p1"}
    assert p["subject_id"] == "fs:bash_exec" and p["proposal"] == {"command": "ls"}


async def test_user_turn_delivery_serialises_its_preface():
    bus = RecordingBus()
    ask = HitlAsk(form="wait",
                  delivery=UserTurnDelivery(task_id="t1", preface="interrupt_edit"))
    await _service(bus).open(ask, session_id="s1", task_id="t1")
    assert bus.payload_of(EventType.HITL_OPENED)["delivery"] == {
        "kind": "user_turn", "task_id": "t1", "preface": "interrupt_edit"}


async def test_reopen_same_tool_call_reuses_request_and_emits_nothing_new():
    bus = RecordingBus()
    svc = _service(bus)
    a = await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    b = await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    assert a is b
    assert bus.types() == [EventType.HITL_OPENED]


async def test_resolve_emits_hitl_resolved_with_outcome_and_message():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    req = await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted", message="go"))
    assert req is not None and req.decision.outcome == "accepted"
    assert bus.types() == [EventType.HITL_OPENED, EventType.HITL_RESOLVED]
    p = bus.payload_of(EventType.HITL_RESOLVED)
    assert p["outcome"] == "accepted" and p["message"] == "go"


async def test_resolve_marks_claimed_true_when_a_hot_slot_takes_it():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    slot = FakeSlot()
    svc.registry.attach_slot("hit_1", slot)
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted", message="go"))
    assert slot.delivered.message == "go"
    assert bus.payload_of(EventType.HITL_RESOLVED)["claimed"] is True


async def test_resolve_marks_claimed_false_when_no_slot():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted"))
    assert bus.payload_of(EventType.HITL_RESOLVED)["claimed"] is False


async def test_resolve_marks_claimed_false_when_slot_refuses():
    """等待方已放弃（超时驱逐与应答擦肩）→ 必须走冷续跑，不能算已消费。"""
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    svc.registry.attach_slot("hit_1", FakeSlot(accepts=False))
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted"))
    assert bus.payload_of(EventType.HITL_RESOLVED)["claimed"] is False


async def test_second_resolve_is_a_noop_and_emits_nothing():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted"))
    assert await svc.resolve(HitlReply(hitl_id="hit_1", outcome="rejected")) is None
    assert bus.types().count(EventType.HITL_RESOLVED) == 1


async def test_resolve_unknown_id_raises_keyerror():
    svc = _service(RecordingBus())
    with pytest.raises(KeyError):
        await svc.resolve(HitlReply(hitl_id="nope", outcome="accepted"))


async def test_modified_arguments_ride_along_in_the_payload():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted",
                                modified_arguments={"command": "ls -l"}))
    assert bus.payload_of(EventType.HITL_RESOLVED)["modified_arguments"] == {
        "command": "ls -l"}


async def test_validation_failure_leaves_the_request_pending_and_emits_nothing():
    """校验先于任何状态改动：被拒的内容不得写进 decision、不得发事实（spec §7.4）。"""
    bus = RecordingBus()
    svc = _service(bus, normalizer=RejectingNormalizer())
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    with pytest.raises(ValueError):
        await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted", message="bad"))
    assert svc.registry.get("hit_1").resolved is False
    assert bus.types() == [EventType.HITL_OPENED]


async def test_cancel_resolves_with_cancelled_and_carries_its_reason():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    req = await svc.cancel("hit_1", message="failure_threshold")
    assert req is not None and req.decision.outcome == "cancelled"
    p = bus.payload_of(EventType.HITL_RESOLVED)
    assert p["outcome"] == "cancelled" and p["message"] == "failure_threshold"


async def test_cancel_on_resolved_request_is_a_noop():
    bus = RecordingBus()
    svc = _service(bus)
    await svc.open(_ask(), session_id="s1", task_id="t1", tool_call_id="call_1")
    await svc.resolve(HitlReply(hitl_id="hit_1", outcome="accepted"))
    assert await svc.cancel("hit_1", message="too late") is None
    assert bus.types().count(EventType.HITL_RESOLVED) == 1


async def test_service_never_imports_runtime_or_loop():
    """分层不变式：core/hitl 不认识协程栈，也不认识 Runtime（spec §3）。"""
    import inspect

    import ctx_weft.core.hitl.service as mod

    src = inspect.getsource(mod)
    assert "core.loop" not in src and "core.runtime" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_service.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ctx_weft.core.hitl.reply_intake'`

- [ ] **Step 3: 实现 ReplyIntake**

创建 `src/ctx_weft/core/hitl/reply_intake.py`：

```python
"""ReplyIntake：应答内容的校验 + 双侧外部化。

**构造期注入，无默认值**——旧实现「未注入 normalizer 时退化为恒等变换」制造了
「单测跑的和生产跑的不是同一个东西」，本类因此不给缺省实现：调用方必须显式提供
一个 normalizer（生产是 Runtime 的三入口共用管线，测试是显式替身）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ctx_weft.core.hitl.registry import PendingHitl
    from ctx_weft.protocols import ContentPart


class ContentNormalizer(Protocol):
    """校验 + 双侧外部化：返回 `(memory 侧内容, event 侧载荷)`。

    两侧各写各的 blob store，两个 ref 不必相同——事件侧载荷必须由**原始**内容算出，
    不能拿 memory 侧的 ref 重算（那份 ref 事件库既无权解读也解不开）。
    """

    async def __call__(
        self, content: "str | list[ContentPart]", session_id: str,
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]": ...


class ReplyIntake:
    """把应答内容过一遍校验与外部化。校验失败**原样抛出**，由调用方保证不推进状态。"""

    def __init__(self, normalizer: ContentNormalizer) -> None:
        self._normalizer = normalizer

    async def normalize(
        self, content: "str | list[ContentPart]", req: "PendingHitl",
    ) -> "tuple[str | list[ContentPart], str | list[dict] | None]":
        """收整个 `PendingHitl` 而非零散字段：blob 的 tenant 锚点要由 `session_id` 解出，
        将来再要别的字段也不必改签名。"""
        return await self._normalizer(content, req.session_id)
```

- [ ] **Step 4: 实现 HitlService**

创建 `src/ctx_weft/core/hitl/service.py`：

```python
"""HitlService：HITL 的唯一漏斗。

只做三件事：登记（open）、终局（resolve / cancel）、**发事实**。它不认识 Runtime、
不认识协程栈、不持久化任何东西——耐久性是 event store provider 的事，冷续跑是
`ResumeCoordinator` 订阅 `HitlResolved` 的事（spec §3.1 / §7.3）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.reply_intake import ReplyIntake
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_CANCELLED,
    Delivery,
    HitlAsk,
    HitlDecision,
    HitlReply,
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)

if TYPE_CHECKING:
    from ctx_weft.protocols.events import EventBus
    from ctx_weft.protocols import ContentPart

logger = logging.getLogger(__name__)


def delivery_to_payload(delivery: Delivery) -> dict[str, Any]:
    """Delivery → 事件载荷。**封闭值域**，故穷举即完备。"""
    if isinstance(delivery, ToolResultDelivery):
        return {"kind": "tool_result", "tool_call_id": delivery.tool_call_id}
    if isinstance(delivery, UserTurnDelivery):
        return {"kind": "user_turn", "task_id": delivery.task_id,
                "preface": delivery.preface}
    if isinstance(delivery, NoResumeDelivery):
        return {"kind": "no_resume"}
    raise ValueError(f"Unknown delivery: {delivery!r}")


class HitlService:
    def __init__(
        self,
        registry: HitlRegistry,
        event_bus: "EventBus",
        reply_intake: ReplyIntake,
        *,
        id_factory: Callable[[], str] = lambda: generate_id("hit"),
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self.registry = registry
        self._bus = event_bus
        self._intake = reply_intake
        self._new_id = id_factory
        self._now = clock

    async def open(
        self,
        ask: HitlAsk,
        *,
        session_id: str,
        task_id: str,
        agent_id: str = "",
        tool_call_id: str = "",
    ) -> PendingHitl:
        """登记一个请求并发 `HitlOpened`。同 tool_call_id 复用既有请求且**不重发事实**。"""
        existing = self.registry.find_for_tool_call(tool_call_id)
        if existing is not None:
            return existing
        req = self.registry.open(
            ask, hitl_id=self._new_id(), session_id=session_id, task_id=task_id,
            agent_id=agent_id, tool_call_id=tool_call_id, created_at=self._now(),
        )
        logger.info("HITL opened [%s]: %s (%s)", req.form, req.id, req.prompt[:80])
        await self._emit(EventType.HITL_OPENED, req, {
            "hitl_id": req.id,
            "form": req.form,
            "delivery": delivery_to_payload(req.delivery),
            "subject_id": req.subject_id,
            "prompt": req.prompt,
            "detail": req.detail,
            "fields": list(req.fields),
            "proposal": req.proposal,
            "tool_call_id": req.tool_call_id,
            "agent_id": req.agent_id,
            "resume_state": req.resume_state,
            "reply_as_result": req.reply_as_result,
        })
        return req

    async def resolve(self, reply: HitlReply) -> PendingHitl | None:
        """终局一个请求。已终局 → `None`（幂等 no-op，不重发事实）；未知 id → `KeyError`。"""
        req = self.registry.get(reply.hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {reply.hitl_id}")
        # 校验/外部化**先于**任何状态改动：被拒的内容不得写进 decision、不得发事实。
        message, event_payload = await self._intake.normalize(reply.message, req)
        decision = HitlDecision(outcome=reply.outcome, message=message,
                                modified_arguments=reply.modified_arguments)
        return await self._commit(req, decision, event_payload)

    async def cancel(self, hitl_id: str, *, message: "str | list[ContentPart]" = "",
                     ) -> PendingHitl | None:
        """收口一个悬挂 pending（会话关闭 / 熔断）。终态、不 requeue；已终局则 no-op。

        message 与 resolve 同走 `ReplyIntake`——不走同一条路就会发出「message 为真、
        载荷为 None」的事实，把「为什么被取消」从重放流里抹掉。
        """
        req = self.registry.get(hitl_id)
        if req is None:
            raise KeyError(f"No HITL request found: {hitl_id}")
        normalized, event_payload = await self._intake.normalize(message, req)
        decision = HitlDecision(outcome=HITL_OUTCOME_CANCELLED, message=normalized)
        return await self._commit(req, decision, event_payload)

    # ── internals ─────────────────────────────────────────────────────────────

    async def _commit(
        self,
        req: PendingHitl,
        decision: HitlDecision,
        message_event_payload: "str | list[dict] | None",
    ) -> PendingHitl | None:
        """状态转移 → 取槽 → 投递 → 发事实。

        转移与取槽在 `registry.resolve()` 里同步完成（无 await ⟹ 原子），因此
        「热投递」与「冷续跑」互斥、不双投。投递与发事实在其后，不占原子段。
        """
        transferred = self.registry.resolve(req.id, decision, self._now())
        if transferred is None:
            return None                              # 已终局：幂等 no-op
        resolved, slot = transferred
        claimed = bool(slot.deliver(decision)) if slot is not None else False
        payload: dict[str, Any] = {
            "hitl_id": resolved.id,
            "outcome": decision.outcome,
            "claimed": claimed,
        }
        # 事件载荷由**原始**内容一步之前算好、顺参数递进来——不在这里拿
        # decision.message 重算：那份内容已是 memory 侧的 ref，event store 解不开。
        if message_event_payload:
            payload["message"] = message_event_payload
        if decision.modified_arguments is not None:
            payload["modified_arguments"] = decision.modified_arguments
        await self._emit(EventType.HITL_RESOLVED, resolved, payload)
        self.registry.gc()
        return resolved

    async def _emit(self, event_type: EventType, req: PendingHitl, payload: dict) -> None:
        await self._bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=req.session_id,
            type=event_type,
            timestamp=self._now(),
            task_id=req.task_id or None,
            agent_id=req.agent_id or None,
            payload=payload,
        ))
```

- [ ] **Step 5: 更新子系统导出面**

把 `src/ctx_weft/core/hitl/__init__.py` 改为：

```python
"""core/hitl：HITL 的自足子系统。

**不 import `core.loop`、不 import `core.runtime`**，包括函数体内的延迟 import——
这是本设计的核心不变式：编排层不认识协程栈，park 只属于 loop（spec §3）。
"""

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl, WaitSlot
from ctx_weft.core.hitl.reply_intake import ContentNormalizer, ReplyIntake
from ctx_weft.core.hitl.service import HitlService, delivery_to_payload

__all__ = [
    "ContentNormalizer",
    "HitlRegistry",
    "HitlService",
    "PendingHitl",
    "ReplyIntake",
    "WaitSlot",
    "delivery_to_payload",
]
```

- [ ] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_service.py -v`
Expected: PASS（15 passed）

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/hitl/ tests/unit/test_hitl_service.py
git commit -m "feat(hitl): HitlService 唯一漏斗 + ReplyIntake（校验前置、只发事实）"
```

---

## Task 5: 双读折叠 `fold_hitl_snapshot`

这是迁移的承重件：它必须**同时**认识新旧两套事件，且对旧数据的解读要与升级前**逐条同构**（spec §12.3.2）。

**Files:**
- Create: `src/ctx_weft/core/hitl/snapshot.py`
- Modify: `src/ctx_weft/core/control/reducers.py`（追加，既有折叠函数一行不改）
- Test: `tests/unit/test_hitl_fold_snapshot.py`

**Interfaces:**
- Consumes: Task 1 类型、Task 3 的 `PendingHitl`
- Produces:
  - `HitlSnapshot`（dataclass：`pending: dict[str, PendingHitl]`、`decisions_for: dict[str, tuple[HitlDecision, dict | None]]`）
  - `fold_hitl_snapshot(events: list[Event]) -> HitlSnapshot`
  - `HITL_FOLD_EVENT_TYPES: tuple[EventType, ...]`（新旧全集，供事件库按类型过滤读取）

- [ ] **Step 1: 写失败的测试**

创建 `tests/unit/test_hitl_fold_snapshot.py`：

```python
"""双读折叠：新旧两套 HITL 事件 → HitlSnapshot（spec §12.3）。

对旧数据的判据必须与升级前**逐条同构**——迁移的正确性标准是「行为不变」，
不是「更符合新设计的意图」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ctx_weft.core.control.reducers import fold_hitl_snapshot
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.protocols.hitl import (
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _ev(event_type: str, payload: dict, *, seq: int = 0, task_id: str = "t1",
        agent_id: str = "a1") -> Event:
    return Event(id=f"evt_{seq}", run_id=None, sequence=seq, session_id="s1",
                 type=event_type, timestamp=T0 + timedelta(seconds=seq),
                 task_id=task_id, agent_id=agent_id, payload=payload)


# ── 旧事件（legacy）─────────────────────────────────────────────────────────────

def _legacy_required(hitl_id="hit_1", form="approval", capability_id="fs:bash_exec",
                     tool_call_id="call_1", context="", seq=0) -> Event:
    return _ev(EventType.HITL_REQUIRED, {
        "hitl_id": hitl_id, "form": form, "capability_id": capability_id,
        "tool_call_id": tool_call_id, "agent_id": "a1",
        "question": "Allow bash?", "context": context,
        "arguments": {"command": "ls"}, "questions": [],
    }, seq=seq)


def test_legacy_required_becomes_pending_with_tool_result_delivery():
    snap = fold_hitl_snapshot([_legacy_required()])
    req = snap.pending["hit_1"]
    assert req.form == "approval" and req.subject_id == "fs:bash_exec"
    assert req.prompt == "Allow bash?" and req.proposal == {"command": "ls"}
    assert req.delivery == ToolResultDelivery(tool_call_id="call_1")
    assert req.agent_id == "a1"


def test_legacy_wait_form_becomes_user_turn_delivery():
    """反推用 form == 'wait'（今天 runtime 的实际判据），不是 sentinel capability_id。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", capability_id="control:wait_for_user",
                         tool_call_id="", context="plain_text")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(task_id="t1",
                                                              preface="normal")


def test_legacy_wait_form_without_the_sentinel_still_becomes_user_turn():
    """判据是 form，不是 capability_id——否则迁移会改变这条在途请求的行为。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", capability_id="", tool_call_id="",
                         context="interrupt")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(task_id="t1",
                                                              preface="interrupt")


def test_legacy_wait_context_interrupt_edit_maps_to_its_preface():
    snap = fold_hitl_snapshot([
        _legacy_required(form="wait", tool_call_id="", context="interrupt:edit")])
    assert snap.pending["hit_1"].delivery == UserTurnDelivery(
        task_id="t1", preface="interrupt_edit")


def test_legacy_non_wait_without_tool_call_id_falls_back_to_no_resume():
    """既非 wait、又无 tool_call 可补 → 显式「只可取消」，不静默丢。"""
    snap = fold_hitl_snapshot([_legacy_required(form="question", tool_call_id="")])
    assert snap.pending["hit_1"].delivery == NoResumeDelivery()


def test_legacy_approved_resolves_to_accepted():
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_APPROVED, {"hitl_id": "hit_1"}, seq=1)])
    assert snap.pending == {}
    decision, resume_state = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted" and decision.modified_arguments is None
    assert resume_state is None


def test_legacy_modified_resolves_to_accepted_with_arguments():
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_MODIFIED,
            {"hitl_id": "hit_1", "modified_arguments": {"command": "ls -l"}}, seq=1)])
    decision, _ = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted"
    assert decision.modified_arguments == {"command": "ls -l"}


def test_legacy_answered_carries_its_message():
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1", "message": "yes"}, seq=1)])
    decision, _ = snap.decisions_for["call_1"]
    assert decision.outcome == "accepted" and decision.message == "yes"


def test_legacy_rejected_and_cancelled_map_to_their_outcomes():
    rejected = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_REJECTED, {"hitl_id": "hit_1", "message": "no"}, seq=1)])
    assert rejected.decisions_for["call_1"][0].outcome == "rejected"

    cancelled = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_CANCELLED, {"hitl_id": "hit_1"}, seq=1)])
    assert cancelled.pending == {}
    assert "call_1" not in cancelled.decisions_for   # cancelled 不是可用决定


def test_answered_without_message_is_not_a_usable_decision():
    """旧事件只有 hitl_id → 还原不出答案。按未决重问，**绝不臆造**（spec §12.3.3）。"""
    snap = fold_hitl_snapshot([
        _legacy_required(form="question"),
        _ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1"}, seq=1)])
    assert "call_1" not in snap.decisions_for
    assert snap.pending == {}          # 已终局，故不在 pending；但也不可用作决定


def test_modified_without_arguments_is_not_a_usable_decision():
    """缺改参会拿原参执行，违背改参意图 → 不可用。"""
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.HITL_MODIFIED, {"hitl_id": "hit_1"}, seq=1)])
    assert "call_1" not in snap.decisions_for


def test_session_paused_hitl_is_ignored():
    """会话暂停态在新模型里由 pending 集合推导，旧事件不再是真相（spec §7.1）。"""
    snap = fold_hitl_snapshot([
        _legacy_required(),
        _ev(EventType.SESSION_PAUSED_HITL, {"capability_id": "fs:bash_exec"}, seq=1)])
    assert set(snap.pending) == {"hit_1"}


# ── 新事件 ──────────────────────────────────────────────────────────────────────

def _opened(hitl_id="hit_9", delivery=None, resume_state=None, seq=0) -> Event:
    return _ev(EventType.HITL_OPENED, {
        "hitl_id": hitl_id, "form": "question",
        "delivery": delivery or {"kind": "tool_result", "tool_call_id": "call_9"},
        "subject_id": "deploy:apply", "prompt": "确认部署？", "detail": "",
        "fields": [], "proposal": None, "tool_call_id": "call_9", "agent_id": "a1",
        "resume_state": resume_state, "reply_as_result": False,
    }, seq=seq)


def test_new_opened_folds_into_pending():
    snap = fold_hitl_snapshot([_opened()])
    req = snap.pending["hit_9"]
    assert req.delivery == ToolResultDelivery(tool_call_id="call_9")
    assert req.subject_id == "deploy:apply" and req.prompt == "确认部署？"


def test_new_user_turn_delivery_round_trips():
    snap = fold_hitl_snapshot([_opened(
        delivery={"kind": "user_turn", "task_id": "t1", "preface": "interrupt_edit"})])
    assert snap.pending["hit_9"].delivery == UserTurnDelivery(
        task_id="t1", preface="interrupt_edit")


def test_new_resolved_pairs_the_decision_with_its_resume_state():
    """决定必须与 resume_state 成对——只给决定就要重做让出前的工作（spec §7.2）。"""
    snap = fold_hitl_snapshot([
        _opened(resume_state={"plan": "deploy-7"}),
        _ev(EventType.HITL_RESOLVED,
            {"hitl_id": "hit_9", "outcome": "accepted", "message": "go",
             "claimed": False}, seq=1)])
    assert snap.pending == {}
    decision, resume_state = snap.decisions_for["call_9"]
    assert decision.outcome == "accepted" and decision.message == "go"
    assert resume_state == {"plan": "deploy-7"}


def test_new_resolved_with_host_custom_outcome_is_passed_through():
    """outcome 是开放值域，core 只判「非空即终局」，不解释语义（spec §9.4）。"""
    snap = fold_hitl_snapshot([
        _opened(),
        _ev(EventType.HITL_RESOLVED,
            {"hitl_id": "hit_9", "outcome": "escalated", "claimed": False}, seq=1)])
    assert snap.decisions_for["call_9"][0].outcome == "escalated"


def test_last_usable_decision_wins_for_the_same_tool_call():
    """同 tool_call 重问副本：最后一条可用决定胜出。"""
    snap = fold_hitl_snapshot([
        _legacy_required(hitl_id="hit_1", seq=0),
        _ev(EventType.HITL_REJECTED, {"hitl_id": "hit_1", "message": "no"}, seq=1),
        _legacy_required(hitl_id="hit_2", seq=2),
        _ev(EventType.HITL_APPROVED, {"hitl_id": "hit_2"}, seq=3)])
    assert snap.decisions_for["call_1"][0].outcome == "accepted"


def test_empty_event_list_yields_an_empty_snapshot():
    snap = fold_hitl_snapshot([])
    assert snap.pending == {} and snap.decisions_for == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_fold_snapshot.py -v`
Expected: FAIL —— `ImportError: cannot import name 'fold_hitl_snapshot'`

- [ ] **Step 3: 实现 HitlSnapshot**

创建 `src/ctx_weft/core/hitl/snapshot.py`：

```python
"""HitlSnapshot：折叠事件得到的、可直接装填进 `HitlRegistry` 的内存态。

恢复是「喂进来」，不是「查回去」：core 的一切 HITL 查询只读内存，装填的完备性
由恢复路径承担（spec §3.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ctx_weft.core.hitl.registry import PendingHitl
from ctx_weft.protocols.hitl import HitlDecision


@dataclass
class HitlSnapshot:
    """`pending`：仍未终局的请求。

    `decisions_for`：`tool_call_id → (决定, resume_state)`。**成对**是硬要求——
    冷路径重入调 `resume(ask_id, decision, resume_state, ctx)`，丢掉 resume_state
    就要求 provider 重做让出前的工作（spec §7.2）。只收录**可用**的决定。
    """

    pending: dict[str, PendingHitl] = field(default_factory=dict)
    decisions_for: dict[str, tuple[HitlDecision, dict[str, Any] | None]] = field(
        default_factory=dict)
```

- [ ] **Step 4: 实现折叠函数**

在 `src/ctx_weft/core/control/reducers.py` 末尾追加（既有折叠函数一行不改）：

```python
# ══════════════════════════════════════════════════════════════════════════════
# HITL v2 双读折叠（2026-09-01 重设计 · 段 1）
# 设计：docs/superpowers/specs/2026-09-01-hitl-redesign-design.md §12.3
# ══════════════════════════════════════════════════════════════════════════════

#: 折叠所需的事件类型全集（新 2 类 + 旧 6 类）。供事件库按类型过滤读取，
#: 无需全量回放。`SessionPausedHitl` 不在其中——新模型由 pending 集合推导。
HITL_FOLD_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.HITL_OPENED,
    EventType.HITL_RESOLVED,
    EventType.HITL_REQUIRED,
    *_HITL_RESOLVE_TYPES,
)

#: 旧 `context` 字符串 → 新 `UserTurnDelivery.preface` 枚举。
_LEGACY_PREFACE: dict[str, str] = {
    "plain_text": PREFACE_NORMAL,
    "interrupt": PREFACE_AFTER_INTERRUPT,
    "interrupt:edit": PREFACE_AFTER_INTERRUPT_EDIT,
}


def _delivery_from_payload(payload: dict) -> Delivery:
    """新事件的 delivery 载荷 → Delivery。封闭值域，穷举即完备。"""
    kind = payload.get("kind", "")
    if kind == "tool_result":
        return ToolResultDelivery(tool_call_id=payload.get("tool_call_id", ""))
    if kind == "user_turn":
        return UserTurnDelivery(task_id=payload.get("task_id", ""),
                                preface=payload.get("preface", PREFACE_NORMAL))
    return NoResumeDelivery()


def _legacy_delivery(form: str, tool_call_id: str, context: str, task_id: str) -> Delivery:
    """旧请求 → Delivery 的反推。

    **复刻旧的「判据」，而不是旧的「意图」**：今天 runtime 的实际分流判据是
    `req.form == "wait"`（runtime.py:1725），不是 sentinel capability_id。用
    sentinel 反推会让「form 是 wait 但 capability_id 不是 sentinel」的在途请求
    从「注入」变成「补 tool_call」——迁移本身改变了行为。迁移的正确性判据是
    与升级前逐条同构（spec §12.3.2）。
    """
    if form == "wait":
        return UserTurnDelivery(task_id=task_id,
                                preface=_LEGACY_PREFACE.get(context, PREFACE_NORMAL))
    if tool_call_id:
        return ToolResultDelivery(tool_call_id=tool_call_id)
    # 既非 wait、又无 tool_call 可补：续跑无从谈起。显式「只可取消」，不静默丢。
    return NoResumeDelivery()


def _legacy_decision(event_type: str, payload: dict) -> HitlDecision | None:
    """旧 resolve 事件 → 决定；**不可用**则返回 None（按未决重问，绝不臆造）。

    可用性规则原样继承自 `fold_cold_hitl_decision`：Answered 须带 message、
    Modified 须带 modified_arguments、Cancelled 不是决定（spec §12.3.3）。

    **message 必须过 `content_from_jsonable`**：事件里存的是 jsonable 形态
    （`str | list[dict]`），而 `HitlDecision.message` 是 `str | list[ContentPart]`。
    不转换的话，带图答复恢复出来是一堆裸 dict，下游 `split_for_tool_result` 按
    `hasattr(p, "text")` 分区 → 人打的字变成空串（重演 6284929 修过的静默丢图）。
    """
    message = content_from_jsonable(payload.get("message") or "")
    if event_type == EventType.HITL_APPROVED:
        return HitlDecision(outcome=HITL_OUTCOME_ACCEPTED, message=message)
    if event_type == EventType.HITL_MODIFIED:
        args = payload.get("modified_arguments")
        if args is None:
            return None
        return HitlDecision(outcome=HITL_OUTCOME_ACCEPTED, message=message,
                            modified_arguments=args)
    if event_type == EventType.HITL_ANSWERED:
        # **按真值判定，不是 `is None`**：`fold_cold_hitl_decision` 的门是
        # `p.get("message")`，故 `""` / `[]` 今天就不是可用决定、会重问。放宽成
        # `is None` 等于从空载荷里造出一个「已接受、答案为空」的决定。
        if not payload.get("message"):
            return None
        return HitlDecision(outcome=HITL_OUTCOME_ACCEPTED, message=message)
    if event_type == EventType.HITL_REJECTED:
        return HitlDecision(outcome=HITL_OUTCOME_REJECTED, message=message)
    return None                                   # HITL_CANCELLED：不是决定


def fold_hitl_snapshot(events: list[Event]) -> HitlSnapshot:
    """双读折叠：新旧两套 HITL 事件 → `HitlSnapshot`。

    同 tool_call 有多条请求（重问副本）时，**最后一条可用决定胜出**。
    """
    snap = HitlSnapshot()
    opened: dict[str, PendingHitl] = {}
    for ev in events:
        p = ev.payload or {}
        rid = p.get("hitl_id", "")
        if not rid:
            continue

        if ev.type == EventType.HITL_OPENED:
            req = PendingHitl(
                id=rid, form=p.get("form", ""), session_id=ev.session_id,
                task_id=ev.task_id or "", agent_id=p.get("agent_id", "") or (ev.agent_id or ""),
                delivery=_delivery_from_payload(p.get("delivery") or {}),
                created_at=ev.timestamp, subject_id=p.get("subject_id", ""),
                prompt=p.get("prompt", ""), detail=p.get("detail", ""),
                fields=list(p.get("fields") or []), proposal=p.get("proposal"),
                tool_call_id=p.get("tool_call_id", ""), resume_state=p.get("resume_state"),
                reply_as_result=bool(p.get("reply_as_result", False)),
            )
            opened[rid] = req
            snap.pending[rid] = req

        elif ev.type == EventType.HITL_REQUIRED:
            form = p.get("form", "approval")
            tool_call_id = p.get("tool_call_id", "")
            req = PendingHitl(
                id=rid, form=form, session_id=ev.session_id, task_id=ev.task_id or "",
                agent_id=p.get("agent_id", "") or (ev.agent_id or ""),
                delivery=_legacy_delivery(form, tool_call_id, p.get("context", ""),
                                          ev.task_id or ""),
                created_at=ev.timestamp, subject_id=p.get("capability_id", ""),
                prompt=p.get("question", ""), detail=p.get("context", ""),
                fields=list(p.get("questions") or []), proposal=p.get("arguments") or None,
                tool_call_id=tool_call_id,
            )
            opened[rid] = req
            snap.pending[rid] = req

        elif ev.type == EventType.HITL_RESOLVED:
            snap.pending.pop(rid, None)
            req = opened.get(rid)
            outcome = p.get("outcome", "")
            if req is None or not outcome:
                continue
            decision = HitlDecision(
                # 同 `_legacy_decision`：事件载荷是 jsonable 形态，必须转回 ContentPart。
                outcome=outcome, message=content_from_jsonable(p.get("message") or ""),
                modified_arguments=p.get("modified_arguments"),
            )
            if outcome != HITL_OUTCOME_CANCELLED and req.tool_call_id:
                snap.decisions_for[req.tool_call_id] = (decision, req.resume_state)

        elif ev.type in _HITL_RESOLVE_TYPES:
            snap.pending.pop(rid, None)
            req = opened.get(rid)
            decision = _legacy_decision(ev.type, p)
            if req is None or decision is None or not req.tool_call_id:
                continue
            snap.decisions_for[req.tool_call_id] = (decision, req.resume_state)

    return snap
```

同时在 `reducers.py` 的 import 区补上本函数用到的名字（该文件已 import `Event` / `EventType`）：

```python
from ctx_weft.core.hitl.snapshot import HitlSnapshot
from ctx_weft.core.hitl.registry import PendingHitl
from ctx_weft.protocols.hitl import (
    HITL_OUTCOME_ACCEPTED,
    HITL_OUTCOME_CANCELLED,
    HITL_OUTCOME_REJECTED,
    PREFACE_AFTER_INTERRUPT,
    PREFACE_AFTER_INTERRUPT_EDIT,
    PREFACE_NORMAL,
    Delivery,
    HitlDecision,
    NoResumeDelivery,
    ToolResultDelivery,
    UserTurnDelivery,
)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_fold_snapshot.py -v`
Expected: PASS（19 passed）

- [ ] **Step 6: 确认零回归**

Run: `uv run pytest tests/unit tests/unit_protocols -q`
Expected: 全绿。旧的 `fold_pending_hitl` / `fold_cold_hitl_decision` 及其测试**必须仍然通过**——段 1 不动它们。

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/hitl/snapshot.py src/ctx_weft/core/control/reducers.py tests/unit/test_hitl_fold_snapshot.py
git commit -m "feat(hitl): fold_hitl_snapshot 双读折叠（新旧两套事件 + Delivery 反推）"
```

---

## Task 6: registry 装填 —— 完备即构造

**Files:**
- Modify: `src/ctx_weft/core/hitl/registry.py`（追加 `load_snapshot`）
- Test: `tests/unit/test_hitl_registry_load.py`

**Interfaces:**
- Consumes: Task 5 的 `HitlSnapshot`
- Produces: `HitlRegistry.load_snapshot(snapshot: HitlSnapshot) -> int`（返回装填的 pending 条数）

- [ ] **Step 1: 写失败的测试**

创建 `tests/unit/test_hitl_registry_load.py`：

```python
"""装填：恢复期把折叠结果喂进 registry，之后 core 的一切查询只读内存（spec §3.1）。"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.hitl.registry import HitlRegistry, PendingHitl
from ctx_weft.core.hitl.snapshot import HitlSnapshot
from ctx_weft.protocols.hitl import HitlDecision, ToolResultDelivery

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _pending(hitl_id: str, tool_call_id: str) -> PendingHitl:
    return PendingHitl(
        id=hitl_id, form="approval", session_id="s1", task_id="t1", agent_id="a1",
        delivery=ToolResultDelivery(tool_call_id=tool_call_id), created_at=T0,
        tool_call_id=tool_call_id)


def test_load_snapshot_restores_pending_requests():
    reg = HitlRegistry()
    n = reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    assert n == 1
    assert reg.get("hit_1") is not None
    assert [r.id for r in reg.list_pending()] == ["hit_1"]


def test_loaded_pending_has_no_wait_slot():
    """重启后一切皆冷：装填出来的请求没有等待槽（spec §10）。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    assert reg.get("hit_1").slot is None


def test_load_snapshot_restores_decisions_with_their_resume_state():
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_9": (HitlDecision(outcome="accepted", message="go"), {"plan": "deploy-7"})}))
    got = reg.decision_for("call_9")
    assert got is not None
    decision, resume_state = got
    assert decision.message == "go" and resume_state == {"plan": "deploy-7"}


def test_loaded_decision_is_queryable_without_touching_storage():
    """装填之后不再有第二级回落——一次内存查询即可（spec §11）。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_9": (HitlDecision(outcome="rejected"), None)}))
    assert reg.decision_for("call_9")[0].outcome == "rejected"
    assert reg.decision_for("call_unknown") is None


def test_live_pending_wins_over_a_loaded_decision_for_the_same_tool_call():
    """内存 pending = 活的等待，不得被日志里的旧决定盖掉。"""
    reg = HitlRegistry()
    reg.load_snapshot(HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")}))
    reg.load_snapshot(HitlSnapshot(decisions_for={
        "call_1": (HitlDecision(outcome="accepted"), None)}))
    assert reg.decision_for("call_1") is None
    assert reg.get("hit_1").resolved is False


def test_load_snapshot_is_idempotent():
    reg = HitlRegistry()
    snap = HitlSnapshot(pending={"hit_1": _pending("hit_1", "call_1")})
    reg.load_snapshot(snap)
    reg.load_snapshot(snap)
    assert len(reg.list_pending()) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_hitl_registry_load.py -v`
Expected: FAIL —— `AttributeError: 'HitlRegistry' object has no attribute 'load_snapshot'`

- [ ] **Step 3: 实现**

在 `src/ctx_weft/core/hitl/registry.py` 的 import 区加上：

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_weft.core.hitl.snapshot import HitlSnapshot
```

并在 `HitlRegistry` 的「写」区追加：

```python
    def load_snapshot(self, snapshot: "HitlSnapshot") -> int:
        """把折叠结果装填进内存，返回 pending 条数。

        **完备即构造**：装填之后 core 的一切查询只读内存，绝不回落去 scan 事件日志。
        装填的完备性因此是恢复路径的责任（spec §3.1）。

        两条规则：
        - 装填出来的 pending **不带等待槽**——重启后一切皆冷（spec §10）。
        - 已有的**活 pending 优先**：日志里的旧决定不得盖掉一个正在等人的请求，
          否则会把活请求判成「已答过」而跳过。
        """
        for hitl_id, req in snapshot.pending.items():
            req.slot = None
            self._requests.setdefault(hitl_id, req)
        for tool_call_id, (decision, resume_state) in snapshot.decisions_for.items():
            live = self.find_for_tool_call(tool_call_id)
            if live is not None:
                continue                       # 活 pending 或已装填的决定，均不覆盖
            placeholder = PendingHitl(
                id=f"loaded:{tool_call_id}", form="", session_id="", task_id="",
                agent_id="", delivery=NoResumeDelivery(), created_at=_EPOCH,
                tool_call_id=tool_call_id, resume_state=resume_state,
                decision=decision,
            )
            self._requests[placeholder.id] = placeholder
        return len(snapshot.pending)
```

并在 `registry.py` 顶部补 `NoResumeDelivery` 的 import 与 `_EPOCH` 常量：

```python
from datetime import UTC, datetime

from ctx_weft.protocols.hitl import (
    Delivery,
    HitlAsk,
    HitlDecision,
    HitlRequestView,
    NoResumeDelivery,
)

#: 装填决定时的占位创建时间——占位项只为回答 `decision_for`，永不出现在 pending 列表里，
#: 故取最小值即可（GC 排序用 resolved_at，装填项无 resolved_at 时回落到它）。
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_hitl_registry_load.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 全量回归 + 分层校验**

Run:
```bash
uv run pytest tests -q
grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/
uv run ruff check src/ctx_weft tests
```
Expected: 测试全绿；grep 无输出；ruff 无告警。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/hitl/registry.py tests/unit/test_hitl_registry_load.py
git commit -m "feat(hitl): registry 装填（完备即构造，活 pending 不被旧决定覆盖）"
```

---

## 段 1 完成判据

- [ ] `uv run pytest tests -q` 全绿，且**旧 HITL 测试一条未改**
- [ ] `grep -rn "core.loop\|core.runtime" src/ctx_weft/core/hitl/` 无输出
- [ ] `git diff --stat <段1 起点>..HEAD -- src/ctx_weft/core/loop src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/hitl_manager.py src/ctx_weft/providers` 为空——本段不碰任何执行路径
- [ ] 新子系统可裸测：`HitlRegistry` 无 async、无 I/O；`HitlService` 只依赖注入进来的 bus 与 intake

---

## 后续计划（不在本文档范围）

- **段 2（原子替换）**：`HitlWaiter` + gateway 接管、`NeedsHuman` 契约切换、`act` / `runtime` 改造、恢复装填接线、旧 `HitlManager` 删除。风险最高（动 `restore` 这条所有会话恢复都走的路径）。
- **段 3（收口）**：host 端点合一为 `POST /hitl/{id}/reply`；核对退役闸门后删除双读折叠。
