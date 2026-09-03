# Agent 中心化交互 + LLM 事件收敛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把外部交互的对象从 session 改为 agent——session 退为成员登记表，agent 自持状态并可被逐个寻址、暂停、取消；同时落地 V2 已定案的 `origin` 信封字段，据此把各 step 自维护的 LLM 镜像事件收敛掉。

**Architecture:** 三层地基自下而上：① `origin` 信封字段按 `events-v2.md` §4 结构性填充（`LoopState.origin` 由 driver 每步写入，`make_event` 默认取，40+ 循环内发射点零改动）；② 有了 `origin`，`BackgroundObserve*` 那族镜像事件失去存在理由，`LLM_*` 的发射统一收进 `llm_gateway`；③ `AgentRegistry` 晋升 `AgentLifecycleManager`——订阅 `TASK_*` 驱动一个五态机、持 `parent→children` 索引、发 `AGENT_*` 事件、提供接收守卫，`SessionManager` 同步降格为只认成员的登记表。外部 API 最后接上去。

**Tech Stack:** Python 3.11+、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。

**Spec:** `docs/superpowers/specs/2026-09-03-agent-centric-interaction-design.md`
**配套机制参考页:** `docs/superpowers/specs/2026-09-03-agent-centric-interaction-mechanism.html`
**事件体系权威文件:** `docs/events-v2.md`（§0 信封 / §4 origin / §5 L 档 / §6 三条不变式 / §7 命名规则）

## Global Constraints

- **Python** `>=3.11`；**ruff** `line-length = 100`，`select = ["E","W","F","I","B","UP","RUF"]`，`ignore = ["E501"]`。
- **pytest** `asyncio_mode = "auto"`、`testpaths = ["tests"]`、`addopts = "-ra -q --strict-markers"`。仓库无 Makefile / CI；命令直接跑：
  - 测试：`python -m pytest tests/unit/test_x.py -q`
  - 全量：`python -m pytest -q`
  - lint：`python -m ruff check src tests`
- **禁区：不要碰 golden 用例。** `tests/unit/test_capsule_golden.py`、`tests/unit/test_dispatch_fold_golden.py`、`tests/unit/test_golden_conformance.py` 有另一个进程正在修改。若本计划的改动让它们变红，**记录下来交给用户，不要自行修改这三个文件**。
- **事件体系三条不变式**（`docs/events-v2.md` §6，每个碰事件的任务都受约束）：
  1. `EventType` 全集 ≡ 实际发射集合 ∪ L 档。**定义即必须发射**；L 档是唯一例外，且必须是显式白名单。
  2. S / O / L 三集合两两不交、并集 ≡ `EventType` 全集。**加一个枚举值就得显式选边。**
  3. `STATE_EVENT_TYPES` ≡ `core/control/reducers.py` 与 host 侧 `projection_updater.py` 折叠集合的并集。host 不共享 core 的 reducer，那部分只能靠 host 侧测试补足。
  外加两条：所有发射出的事件 `origin` 非空；L 档 ∩ 实际发射集合 = ∅。
- **只删发射，不删枚举。** 本计划停发 9 个事件类型，`EventType` 枚举值与 `reducers._apply` 的对应分支**一律保留**（`docs/events-v2.md` §5）。删枚举值要另过两级退役闸门，不在本计划范围。
- **新名字绝不复用任何曾经发射过的字符串**（§7）。本计划新增的 5 个 `AGENT_*` 字符串全仓从未出现，实施前用 `git log -S` 复核一次。
- **`TaskStatus` 是 `Literal` 不是枚举**（`core/state/models.py`）——没有 `TaskStatus.FINISHED` 属性，一律与字符串字面量比较。
- 每个任务结束时 `python -m ruff check src tests` 必须干净。

---

## File Structure

**新建**

| 文件 | 职责 |
|---|---|
| `src/ctx_weft/core/orchestrator/agent_state.py` | agent 五态机的纯函数层：`AgentStatus` 值域、`AgentInput` 输入、`AgentTransition`、`next_agent_transition()`。与 `session_state.py` 同构，无副作用、易测 |
| `tests/unit/test_agent_state_machine.py` | 状态机纯函数单测 |
| `tests/unit/test_event_origin.py` | `origin` 字段与不变式（发射出的事件 origin 非空） |
| `tests/unit/test_agent_lifecycle.py` | ALM 订阅 → 状态转移 → 发 `AGENT_*` |
| `tests/unit/test_agent_cascade.py` | cancel / pause / resume 的级联 |
| `tests/unit/test_runtime_agent_api.py` | `send_message` / `list_agents` / `get_agent` |
| `tests/unit/test_llm_event_convergence.py` | LLM_* 由 gateway 统一发射、5 个镜像事件停发 |

**修改**

| 文件 | 改什么 |
|---|---|
| `src/ctx_weft/protocols/events.py` | `Event` 加 `origin` 字段；`EventOrigin` 17 个常量；5 个 `AGENT_*` 枚举值；`L_TIER_EVENT_TYPES` 白名单；`STATE_EVENT_TYPES` 补 `AGENT_*` |
| `src/ctx_weft/core/loop/driver.py` | `make_event` 默认从 `state.origin` 取 + 支持显式覆盖；driver 每步写 `state.origin` |
| `src/ctx_weft/core/loop/loop_state.py`（或 LoopState 定义处） | 加 `origin: str = ""` 字段 |
| `src/ctx_weft/core/loop/llm_gateway.py` | 接管 6 种 `LLM_*` 的发射 |
| `src/ctx_weft/core/loop/steps/act.py` | 删除自己的 5 处 LLM_* 发射 |
| `src/ctx_weft/core/loop/steps/observe.py` | 删 `ReactEventTypes` + 两个常量 + `event_types` 形参 |
| `src/ctx_weft/core/loop/steps/background_observe.py` | 改用 `origin` 显式覆盖 |
| `src/ctx_weft/core/loop/steps/recognize_intent.py` | 切 `stream_llm_resilient`；删 `RECOGNIZE_INTENT_LLM_PROMPT` 发射；脱敏统一 |
| `src/ctx_weft/core/orchestrator/agent_registry.py` | 晋升为 lifecycle manager：status / current_task_id / parent→children 索引 / 订阅 / 发事件 / 守卫 / 级联 |
| `src/ctx_weft/core/orchestrator/session_manager.py` | `_SessionState` 换成 `agent_ids`；改订阅 `AGENT_INSTANTIATED`/`AGENT_SPAWNED`；停发 4 个 `SESSION_*` |
| `src/ctx_weft/core/orchestrator/task_manager.py` | `_emit` 信封补 `agent_id` |
| `src/ctx_weft/core/control/reducers.py` | 折叠 5 个 `AGENT_*`；保留 9 个停发类型的旧分支 |
| `src/ctx_weft/core/runtime.py` | 新增 6 个 agent 级 API；`start_session` 返回 root_agent_id；session 级 API 改广播 |
| `src/ctx_weft/protocols/hitl.py` | `HitlReply` 加 `agent_id` |

**删除**：`src/ctx_weft/core/orchestrator/session_state.py` 的状态机部分（Task 16 详述保留边界）。

---

## Phase A · origin 地基

### Task 1: `Event.origin` 字段与 `EventOrigin` 常量

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`
- Test: `tests/unit/test_event_origin.py`（新建）

**Interfaces:**
- Consumes: 无（本计划起点）
- Produces: `Event.origin: str`（默认 `""`）；`EventOrigin` 类，17 个 `str` 常量，属性名如 `LOOP_ACT`、`ORCHESTRATOR_TASK_MANAGER`；`EventOrigin.all()` 返回 `frozenset[str]`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_event_origin.py
from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.protocols.events import Event, EventOrigin


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id=None, sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC),
    )
    base.update(kw)
    return Event(**base)


def test_origin_defaults_to_empty_string():
    """存量事件读出空串——§0 明说不做反推。"""
    assert _ev().origin == ""


def test_origin_round_trips():
    assert _ev(origin=EventOrigin.LOOP_ACT).origin == "loop.act"


def test_event_origin_has_17_values():
    assert len(EventOrigin.all()) == 17


def test_origin_values_are_two_level_dotted_or_bare():
    """§4：两级点号供 host 前缀匹配；分隔符用 . 不用 :（: 留给 capability id）。"""
    for v in EventOrigin.all():
        assert ":" not in v
        assert v.count(".") <= 1
        assert v == v.strip()


def test_loop_prefix_matches_all_loop_origins():
    loop = {v for v in EventOrigin.all() if v.startswith("loop.")}
    assert EventOrigin.LOOP_ACT in loop
    assert EventOrigin.LOOP_BACKGROUND_OBSERVE in loop
    assert EventOrigin.RUNTIME not in loop
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_event_origin.py -q`
Expected: FAIL — `ImportError: cannot import name 'EventOrigin'`

- [ ] **Step 3: 实现**

在 `src/ctx_weft/protocols/events.py` 的 `Event` dataclass 里，**在 `payload` 之前**插入字段（§0 信封表的顺序：`agent_id` 之后、`payload` 之前）：

```python
@dataclass
class Event:
    id: str
    run_id: str | None
    sequence: int
    session_id: str
    type: str
    timestamp: datetime
    tenant_id: str = "default"
    task_id: str | None = None
    agent_id: str | None = None
    origin: str = ""  # V2 新增：哪个组件发出的，见 docs/events-v2.md §4。存量事件读出空串
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    causation_id: str | None = None
    schema_version: int = 1
```

在同文件 `EventType` 定义之后加常量类：

```python
class EventOrigin:
    """`origin` 的 17 个取值（docs/events-v2.md §4）。

    两级点号是为了让 host 能前缀匹配：`loop.` 取全部循环内事件，
    `loop.background_observe` 精确排除后台观察的渲染。
    分隔符用 `.` 不用 `:`——`:` 留给可路由的 capability id（`provider:tool`）。
    """

    ORCHESTRATOR_SESSION_MANAGER = "orchestrator.session_manager"
    ORCHESTRATOR_TASK_MANAGER = "orchestrator.task_manager"
    LOOP_DRIVER = "loop.driver"
    LOOP_PREPARE = "loop.prepare"
    LOOP_ACT = "loop.act"
    LOOP_OBSERVE = "loop.observe"
    LOOP_BACKGROUND_OBSERVE = "loop.background_observe"
    LOOP_RECOGNIZE_INTENT = "loop.recognize_intent"
    LOOP_COMPACT = "loop.compact"
    LOOP_FINALIZE = "loop.finalize"
    LOOP_SUSPEND = "loop.suspend"
    LOOP_RECONCILE = "loop.reconcile"
    LOOP_CAPABILITY_GATEWAY = "loop.capability_gateway"
    LOOP_LLM_GATEWAY = "loop.llm_gateway"
    HITL_SERVICE = "hitl.service"
    RUNTIME = "runtime"
    PERSISTENCE_SNAPSHOT_WRITER = "persistence.snapshot_writer"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(
            v for k, v in vars(cls).items()
            if not k.startswith("_") and isinstance(v, str)
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_event_origin.py -q`
Expected: 5 passed

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q` 然后 `python -m ruff check src tests`
Expected: 除三个 golden 文件外无新增失败。`Event` 加的是带默认值的字段，所有既有构造点不受影响。**若 golden 变红，记录不修。**

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/events.py tests/unit/test_event_origin.py
git commit -m "feat(events): Event 信封新增 origin 字段 + EventOrigin 17 个取值（V2 §0/§4）"
```

---

### Task 2: `LoopState.origin` 管道与 `make_event` 默认取值

**Files:**
- Modify: LoopState 定义处（先 `grep -rn "class LoopState" src/`；预期在 `src/ctx_weft/core/loop/` 下）
- Modify: `src/ctx_weft/core/loop/driver.py`（`make_event` 约 157-182；`StepDriver.run` 约 268-292）
- Test: `tests/unit/test_event_origin.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Event.origin`、`EventOrigin`
- Produces: `LoopState.origin: str`；`make_event(state, type, payload=None, *, origin: str | None = None) -> Event` —— `origin=None` 时取 `state.origin`，显式传值时覆盖

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_event_origin.py
from ctx_weft.core.loop.driver import make_event
from ctx_weft.protocols.events import EventType


class _FakeState:
    """make_event 只读这几个字段。"""
    def __init__(self, origin: str = ""):
        self.session_id = "s1"
        self.task_id = "t1"
        self.agent_id = "a1"
        self.run_id = "r1"
        self.tenant_id = "default"
        self.sequence_counter = 0
        self.origin = origin


def test_make_event_takes_origin_from_state():
    """§4 填充方式 1：40+ 个循环内发射点零改动。"""
    ev = make_event(_FakeState(origin=EventOrigin.LOOP_ACT), EventType.ACT_TURN_STARTED, {})
    assert ev.origin == "loop.act"


def test_make_event_explicit_origin_overrides_state():
    """§4 填充方式 2：给 background observe 这类脱离主 driver 序列的场景。"""
    ev = make_event(
        _FakeState(origin=EventOrigin.LOOP_OBSERVE),
        EventType.LLM_PROMPT_SENT,
        {},
        origin=EventOrigin.LOOP_BACKGROUND_OBSERVE,
    )
    assert ev.origin == "loop.background_observe"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_event_origin.py -q -k origin_from_state`
Expected: FAIL — `_FakeState` 的 `origin` 没被读取，`ev.origin == ""`

- [ ] **Step 3: 实现**

先给 `LoopState` 加字段（放在已有字段末尾，带默认值，避免破坏既有构造）：

```python
    origin: str = ""  # driver 每步开始前写入，make_event 默认从这里取（events-v2.md §4）
```

改 `make_event`（保持既有签名向后兼容，只加一个 keyword-only 参数）：

```python
def make_event(
    state: "LoopState",
    event_type: EventType,
    payload: dict | None = None,
    *,
    origin: str | None = None,
) -> Event:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
    state.sequence_counter += 1
    return Event(
        id=generate_id("evt"),
        run_id=state.run_id,
        sequence=state.sequence_counter,
        session_id=state.session_id,
        type=event_type,
        timestamp=now_utc(),
        tenant_id=getattr(state, "tenant_id", "default"),
        task_id=state.task_id,
        agent_id=state.agent_id,
        origin=origin if origin is not None else getattr(state, "origin", ""),
        payload=payload or {},
    )
```

> 保留 `make_event` 原有的 sequence 分配与 `EVENT_TYPES` 校验逻辑不变——上面只是在返回的 `Event(...)` 里多填一个 `origin=`，其余按现有实现原样保留。

在 `StepDriver.run` 里，每步开始前写 `state.origin`。step 名到 origin 的映射：

```python
_STEP_ORIGIN: dict[str, str] = {
    "prepare": EventOrigin.LOOP_PREPARE,
    "act": EventOrigin.LOOP_ACT,
    "observe": EventOrigin.LOOP_OBSERVE,
    "recognize_intent": EventOrigin.LOOP_RECOGNIZE_INTENT,
    "compact": EventOrigin.LOOP_COMPACT,
    "finalize": EventOrigin.LOOP_FINALIZE,
    "suspend": EventOrigin.LOOP_SUSPEND,
    "reconcile": EventOrigin.LOOP_RECONCILE,
}
```

在发 `STEP_STARTED` 之前插一行（`step_name` 是该处已有的局部变量）：

```python
        state.origin = _STEP_ORIGIN.get(step_name, EventOrigin.LOOP_DRIVER)
```

`STEP_STARTED` / `STEP_COMPLETED` / `STEP_FAILED` 三条是 driver 自己的事实，改成显式覆盖：

```python
        await ctx.event_bus.emit(make_event(
            state, EventType.STEP_STARTED, {"step_name": step_name},
            origin=EventOrigin.LOOP_DRIVER,
        ))
```
（`STEP_COMPLETED` / `STEP_FAILED` 两处同样加 `origin=EventOrigin.LOOP_DRIVER`。）

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_event_origin.py -q`
Expected: 7 passed

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: 无新增失败（golden 除外，记录不修）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/ tests/unit/test_event_origin.py
git commit -m "feat(events): LoopState.origin 管道 + make_event 默认取值（V2 §4 填充方式 1/2）"
```

---

### Task 3: 循环外 5 个发射者各持模块常量

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`（`_emit` + `_emit_session_event`）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_emit` 约 1119）
- Modify: `src/ctx_weft/core/hitl/service.py`（`_emit`）
- Modify: `src/ctx_weft/core/runtime.py`（run 域事件的构造点）
- Modify: `src/ctx_weft/providers/events/persister.py` 或 SnapshotWriter 所在文件
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`（4 处内联 `Event(...)`）
- Modify: `src/ctx_weft/core/loop/capability_gateway.py`、`src/ctx_weft/core/loop/llm_gateway.py`
- Test: `tests/unit/test_event_origin.py`（追加不变式测试）

**Interfaces:**
- Consumes: Task 1 的 `EventOrigin`
- Produces: 所有发射点填好 `origin`；不变式「发射出的事件 origin 非空」可被测试断言

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_event_origin.py
import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.runtime import SessionStartParams
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime


@pytest.mark.asyncio
async def test_every_emitted_event_has_nonempty_origin():
    """docs/events-v2.md §6 外加条：所有发射出的事件 origin 非空。"""
    rt: CtxWeftRuntime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    seen: list[tuple[str, str]] = []

    async def _spy(ev):
        seen.append((ev.type, ev.origin))

    rt._event_bus.subscribe(None, _spy)

    await rt.start_session(SessionStartParams.create(
        template_id="inline",
        user_prompt="hi",
        initial_task_settings=NormalTaskSettings(),
        context_limit=8000,
    ))

    assert seen, "没有采集到任何事件"
    blank = sorted({t for t, o in seen if not o})
    assert blank == [], f"这些事件类型的 origin 为空：{blank}"
```

> 若 `SessionStartParams.create` 的必填参数与此处不符，以 `src/ctx_weft/core/runtime.py` 里 `create` 的实际签名为准调整；本测试只关心「跑一轮、收事件、断言 origin 非空」。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_event_origin.py -q -k nonempty_origin`
Expected: FAIL，列出一串 origin 为空的事件类型（`SessionCreated` / `TaskCreated` / `HitlOpened` 等）

- [ ] **Step 3: 实现**

每个循环外发射者在文件顶部（import 之后）加一个模块常量，然后在自己的 `_emit` 里填。

`session_manager.py`：
```python
_ORIGIN = EventOrigin.ORCHESTRATOR_SESSION_MANAGER
```
`_emit` 与 `_emit_session_event` 的 `Event(...)` 里加 `origin=_ORIGIN,`。

`task_manager.py`：
```python
_ORIGIN = EventOrigin.ORCHESTRATOR_TASK_MANAGER
```
`_emit` 的 `Event(...)` 里加 `origin=_ORIGIN,`。

`hitl/service.py`：
```python
_ORIGIN = EventOrigin.HITL_SERVICE
```

`agent_registry.py`：agent 域事件由 registry 发出，归 `runtime` 档（它不在循环内，也不是 orchestrator 的两个之一——§4 的 17 值里没有 `orchestrator.agent_registry`，用 `RUNTIME`）：
```python
_ORIGIN = EventOrigin.RUNTIME
```
四处内联 `Event(...)`（`AGENT_SPAWNED` 约 333-347、`AGENT_INSTANTIATED` 约 361、`AGENT_LLM_CHANGED` 约 395、`SPAWN_REJECTED` 约 275）各加 `origin=_ORIGIN,`。

> **注意**：`origin` 的 17 个取值是 §4 冻结的白名单，**不要新增第 18 个**。若实施中发现某发射者无法归入这 17 个，停下来记录，交用户裁定。

`runtime.py` 里 run 域事件（`RUN_STARTED` 约 2506、`RUN_INTERRUPTED` 约 2571/2610、`RUN_CANCELED` 约 2627、`RUN_FINISHED` 约 2646）——这些走 `make_event(state, ...)`，`state.origin` 此时是最后一个 step 的值，不对，须显式覆盖：
```python
origin=EventOrigin.RUNTIME,
```

`capability_gateway.py` 的 3 处与 `llm_gateway.py` 的 `_emit_retry`：它们在循环内、有 `state`，但语义上属于自己的 gateway，显式覆盖：
```python
origin=EventOrigin.LOOP_CAPABILITY_GATEWAY,   # capability_gateway.py 三处
origin=EventOrigin.LOOP_LLM_GATEWAY,          # llm_gateway.py _emit_retry
```

SnapshotWriter 所在处：`origin=EventOrigin.PERSISTENCE_SNAPSHOT_WRITER`（若它不发事件、只写快照，则跳过并在提交信息里注明）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_event_origin.py -q`
Expected: 全部 passed；`blank == []`

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ tests/unit/test_event_origin.py
git commit -m "feat(events): 循环外 5 个发射者填 origin，不变式「发射事件 origin 非空」落地"
```

---

## Phase B · LLM 事件收敛

### Task 4: `llm_gateway` 接管 6 种 `LLM_*` 的发射

**Files:**
- Modify: `src/ctx_weft/core/loop/llm_gateway.py`（`stream_llm_resilient` 约 477、`_emit_retry` 约 457）
- Modify: `src/ctx_weft/core/loop/steps/act.py`（删除 218/226/250/255/286 五处发射）
- Test: `tests/unit/test_llm_event_convergence.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `make_event(..., origin=...)`
- Produces: `stream_llm_resilient` 自己发射 `LLM_REQUEST_STARTED` / `LLM_PROMPT_SENT` / `LLM_TOKEN_STREAMED` / `LLM_REASONING_STREAMED` / `LLM_RESPONSE_FINISHED`；`act.py` 不再发任何 `LLM_*`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_llm_event_convergence.py
from __future__ import annotations

import inspect

import pytest

from ctx_weft.core.loop import llm_gateway
from ctx_weft.core.loop.steps import act
from ctx_weft.protocols.events import EventType

pytestmark = pytest.mark.asyncio

_LLM_TYPES = {
    EventType.LLM_REQUEST_STARTED,
    EventType.LLM_PROMPT_SENT,
    EventType.LLM_TOKEN_STREAMED,
    EventType.LLM_REASONING_STREAMED,
    EventType.LLM_RESPONSE_FINISHED,
    EventType.LLM_RETRY_TRIGGERED,
}


def test_act_no_longer_emits_llm_events():
    """act.py 源码里不应再出现任何 LLM_* 事件类型。"""
    src = inspect.getsource(act)
    leaked = sorted(t for t in _LLM_TYPES if f"EventType.{t.name}" in src)
    assert leaked == [], f"act.py 仍在发射：{leaked}"


def test_gateway_emits_all_six_llm_events():
    """六种 LLM_* 全部由 gateway 发射。"""
    src = inspect.getsource(llm_gateway)
    missing = sorted(t.name for t in _LLM_TYPES if f"EventType.{t.name}" not in src)
    assert missing == [], f"gateway 未接管：{missing}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q`
Expected: 两条都 FAIL

- [ ] **Step 3: 实现**

把 `act.py:206` 的 `_run_llm_turn` 里五处 `emit(make_event(state, EventType.LLM_*, ...))` **整段搬进** `stream_llm_resilient`。gateway 里已有 `state` 与 `ctx.event_bus`（`_emit_retry` 就是这么拿的），因此不需要新增参数。

在 `stream_llm_resilient` 内部，请求开始前：

```python
    bus = getattr(ctx, "event_bus", None)
    request_id = generate_id("req")
    turn = getattr(state, "turns_used", 0)

    if bus is not None and state is not None:
        await bus.emit(make_event(state, EventType.LLM_REQUEST_STARTED, {
            "request_id": request_id,
            "model": request.model,
            "llm_account": request.llm_account,
            "turn": turn,
        }))
        await bus.emit(make_event(state, EventType.LLM_PROMPT_SENT, {
            "request_id": request_id,
            "turn": turn,
            "system": request.system,
            "messages": [
                {"role": m.role, "content": redact_content_for_event(m.content)}
                for m in request.messages
            ],
            "tool_names": [t.name for t in (request.tools or [])],
        }))
```

流式过程中（在既有的 chunk 循环里，按 chunk 类型分派）：

```python
        if chunk.delta:
            await bus.emit(make_event(state, EventType.LLM_TOKEN_STREAMED,
                                      {"request_id": request_id, "delta": chunk.delta}))
        if chunk.reasoning_delta:
            await bus.emit(make_event(state, EventType.LLM_REASONING_STREAMED,
                                      {"request_id": request_id, "delta": chunk.reasoning_delta}))
```

收尾：

```python
        await bus.emit(make_event(state, EventType.LLM_RESPONSE_FINISHED, {
            "request_id": request_id,
            "content": final.content,
            "reasoning": final.reasoning,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in final.tool_calls
            ],
            "usage": final.usage,
            "llm_model": final.llm_model,
            "llm_account": final.llm_account,
            "finish_reason": final.finish_reason,
            "turn": turn,
        }))
```

> **字段名以 `act.py` 现有的五处 payload 构造为准逐字搬运**——上面是形状示意，实施时打开 `act.py:218/226/250/255/286` 照抄，避免改变 payload 契约（`LLM_*` 是 O 档，但 host SSE 在消费）。
> `request_id` 前缀统一用 `req_`：原来 `obs_` / `bgobs_` 的区分职责已由 `origin` 接管（spec §9.5）。

`act.py` 侧：删掉这五处 emit 与 `request_id` 的生成，改为从 gateway 的返回值/chunk 里取需要的部分。`_run_llm_turn` 保留其余逻辑不变。

`_emit_retry` 已有的 `origin` 覆盖（Task 3 加的）保持不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q`
Expected: 2 passed

- [ ] **Step 5: 端到端回归**

Run: `python -m pytest tests/integration -q && python -m pytest tests/unit -q`
Expected: 断言 `LLM_*` 发射点/顺序的既有测试可能红——**这类改断言**（事件从哪发变了，属于本次预期变更）。若某测试断言的是「某事件不该出现了」，停下来核对。golden 三件套照旧记录不修。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/ tests/unit/test_llm_event_convergence.py
git commit -m "refactor(llm): 6 种 LLM_* 发射收敛到 llm_gateway，act.py 不再自发射"
```

---

### Task 5: 删除 `ReactEventTypes`，background_observe 改用 origin 覆盖

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（`ReactEventTypes` 约 44-67、`run_observe_react` 约 70、发射点 103/120/139/160、`OBSERVE_REACT_EVENTS` 约 380）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（约 282-290）
- Test: `tests/unit/test_llm_event_convergence.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 gateway 发射
- Produces: `run_observe_react` 签名去掉 `event_types` 形参；`ReactEventTypes` / `OBSERVE_REACT_EVENTS` / `BACKGROUND_OBSERVE_REACT_EVENTS` 三个符号消失

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_llm_event_convergence.py
from ctx_weft.core.loop.steps import observe


def test_react_event_types_indirection_is_gone():
    """该间接层唯一的目的是区分两组事件类型，目的消失则层消失。"""
    for name in ("ReactEventTypes", "OBSERVE_REACT_EVENTS", "BACKGROUND_OBSERVE_REACT_EVENTS"):
        assert not hasattr(observe, name), f"{name} 应已删除"


def test_run_observe_react_has_no_event_types_param():
    sig = inspect.signature(observe.run_observe_react)
    assert "event_types" not in sig.parameters
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q -k react`
Expected: FAIL — 三个符号仍在

- [ ] **Step 3: 实现**

`observe.py`：
1. 删除 `ReactEventTypes` dataclass（44-67）、`OBSERVE_REACT_EVENTS`、`BACKGROUND_OBSERVE_REACT_EVENTS` 两个常量。
2. `run_observe_react` 签名去掉 `event_types` 形参。
3. 函数体内 103/120/139/160 四处发射**整段删除**——这四条现在由 Task 4 的 gateway 统一发出。
4. `round` 字段随之消失（gateway 统一用 `turn`，spec §9.4）。若 `run_observe_react` 内部仍需轮次计数，保留局部变量但不再进 payload。

`background_observe.py`（约 282-290）：调用 `run_observe_react` 时不再传 `event_types`，改为在调用前设置 state 的 origin：

```python
    state.origin = EventOrigin.LOOP_BACKGROUND_OBSERVE
    await run_observe_react(ctx, state, ...)
```

> 后台 recap 已有独立的 run_id/sequence_counter 隔离（见 commit cda055b），`origin` 同样是这个隔离 state 上的字段，不会污染主 run。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q`
Expected: 4 passed

- [ ] **Step 5: 回归**

Run: `python -m pytest tests/unit tests/integration -q && python -m ruff check src tests`
Expected: 断言 `BACKGROUND_OBSERVE_*` 出现的测试会红——下一个 Task 一并处理；先记录清单。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/steps/ tests/unit/test_llm_event_convergence.py
git commit -m "refactor(observe): 删除 ReactEventTypes 间接层，origin 接管后台/前台区分"
```

---

### Task 6: `recognize_intent` 切 `stream_llm_resilient` + 脱敏统一

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py`（裸调 `stream_llm` 约 167、`RECOGNIZE_INTENT_LLM_PROMPT` 发射约 151、`content_to_text` 约 153）
- Test: `tests/unit/test_llm_event_convergence.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 gateway 发射
- Produces: `recognize_intent` 走 `stream_llm_resilient`，自动获得 6 种 `LLM_*` 与退避重试；不再发 `RECOGNIZE_INTENT_LLM_PROMPT`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_llm_event_convergence.py
from ctx_weft.core.loop.steps import recognize_intent


def test_recognize_intent_uses_resilient_gateway():
    """裸 stream_llm 没有自愈退避——切到 resilient 顺带修掉这个缺陷。"""
    src = inspect.getsource(recognize_intent)
    assert "stream_llm_resilient" in src
    assert "content_to_text" not in src, "脱敏应统一到 redact_content_for_event"


def test_recognize_intent_no_longer_emits_its_mirror_event():
    src = inspect.getsource(recognize_intent)
    assert "RECOGNIZE_INTENT_LLM_PROMPT" not in src
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q -k recognize`
Expected: 两条 FAIL

- [ ] **Step 3: 实现**

1. import 从 `stream_llm` 改成 `stream_llm_resilient`，调用点（约 167）相应改为 `stream_llm_resilient(ctx, state, request)`。
2. 删除 `RECOGNIZE_INTENT_LLM_PROMPT` 的发射（约 151）及其 payload 构造（约 153 的 `content_to_text`）。
3. 若 `content_to_text` 在本文件已无其他用处，删掉该 import。
4. `state.origin` 由 Task 2 的 `_STEP_ORIGIN` 自动填成 `loop.recognize_intent`，无需手写。
5. `RECOGNIZE_INTENT_STARTED` / `_SKIPPED` / `_TOOL_CALL` / `_COMPLETED` 四个事件**保留不动**——它们是该 step 自己的语义事实，不是 LLM 镜像。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q`
Expected: 6 passed

- [ ] **Step 5: 回归**

Run: `python -m pytest tests/unit -q -k "recognize or intent"` 然后 `python -m pytest -q`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/loop/steps/recognize_intent.py tests/unit/test_llm_event_convergence.py
git commit -m "refactor(recognize_intent): 切 stream_llm_resilient，删镜像事件，脱敏统一"
```

---

### Task 7: 5 个镜像事件进 L 档 + 白名单落地

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（新增 `L_TIER_EVENT_TYPES`）
- Modify: `src/ctx_weft/core/control/reducers.py`（确认旧分支保留）
- Test: `tests/unit/test_llm_event_convergence.py`（追加）

**Interfaces:**
- Consumes: Task 4-6
- Produces: `L_TIER_EVENT_TYPES: frozenset[str]` —— 显式白名单，供不变式 1 的测试使用

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_llm_event_convergence.py
from ctx_weft.protocols.events import EVENT_TYPES, L_TIER_EVENT_TYPES

_RETIRED_THIS_CHANGE = {
    "RecognizeIntentLlmPrompt",
    "BackgroundObserveRequestStarted",
    "BackgroundObservePromptSent",
    "BackgroundObserveTokenStreamed",
    "BackgroundObserveResponseFinished",
}


def test_retired_mirrors_are_in_l_tier():
    """§5：只删发射，不删枚举——枚举值仍在，但进 L 档白名单。"""
    assert _RETIRED_THIS_CHANGE <= set(L_TIER_EVENT_TYPES)


def test_retired_mirrors_still_in_event_types():
    """不变式 1：EventType 全集 ≡ 实际发射 ∪ L 档。枚举值不删。"""
    assert _RETIRED_THIS_CHANGE <= set(EVENT_TYPES)


def test_l_tier_not_emitted_anywhere_in_core():
    """外加条：L 档 ∩ 实际发射集合 = ∅。"""
    import pathlib
    core = pathlib.Path("src/ctx_weft/core")
    hits = []
    for py in core.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for name in _RETIRED_THIS_CHANGE:
            # 枚举成员名形如 EventType.BACKGROUND_OBSERVE_PROMPT_SENT
            member = "".join("_" + c if c.isupper() else c.upper() for c in name).lstrip("_")
            if f"EventType.{member}" in text:
                hits.append(f"{py}:{member}")
    assert hits == [], f"L 档事件仍在被发射：{hits}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q -k l_tier`
Expected: FAIL — `ImportError: cannot import name 'L_TIER_EVENT_TYPES'`

- [ ] **Step 3: 实现**

在 `protocols/events.py` 加显式白名单（把 2026-09-02 已进 L 档的 9 个一并写进来，凑成 §5 的完整清单 + 本次 5 个）：

```python
# ── L 档：曾发射、现已停发、重放仍须认识（docs/events-v2.md §5）─────────────
# 只读白名单。不得再发射；删除枚举值须过 §5 的两级退役闸门。
L_TIER_EVENT_TYPES: frozenset[str] = frozenset({
    # §5.1 HITL 那批 · 8 个（2026-09-02 已入档）
    EventType.HITL_REQUIRED,
    EventType.HITL_APPROVED,
    EventType.HITL_MODIFIED,
    EventType.HITL_ANSWERED,
    EventType.HITL_REJECTED,
    EventType.HITL_CANCELLED,
    EventType.HITL_TIMEOUT,
    EventType.SESSION_PAUSED_HITL,
    # §5.2 · 1 个（2026-09-02 已入档）
    EventType.SESSION_STATUS_CHANGED,
    # 本次新入档：LLM 镜像 5 个（spec §9）
    EventType.RECOGNIZE_INTENT_LLM_PROMPT,
    EventType.BACKGROUND_OBSERVE_REQUEST_STARTED,
    EventType.BACKGROUND_OBSERVE_PROMPT_SENT,
    EventType.BACKGROUND_OBSERVE_TOKEN_STREAMED,
    EventType.BACKGROUND_OBSERVE_RESPONSE_FINISHED,
})
```

> 若 `EventType.HITL_TIMEOUT` 等成员名与实际不符，以 `events.py` 里的真实成员名为准。

`reducers.py`：确认这 5 个类型的 `_apply` 分支**存在且保留**。若原本没有分支（它们是 O 档、reducer 不折叠），则无需新增——在提交信息里注明「O 档入 L 档，reducer 无分支需保留」。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_llm_event_convergence.py -q`
Expected: 9 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/core/control/reducers.py tests/
git commit -m "feat(events): L_TIER_EVENT_TYPES 显式白名单，5 个 LLM 镜像事件入档"
```

---

## Phase C · TaskManager 信封补 agent_id

### Task 8: `TaskManager._emit` 填 `agent_id`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`_emit` 约 1119）
- Test: `tests/unit/test_agent_lifecycle.py`（新建，先放这一条）

**Interfaces:**
- Consumes: 无
- Produces: 所有 `TASK_*` 事件的信封带 `agent_id` —— **Phase D 的 ALM 依赖这个**

> **为什么必须先做这个**：`TaskManager._emit` 目前只传 `task_id`，从不设 `agent_id`。ALM 若从 `ev.agent_id` 读会永远拿到 `None`。V2 §0 明说「envelope 管身份」，所以正解是补信封而不是让 ALM 去翻 payload。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_lifecycle.py
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Task
from ctx_weft.protocols.events import EventType

pytestmark = pytest.mark.asyncio


class _SpyBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)

    def subscribe(self, _flt, _handler) -> None:
        pass


async def test_task_events_carry_agent_id_on_envelope():
    """V2 §0：envelope 管身份。ALM 靠 ev.agent_id 定位 agent。"""
    bus = _SpyBus()
    tm = TaskManager("s1", event_bus=bus)
    task = Task(id="t1", session_id="s1", status="PENDING", assigned_agent_id="a1")
    await tm.push_task(task)

    created = [e for e in bus.events if e.type == EventType.TASK_CREATED]
    assert created, "没有 TaskCreated"
    assert created[0].agent_id == "a1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: FAIL — `assert None == 'a1'`

- [ ] **Step 3: 实现**

`_emit` 加一个可选参数并填进信封：

```python
    async def _emit(
        self,
        event_type: EventType,
        task_id: str | None = None,
        payload: dict | None = None,
        *,
        agent_id: str | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
        tenant_id = self._session.tenant_id if self._session else "default"
        if agent_id is None and task_id is not None:
            agent_id = self._agent_id_of(task_id)
        await self._event_bus.emit(Event(
            id=generate_id("evt"), run_id=None, sequence=0,
            session_id=self._session_id, type=event_type, timestamp=now_utc(),
            tenant_id=tenant_id, task_id=task_id, agent_id=agent_id,
            origin=_ORIGIN,
            payload=payload or {},
        ))
```

新增私有查询（放在 `running_agent_of` 附近）：

```python
    def _agent_id_of(self, task_id: str) -> str | None:
        """先看正在跑的登记，再回落到 task 自己的 assigned_agent_id。"""
        running = self._running_agents.get(task_id)
        if running:
            return running
        task = self._tasks.get(task_id)
        return task.assigned_agent_id if task is not None else None
```

> `self._tasks` 是 TaskManager 持有的 task 表；实施时确认其真实属性名（`grep -n "self\._tasks" src/ctx_weft/core/orchestrator/task_manager.py`），不一致就按实际改。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 1 passed

- [ ] **Step 5: 回归**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: 信封多填一个字段，不应影响既有断言。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_agent_lifecycle.py
git commit -m "fix(task_manager): _emit 信封补 agent_id（V2 §0 envelope 管身份）"
```

---

## Phase D · AgentLifecycleManager

### Task 9: agent 五态机（纯函数层）

**Files:**
- Create: `src/ctx_weft/core/orchestrator/agent_state.py`
- Test: `tests/unit/test_agent_state_machine.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `AgentStatus = Literal["idle","running","waiting_human","interrupted","terminated"]`
  - `AgentInput(StrEnum)`：`TASK_STARTED` `AWAITING_HUMAN` `HUMAN_RESOLVED` `INTERRUPTED` `RESUMED` `SETTLED` `CANCEL`
  - `AgentTransition`（frozen dataclass）：`status: str`、`event_type: str`、`payload: dict`
  - `next_agent_transition(current, inp, *, task_id=None, hitl_id="", reason="", cascaded_from=None) -> AgentTransition | None`
  - `TERMINAL_AGENT_STATUSES: frozenset[str]`

> 与 `session_state.py` 同构：纯函数、无副作用、不碰事件总线。副作用全在 ALM。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_state_machine.py
from __future__ import annotations

from ctx_weft.core.orchestrator.agent_state import (
    TERMINAL_AGENT_STATUSES,
    AgentInput,
    next_agent_transition,
)


def test_idle_to_running_on_task_started():
    t = next_agent_transition("idle", AgentInput.TASK_STARTED, task_id="t1")
    assert t is not None
    assert t.status == "running"
    assert t.event_type == "AgentRunning"
    assert t.payload["trigger"] == "task_started"
    assert t.payload["from_status"] == "idle"


def test_running_to_waiting_human():
    t = next_agent_transition("running", AgentInput.AWAITING_HUMAN, hitl_id="h1", task_id="t1")
    assert t.status == "waiting_human"
    assert t.event_type == "AgentWaitingHuman"
    assert t.payload["hitl_id"] == "h1"


def test_waiting_human_back_to_running():
    t = next_agent_transition("waiting_human", AgentInput.HUMAN_RESOLVED, task_id="t1")
    assert t.status == "running"
    assert t.payload["trigger"] == "human_replied"


def test_pause_resume_round_trip():
    a = next_agent_transition("running", AgentInput.INTERRUPTED, reason="llm_outage", task_id="t1")
    assert a.status == "interrupted"
    b = next_agent_transition("interrupted", AgentInput.RESUMED, task_id="t1")
    assert b.status == "running"
    assert b.payload["trigger"] == "resumed"


def test_task_terminal_returns_agent_to_idle_not_terminal():
    """task 终态 != agent 终态——agent 回 idle 等下一条消息。"""
    t = next_agent_transition("running", AgentInput.SETTLED, reason="task_finished")
    assert t.status == "idle"
    assert t.event_type == "AgentIdle"
    assert t.status not in TERMINAL_AGENT_STATUSES


def test_only_cancel_reaches_terminated():
    for src in ("idle", "running", "waiting_human", "interrupted"):
        t = next_agent_transition(src, AgentInput.CANCEL, reason="user", cascaded_from=None)
        assert t.status == "terminated"
        assert t.event_type == "AgentTerminated"
    assert "terminated" in TERMINAL_AGENT_STATUSES


def test_terminated_is_absorbing():
    for inp in AgentInput:
        assert next_agent_transition("terminated", inp) is None


def test_no_op_transitions_return_none():
    """同态输入不产生转移，避免刷屏。"""
    assert next_agent_transition("idle", AgentInput.SETTLED) is None
    assert next_agent_transition("running", AgentInput.TASK_STARTED) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_state_machine.py -q`
Expected: FAIL — `ModuleNotFoundError: ctx_weft.core.orchestrator.agent_state`

- [ ] **Step 3: 实现**

```python
# src/ctx_weft/core/orchestrator/agent_state.py
"""agent 五态机的纯函数层。

与 `session_state.py` 同构：只做「当前状态 + 输入 -> 下一状态 + 该发哪条事件」，
不碰事件总线、不持有实例状态。副作用全在 `AgentRegistry`（lifecycle manager）。

设计要点（spec 3.1）：
- `idle` 既是初始态，也是每轮交互处理完后回到的态；它**不等于**「没有活着的 task」。
- task 终态（finished/failed/canceled/finalized）让 agent 回 `idle` 而**不是**终态——
  agent 是跨多轮的容器，可以接新消息开新 task。
- 真正的终态 `terminated` **只**由外部显式 cancel 触发。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

AgentStatus = Literal["idle", "running", "waiting_human", "interrupted", "terminated"]

TERMINAL_AGENT_STATUSES: frozenset[str] = frozenset({"terminated"})


class AgentInput(StrEnum):
    """状态机输入。由 ALM 从 TASK_* 事件类型翻译而来——判据是类型，不是 payload 文本。"""

    TASK_STARTED = "task_started"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN_RESOLVED = "human_resolved"
    INTERRUPTED = "interrupted"
    RESUMED = "resumed"
    SETTLED = "settled"
    CANCEL = "cancel"


@dataclass(frozen=True)
class AgentTransition:
    status: str
    event_type: str
    payload: dict = field(default_factory=dict)


def next_agent_transition(
    current: str,
    inp: AgentInput,
    *,
    task_id: str | None = None,
    hitl_id: str = "",
    reason: str = "",
    cascaded_from: str | None = None,
) -> AgentTransition | None:
    """返回 None 表示不转移（同态输入，或已在终态）。"""
    if current in TERMINAL_AGENT_STATUSES:
        return None

    if inp is AgentInput.CANCEL:
        return AgentTransition(
            "terminated",
            "AgentTerminated",
            {"from_status": current, "reason": reason, "cascaded_from": cascaded_from},
        )

    if inp is AgentInput.TASK_STARTED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "task_started"},
        )

    if inp is AgentInput.AWAITING_HUMAN:
        if current == "waiting_human":
            return None
        return AgentTransition(
            "waiting_human",
            "AgentWaitingHuman",
            {"from_status": current, "hitl_id": hitl_id, "task_id": task_id},
        )

    if inp is AgentInput.HUMAN_RESOLVED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "human_replied"},
        )

    if inp is AgentInput.INTERRUPTED:
        if current == "interrupted":
            return None
        return AgentTransition(
            "interrupted",
            "AgentInterrupted",
            {"from_status": current, "reason": reason, "task_id": task_id},
        )

    if inp is AgentInput.RESUMED:
        if current == "running":
            return None
        return AgentTransition(
            "running",
            "AgentRunning",
            {"from_status": current, "task_id": task_id, "trigger": "resumed"},
        )

    if inp is AgentInput.SETTLED:
        if current == "idle":
            return None
        return AgentTransition(
            "idle",
            "AgentIdle",
            {"from_status": current, "reason": reason},
        )

    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_state_machine.py -q`
Expected: 8 passed

- [ ] **Step 5: lint**

Run: `python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/agent_state.py tests/unit/test_agent_state_machine.py
git commit -m "feat(agent): 五态机纯函数层 agent_state.py"
```

---

### Task 10: 5 个 `AGENT_*` 事件类型入枚举并选边进 S 档

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`
- Test: `tests/unit/test_agent_lifecycle.py`（追加）

**Interfaces:**
- Consumes: Task 9 的 `AgentTransition.event_type` 字符串
- Produces: `EventType.AGENT_RUNNING` / `AGENT_IDLE` / `AGENT_WAITING_HUMAN` / `AGENT_INTERRUPTED` / `AGENT_TERMINATED`，且已进 `STATE_EVENT_TYPES`

- [ ] **Step 1: 复核命名未被复用（events-v2.md §7 硬规则）**

Run:
```bash
git log -S "AgentRunning" --oneline | head
git log -S "AgentIdle" --oneline | head
git log -S "AgentWaitingHuman" --oneline | head
git log -S "AgentInterrupted" --oneline | head
git log -S "AgentTerminated" --oneline | head
```
Expected: 五条都**无输出**。若任一有输出——该字符串历史上发射过，**停下来**，按 §7 换名并回报用户。

- [ ] **Step 2: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
from ctx_weft.protocols.events import (
    EVENT_TYPES,
    L_TIER_EVENT_TYPES,
    STATE_EVENT_TYPES,
    EventType,
)

_NEW_AGENT_TYPES = {
    EventType.AGENT_RUNNING,
    EventType.AGENT_IDLE,
    EventType.AGENT_WAITING_HUMAN,
    EventType.AGENT_INTERRUPTED,
    EventType.AGENT_TERMINATED,
}


def test_new_agent_types_registered():
    assert _NEW_AGENT_TYPES <= set(EVENT_TYPES)


def test_new_agent_types_are_s_tier():
    """不变式 2：加一个枚举值就得显式选边。ALM 状态被 reducer 折叠 -> S 档。"""
    assert _NEW_AGENT_TYPES <= set(STATE_EVENT_TYPES)
    assert not (_NEW_AGENT_TYPES & set(L_TIER_EVENT_TYPES))


def test_agent_event_wire_values_are_pascal_case():
    assert EventType.AGENT_RUNNING == "AgentRunning"
    assert EventType.AGENT_IDLE == "AgentIdle"
    assert EventType.AGENT_WAITING_HUMAN == "AgentWaitingHuman"
    assert EventType.AGENT_INTERRUPTED == "AgentInterrupted"
    assert EventType.AGENT_TERMINATED == "AgentTerminated"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k agent_types`
Expected: FAIL — `AttributeError: AGENT_RUNNING`

- [ ] **Step 4: 实现**

在 `EventType` 的 Agent 域（`AGENT_INSTANTIATED` 附近）加 5 个成员：

```python
    # agent 生命周期状态（spec 3.3）。ALM 是唯一发射者。
    AGENT_RUNNING = "AgentRunning"
    AGENT_IDLE = "AgentIdle"
    AGENT_WAITING_HUMAN = "AgentWaitingHuman"
    AGENT_INTERRUPTED = "AgentInterrupted"
    AGENT_TERMINATED = "AgentTerminated"
```

在 `STATE_EVENT_TYPES` 集合里加上这 5 个。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 4 passed（含 Task 8 那条）

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/events.py tests/unit/test_agent_lifecycle.py
git commit -m "feat(events): 新增 5 个 AGENT_* 状态事件类型，选边进 S 档"
```

---

### Task 11: `_AgentRecord` 加状态字段 + parent→children 索引

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`
- Test: `tests/unit/test_agent_lifecycle.py`（追加）

**Interfaces:**
- Consumes: Task 9 的 `AgentStatus`
- Produces:
  - `_AgentRecord.status: str = "idle"`、`_AgentRecord.current_task_id: str | None = None`
  - `AgentRegistry._children: dict[str, set[str]]`
  - `children_of(agent_id) -> set[str]`、`descendants_of(agent_id) -> list[str]`、`status_of(agent_id) -> str`、`agent_ids_of_session(session_id) -> list[str]`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
from ctx_weft.core.orchestrator.agent_registry import AgentRegistry, _AgentRecord


def _reg() -> AgentRegistry:
    return AgentRegistry(
        template_lookup=None, event_bus=_SpyBus(), model_resolver=lambda a, m: None
    )


def _plant(reg: AgentRegistry, agent_id: str, parent: str | None, session_id: str = "s1") -> None:
    """直接种记录，绕开 instantiate 的模板依赖。"""
    reg._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=None, loop_config=None,
    )
    if parent is not None:
        reg._children.setdefault(parent, set()).add(agent_id)


def test_record_defaults_to_idle_with_no_task():
    reg = _reg()
    _plant(reg, "root", None)
    assert reg.status_of("root") == "idle"
    assert reg._agents["root"].current_task_id is None


def test_children_and_descendants():
    reg = _reg()
    _plant(reg, "root", None)
    _plant(reg, "kid1", "root")
    _plant(reg, "kid2", "root")
    _plant(reg, "grandkid", "kid1")

    assert reg.children_of("root") == {"kid1", "kid2"}
    assert set(reg.descendants_of("root")) == {"kid1", "kid2", "grandkid"}
    assert reg.descendants_of("grandkid") == []


def test_descendants_tolerates_cycle():
    """防御性：父子关系理论上无环，索引损坏时也不能死循环。"""
    reg = _reg()
    _plant(reg, "a", None)
    _plant(reg, "b", "a")
    reg._children.setdefault("b", set()).add("a")
    assert set(reg.descendants_of("a")) == {"b"}


def test_agent_ids_of_session():
    reg = _reg()
    _plant(reg, "a", None, session_id="s1")
    _plant(reg, "b", None, session_id="s2")
    assert reg.agent_ids_of_session("s1") == ["a"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k "descendants or idle_with_no_task"`
Expected: FAIL — `_AgentRecord` 无 `status`；`AgentRegistry` 无 `_children`

- [ ] **Step 3: 实现**

`_AgentRecord` 末尾加两个带默认值的字段：

```python
    llm: ModelChoice = field(default_factory=ModelChoice)
    status: str = "idle"                     # spec 3.1 五态机的当前值
    current_task_id: str | None = None       # 消息路由据此判断新建还是复用 task
```

`AgentRegistry` 加索引字段（放在 `_sessions` 之后）：

```python
    _children: dict[str, set[str]] = field(default_factory=dict)
```

加查询方法：

```python
    def status_of(self, agent_id: str) -> str:
        rec = self._agents.get(agent_id)
        return rec.status if rec is not None else "terminated"

    def children_of(self, agent_id: str) -> set[str]:
        return set(self._children.get(agent_id, ()))

    def descendants_of(self, agent_id: str) -> list[str]:
        """深度优先展开全部子孙，不含自己。带 seen 集防御索引成环。"""
        out: list[str] = []
        seen: set[str] = {agent_id}
        stack = list(self._children.get(agent_id, ()))
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            out.append(cur)
            stack.extend(self._children.get(cur, ()))
        return out

    def agent_ids_of_session(self, session_id: str) -> list[str]:
        return [k for k, r in self._agents.items() if r.session_id == session_id]
```

在 `instantiate` 里落 record 之后维护索引：

```python
        if parent_agent_id is not None:
            self._children.setdefault(parent_agent_id, set()).add(agent_id)
```

`release_session` 里清理：把该 session 的 agent 从 `_children` 的键与所有值集合中摘除。
同时把 `set_session_llm`（约 415）与 `release_session`（约 125）两处线性扫描改为调用 `agent_ids_of_session`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 8 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/agent_registry.py tests/unit/test_agent_lifecycle.py
git commit -m "feat(agent): _AgentRecord 加 status/current_task_id，registry 建 parent-children 索引"
```

---

### Task 12: ALM 订阅 `TASK_*` 驱动转移并发 `AGENT_*`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`
- Modify: `src/ctx_weft/core/runtime.py`（构造后调 `attach_to_bus()`，约 493-614）
- Test: `tests/unit/test_agent_lifecycle.py`（追加）

**Interfaces:**
- Consumes: Task 8 的信封 `agent_id`、Task 9 状态机、Task 10 事件类型、Task 11 字段
- Produces:
  - `AgentRegistry._INPUT_BY_EVENT: ClassVar[dict[str, AgentInput]]`
  - `attach_to_bus() -> None`、`handle_event(ev: Event) -> None`
  - `apply_input(agent_id, inp, *, task_id=None, hitl_id="", reason="", cascaded_from=None) -> bool` —— 状态转移与事件发射的唯一入口，Phase F 的 cancel/pause/resume 都走它

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
from datetime import UTC, datetime

from ctx_weft.core.orchestrator.agent_state import AgentInput
from ctx_weft.protocols.events import Event


def _task_ev(t: str, agent_id: str, payload: dict | None = None) -> Event:
    return Event(
        id="evt_x", run_id=None, sequence=0, session_id="s1", type=t,
        timestamp=datetime.now(UTC), task_id="t1", agent_id=agent_id,
        payload=payload or {},
    )


async def test_task_started_drives_agent_to_running_and_emits():
    reg = _reg()
    _plant(reg, "a1", None)
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1", {"assigned_agent_id": "a1"}))

    assert reg.status_of("a1") == "running"
    emitted = [e for e in reg.event_bus.events if e.type == EventType.AGENT_RUNNING]
    assert len(emitted) == 1
    assert emitted[0].agent_id == "a1"
    assert emitted[0].session_id == "s1"
    assert emitted[0].payload["trigger"] == "task_started"


async def test_awaiting_human_then_resolved():
    reg = _reg()
    _plant(reg, "a1", None)
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1"))
    await reg.handle_event(_task_ev(EventType.TASK_AWAITING_HUMAN, "a1", {"hitl_id": "h1"}))
    assert reg.status_of("a1") == "waiting_human"

    await reg.handle_event(_task_ev(EventType.TASK_HUMAN_RESOLVED, "a1", {"hitl_id": "h1"}))
    assert reg.status_of("a1") == "running"


async def test_task_terminal_returns_to_idle_not_terminated():
    """spec 3.1：task 终态不是 agent 终态。"""
    for t in (
        EventType.TASK_FINISHED,
        EventType.TASK_FAILED,
        EventType.TASK_CANCELED,
        EventType.TASK_FINALIZED,
        EventType.TASK_REQUEUED,
        EventType.TASK_SUSPENDED,
    ):
        reg = _reg()
        _plant(reg, "a1", None)
        await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1"))
        await reg.handle_event(_task_ev(t, "a1"))
        assert reg.status_of("a1") == "idle", f"{t} 应回 idle"


async def test_unknown_agent_id_is_ignored():
    reg = _reg()
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "ghost"))
    assert reg.event_bus.events == []


async def test_current_task_id_tracked():
    reg = _reg()
    _plant(reg, "a1", None)
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1"))
    assert reg._agents["a1"].current_task_id == "t1"


async def test_no_duplicate_event_on_same_state_input():
    reg = _reg()
    _plant(reg, "a1", None)
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1"))
    await reg.handle_event(_task_ev(EventType.TASK_STARTED, "a1"))
    running = [e for e in reg.event_bus.events if e.type == EventType.AGENT_RUNNING]
    assert len(running) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k drives_agent`
Expected: FAIL — `AgentRegistry` 无 `handle_event`

- [ ] **Step 3: 实现**

模块级映射（放在类定义之前）：

```python
_SETTLE_REASON: dict[str, str] = {
    EventType.TASK_FINISHED: "task_finished",
    EventType.TASK_FAILED: "task_failed",
    EventType.TASK_CANCELED: "task_canceled",
    EventType.TASK_FINALIZED: "task_finalized",
    EventType.TASK_REQUEUED: "task_requeued",
    EventType.TASK_SUSPENDED: "task_suspended",
}
```

`AgentRegistry` 内新增：

```python
    _INPUT_BY_EVENT: ClassVar[dict[str, AgentInput]] = {
        EventType.TASK_STARTED: AgentInput.TASK_STARTED,
        EventType.TASK_AWAITING_HUMAN: AgentInput.AWAITING_HUMAN,
        EventType.TASK_HUMAN_RESOLVED: AgentInput.HUMAN_RESOLVED,
        EventType.TASK_INTERRUPTED: AgentInput.INTERRUPTED,
        EventType.TASK_RESUMED: AgentInput.RESUMED,
        # 以下六种一律回 idle——task 终态不是 agent 终态（spec 3.1）
        EventType.TASK_FINISHED: AgentInput.SETTLED,
        EventType.TASK_FAILED: AgentInput.SETTLED,
        EventType.TASK_CANCELED: AgentInput.SETTLED,
        EventType.TASK_FINALIZED: AgentInput.SETTLED,
        EventType.TASK_REQUEUED: AgentInput.SETTLED,
        EventType.TASK_SUSPENDED: AgentInput.SETTLED,
    }

    def attach_to_bus(self) -> None:
        self.event_bus.subscribe(None, self.handle_event)

    async def handle_event(self, ev: Event) -> None:
        inp = self._INPUT_BY_EVENT.get(ev.type)
        if inp is None:
            return
        agent_id = ev.agent_id or (ev.payload or {}).get("assigned_agent_id")
        if not agent_id or agent_id not in self._agents:
            return
        if ev.task_id:
            self._agents[agent_id].current_task_id = ev.task_id
        p = ev.payload or {}
        await self.apply_input(
            agent_id,
            inp,
            task_id=ev.task_id,
            hitl_id=str(p.get("hitl_id", "")),
            reason=str(p.get("reason", "")) or _SETTLE_REASON.get(ev.type, ""),
        )

    async def apply_input(
        self,
        agent_id: str,
        inp: AgentInput,
        *,
        task_id: str | None = None,
        hitl_id: str = "",
        reason: str = "",
        cascaded_from: str | None = None,
    ) -> bool:
        """状态转移与事件发射的唯一入口。返回是否真的发生了转移。"""
        rec = self._agents.get(agent_id)
        if rec is None:
            return False
        tr = next_agent_transition(
            rec.status, inp,
            task_id=task_id, hitl_id=hitl_id, reason=reason, cascaded_from=cascaded_from,
        )
        if tr is None:
            return False
        rec.status = tr.status
        await self.event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=rec.session_id,
            type=tr.event_type,
            timestamp=now_utc(),
            tenant_id=rec.tenant_id,
            task_id=task_id,
            agent_id=agent_id,
            origin=_ORIGIN,
            payload=dict(tr.payload),
        ))
        return True
```

`runtime.py` 构造处（`self._agent_registry = AgentRegistry(...)` 之后）加一行：

```python
        self._agent_registry.attach_to_bus()
```

> **订阅顺序**：`attach_persistence` 已在更早处调用，保持不动（EventPersister 必须先订阅）。ALM 与 SessionManager 之间无顺序依赖——ALM 消费 `TASK_*`，SM（Task 15 后）消费 `AGENT_*`；同步 drain 下 ALM 发出的 `AGENT_*` 在其 `emit` 内被 SM 收到，不成环。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 14 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/agent_registry.py src/ctx_weft/core/runtime.py tests/unit/test_agent_lifecycle.py
git commit -m "feat(agent): ALM 订阅 TASK_* 驱动五态机并发 AGENT_* 事件"
```

---

### Task 13: 接收守卫 `assert_can_receive`

**Files:**
- Modify: `src/ctx_weft/core/errors.py`
- Modify: `src/ctx_weft/core/orchestrator/agent_registry.py`
- Test: `tests/unit/test_agent_lifecycle.py`（追加）

**Interfaces:**
- Consumes: Task 11 的 `status_of`
- Produces: `UnknownAgentError` / `AgentBusyError` / `AgentTerminatedError`（均继承 `CtxWeftError`）；`assert_can_receive(agent_id: str) -> None`

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
import pytest as _pytest

from ctx_weft.core.errors import AgentBusyError, AgentTerminatedError, UnknownAgentError


def test_guard_allows_idle_and_waiting_human():
    reg = _reg()
    _plant(reg, "a1", None)
    reg.assert_can_receive("a1")
    reg._agents["a1"].status = "waiting_human"
    reg.assert_can_receive("a1")


def test_guard_rejects_running():
    """spec 4.1：忙碌直接报错，不排队。"""
    reg = _reg()
    _plant(reg, "a1", None)
    reg._agents["a1"].status = "running"
    with _pytest.raises(AgentBusyError):
        reg.assert_can_receive("a1")


def test_guard_rejects_terminated_and_unknown():
    reg = _reg()
    _plant(reg, "a1", None)
    reg._agents["a1"].status = "terminated"
    with _pytest.raises(AgentTerminatedError):
        reg.assert_can_receive("a1")
    with _pytest.raises(UnknownAgentError):
        reg.assert_can_receive("ghost")


def test_guard_allows_interrupted():
    """interrupted 是可恢复态，不拒收——resume 后继续处理。"""
    reg = _reg()
    _plant(reg, "a1", None)
    reg._agents["a1"].status = "interrupted"
    reg.assert_can_receive("a1")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k guard`
Expected: FAIL — `ImportError: cannot import name 'AgentBusyError'`

- [ ] **Step 3: 实现**

`core/errors.py`（跟随已有 `CtxWeftError` 子类写法）：

```python
class UnknownAgentError(CtxWeftError):
    """agent_id 不存在于 registry。"""


class AgentBusyError(CtxWeftError):
    """agent 正在执行（running），拒收新消息。调用方自行重试，或先 pause/cancel。"""


class AgentTerminatedError(CtxWeftError):
    """agent 已被显式 cancel，不再接受任何输入。"""
```

`agent_registry.py`：

```python
    def assert_can_receive(self, agent_id: str) -> None:
        """外部消息投递前的同步守卫。判断逻辑收敛在此一处（spec 3.5）。"""
        rec = self._agents.get(agent_id)
        if rec is None:
            raise UnknownAgentError(f"unknown agent: {agent_id}")
        if rec.status == "terminated":
            raise AgentTerminatedError(f"agent {agent_id} already terminated")
        if rec.status == "running":
            raise AgentBusyError(
                f"agent {agent_id} is running; retry later or pause/cancel it first"
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/errors.py src/ctx_weft/core/orchestrator/agent_registry.py tests/unit/test_agent_lifecycle.py
git commit -m "feat(agent): assert_can_receive 接收守卫，忙碌直接报错不排队"
```

---

### Task 14: reducers 折叠 `AGENT_*`

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（`_apply` 约 380 起）
- Modify: `AgentView` 定义处（先 `grep -rn "class AgentView" src/`）
- Test: `tests/unit/test_agent_lifecycle.py`（追加）

**Interfaces:**
- Consumes: Task 10 的事件类型
- Produces: `AgentView.status` / `AgentView.current_task_id`；`rebuild_view` 冷重建与运行时内存态等价

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
from ctx_weft.core.control.reducers import rebuild_view


class _MemStore:
    def __init__(self, events):
        self._events = events

    async def read_by_session(self, session_id, **_kw):
        return list(self._events)


async def test_rebuild_view_folds_agent_status():
    """不变式 3：S 档事件必须被 reducer 折叠，冷重建与内存态等价。"""
    evs = [
        _task_ev(EventType.AGENT_INSTANTIATED, "a1", {"template_id": "tpl"}),
        _task_ev(EventType.AGENT_RUNNING, "a1", {"from_status": "idle", "trigger": "task_started"}),
        _task_ev(EventType.AGENT_WAITING_HUMAN, "a1", {"from_status": "running", "hitl_id": "h1"}),
    ]
    view = await rebuild_view(_MemStore(evs), "s1")
    assert view.agents["a1"].status == "waiting_human"


async def test_rebuild_view_terminated_is_sticky():
    evs = [
        _task_ev(EventType.AGENT_INSTANTIATED, "a1", {"template_id": "tpl"}),
        _task_ev(EventType.AGENT_TERMINATED, "a1", {"from_status": "idle", "reason": "user"}),
        _task_ev(EventType.AGENT_RUNNING, "a1", {"from_status": "idle"}),
    ]
    view = await rebuild_view(_MemStore(evs), "s1")
    assert view.agents["a1"].status == "terminated"
```

> `rebuild_view` 与 store 的实际签名以 `reducers.py:289` 为准；若为同步函数则去掉 `await`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k rebuild_view`
Expected: FAIL — `AgentView` 无 `status`

- [ ] **Step 3: 实现**

1. `AgentView` 加两个带默认值的字段：

```python
    status: str = "idle"
    current_task_id: str | None = None
```

2. `reducers.py` 模块级加映射：

```python
_AGENT_STATUS_BY_EVENT: dict[str, str] = {
    EventType.AGENT_RUNNING: "running",
    EventType.AGENT_IDLE: "idle",
    EventType.AGENT_WAITING_HUMAN: "waiting_human",
    EventType.AGENT_INTERRUPTED: "interrupted",
    EventType.AGENT_TERMINATED: "terminated",
}
```

3. `_apply` 加分支：

```python
    if ev.type in _AGENT_STATUS_BY_EVENT:
        agent = view.agents.get(ev.agent_id or "")
        if agent is not None and agent.status != "terminated":
            agent.status = _AGENT_STATUS_BY_EVENT[ev.type]
            if ev.task_id:
                agent.current_task_id = ev.task_id
        return
```

> `terminated` 粘滞：一旦终态就不被迟到事件改回，与 SessionManager 已有的「已终态就不再转移」同构。

4. **确认 9 个停发类型的旧分支原样保留**——`SESSION_RUNNING`/`WAITING`/`INTERRUPTED`/`FINISHED` 的 `_apply` 分支这轮**不动**（Task 16 才停发；reducer 分支永久保留供重放，见 events-v2.md §5）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 20 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/control/reducers.py src/ctx_weft/core/state/ tests/unit/test_agent_lifecycle.py
git commit -m "feat(reducers): 折叠 5 个 AGENT_* 事件，AgentView 带 status/current_task_id"
```

---

## Phase E · SessionManager 降格

### Task 15: `_SessionState` 换成成员集合，改订阅 `AGENT_*`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`
- Test: `tests/unit/test_session_manager_state.py`（改写既有用例）

**Interfaces:**
- Consumes: Task 10 的 `AGENT_INSTANTIATED`（既有）/ `AGENT_SPAWNED`（既有）
- Produces:
  - `_SessionState`：`tenant_id: str = "default"`、`agent_ids: set[str]`（**去掉 `status`**）
  - `SessionManager.agent_ids_of(session_id) -> set[str]`
  - `_INPUT_BY_EVENT` 整个删除；`handle_event` 改为只维护成员集合

> `SessionManager` 保留 `create_session` / `resume_session` / `_make_root_task_manager` 等既有职责不动——本任务只拆掉「状态机」那一半。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_session_manager_state.py —— 整体改写
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.core.orchestrator.session_manager import SessionManager, _SessionState
from ctx_weft.protocols.events import Event, EventType

pytestmark = pytest.mark.asyncio


class _SpyBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)

    def subscribe(self, _flt, _handler) -> None:
        pass


def _sm() -> SessionManager:
    return SessionManager(agent_registry=None, event_bus=_SpyBus())


def _agent_ev(t: str, agent_id: str, payload: dict | None = None) -> Event:
    return Event(
        id="evt_x", run_id=None, sequence=0, session_id="s1", type=t,
        timestamp=datetime.now(UTC), agent_id=agent_id, payload=payload or {},
    )


def test_session_state_has_no_status_field():
    """状态整体挪到 agent 身上（spec 2）。"""
    st = _SessionState()
    assert not hasattr(st, "status")
    assert st.agent_ids == set()


async def test_agent_instantiated_joins_member_set():
    sm = _sm()
    sm.register_session("s1")
    await sm.handle_event(_agent_ev(EventType.AGENT_INSTANTIATED, "root", {"template_id": "t"}))
    assert sm.agent_ids_of("s1") == {"root"}


async def test_agent_spawned_joins_member_set():
    sm = _sm()
    sm.register_session("s1")
    await sm.handle_event(_agent_ev(EventType.AGENT_INSTANTIATED, "root", {}))
    await sm.handle_event(
        _agent_ev(EventType.AGENT_SPAWNED, "kid", {"parent_agent_id": "root"})
    )
    assert sm.agent_ids_of("s1") == {"root", "kid"}


async def test_session_manager_no_longer_consumes_queue_signals():
    """三条 TaskQueue* 原是 SM 唯一输入，现在不再消费（保留发射作可观测信号）。"""
    sm = _sm()
    sm.register_session("s1")
    for t in (
        EventType.TASK_QUEUE_BLOCKED,
        EventType.TASK_QUEUE_INTERRUPTED,
        EventType.TASK_QUEUE_DRAINED,
        EventType.TASK_STARTED,
    ):
        await sm.handle_event(_agent_ev(t, "root", {"count": 1}))
    assert sm.event_bus.events == [], "SM 不应再因队列信号发任何事件"


def test_input_by_event_table_is_gone():
    assert not hasattr(SessionManager, "_INPUT_BY_EVENT")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_session_manager_state.py -q`
Expected: FAIL — `_SessionState` 仍有 `status`，`_INPUT_BY_EVENT` 仍在

- [ ] **Step 3: 实现**

```python
@dataclass
class _SessionState:
    """会话容器：只记 tenant 归属与成员 agent。

    状态整体挪到 agent 身上（spec 2）——外部若想知道「这个 session 整体闲不闲」，
    自己聚合该 session 下所有 AgentSummary.status，不由系统预先算好广播。
    """

    tenant_id: str = "default"
    agent_ids: set[str] = field(default_factory=set)
```

删除 `_INPUT_BY_EVENT`、`_apply`、`_emit_session_event`、`status_of`、`is_terminal`。

`handle_event` 改为：

```python
    _MEMBER_EVENTS: ClassVar[frozenset[str]] = frozenset({
        EventType.AGENT_INSTANTIATED,
        EventType.AGENT_SPAWNED,
    })

    async def handle_event(self, ev: Event) -> None:
        if ev.type not in self._MEMBER_EVENTS:
            return
        st = self._states.get(ev.session_id)
        if st is None or not ev.agent_id:
            return
        st.agent_ids.add(ev.agent_id)

    def agent_ids_of(self, session_id: str) -> set[str]:
        st = self._states.get(session_id)
        return set(st.agent_ids) if st is not None else set()
```

`register_session` 保持签名不变（仍写 `tenant_id`）；`forget_session` 不变。
`resume_session` 里 `self._states[session_id].status = "RUNNING"`（约 330）那一行**删除**。
`cancel(session_id)` 改为广播——见 Task 20。

> `create_session` 中「`agent_id` 在 `SESSION_CREATED` 之前生成」的因果顺序（约 212）**保持不动**，host 侧 FK 依赖它。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_session_manager_state.py -q`
Expected: 5 passed

- [ ] **Step 5: 回归**

Run: `python -m pytest -q`
Expected: `test_session_state_machine.py` / `test_session_status_domain.py` / `test_agent_llm_replay.py` 会红——Task 16 处理。先记录清单。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/session_manager.py tests/unit/test_session_manager_state.py
git commit -m "refactor(session): SessionManager 降格为成员登记表，去掉会话状态机"
```

---

### Task 16: 4 个 `SESSION_*` 停发进 L 档 + 清理 `session_state.py`

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（`L_TIER_EVENT_TYPES` 追加 4 个）
- Delete: `src/ctx_weft/core/orchestrator/session_state.py` 的状态机部分
- Modify: `src/ctx_weft/core/control/reducers.py`（**只确认分支保留，不删**）
- Delete/改写: `tests/unit/test_session_state_machine.py`、`tests/unit/test_session_status_domain.py`
- Modify: `tests/unit/test_agent_llm_replay.py`
- Test: `tests/unit/test_agent_lifecycle.py`（追加 L 档断言）

**Interfaces:**
- Consumes: Task 7 的 `L_TIER_EVENT_TYPES`、Task 15 的降格
- Produces: `SESSION_RUNNING` / `SESSION_WAITING` / `SESSION_INTERRUPTED` / `SESSION_FINISHED` 停发但枚举值与 reducer 分支保留

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_lifecycle.py
import pathlib

_RETIRED_SESSION_TYPES = {
    "SessionRunning", "SessionWaiting", "SessionInterrupted", "SessionFinished",
}


def test_retired_session_types_in_l_tier():
    assert _RETIRED_SESSION_TYPES <= set(L_TIER_EVENT_TYPES)


def test_retired_session_types_still_in_enum():
    """§5：只删发射，不删枚举。"""
    assert _RETIRED_SESSION_TYPES <= set(EVENT_TYPES)


def test_retired_session_types_not_emitted_in_core():
    """外加条：L 档 ∩ 实际发射集合 = ∅。"""
    members = [
        "SESSION_RUNNING", "SESSION_WAITING", "SESSION_INTERRUPTED", "SESSION_FINISHED",
    ]
    hits = []
    for py in pathlib.Path("src/ctx_weft/core").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in members:
            if f"EventType.{m}" in text and "reducers.py" not in str(py):
                hits.append(f"{py}:{m}")
    assert hits == [], f"停发类型仍在被发射：{hits}"


def test_reducers_still_understands_retired_session_types():
    """reducer 分支必须保留——存量日志靠它重建。"""
    src = pathlib.Path("src/ctx_weft/core/control/reducers.py").read_text(encoding="utf-8")
    for m in ("SESSION_RUNNING", "SESSION_WAITING", "SESSION_INTERRUPTED", "SESSION_FINISHED"):
        assert f"EventType.{m}" in src, f"reducers 丢了 {m} 的重放分支"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q -k retired_session`
Expected: FAIL — 4 个类型不在 `L_TIER_EVENT_TYPES`

- [ ] **Step 3: 实现**

1. `L_TIER_EVENT_TYPES` 追加：

```python
    # 本次新入档：session 运行态 4 个（spec 2 / 10）
    EventType.SESSION_RUNNING,
    EventType.SESSION_WAITING,
    EventType.SESSION_INTERRUPTED,
    EventType.SESSION_FINISHED,
```

2. `session_state.py`：删除 `SessionInput`、`Transition`、`next_transition`。
   **保留** `TERMINAL_SESSION_STATUSES`（`reducers.py` 与 `models.py` 仍引用它做重放期的会话终态判断）。
   若删完后文件只剩这一个常量，把它移到 `core/state/models.py`（`SessionStatus` 旁边）并删除整个 `session_state.py`；否则保留文件。以 `grep -rn "TERMINAL_SESSION_STATUSES\|next_transition\|SessionInput" src/ tests/` 的实际结果决定。

3. `reducers.py`：**只确认**四个分支在，不做任何删除。

4. 测试清理：
   - `tests/unit/test_session_state_machine.py`：整文件删除（被测对象已不存在）。
   - `tests/unit/test_session_status_domain.py`：整文件删除。
   - `tests/unit/test_agent_llm_replay.py`：把对 `SessionInput` / `next_transition` 的 import 与用法删掉，只保留 LLM 重放相关断言。

> 这三个测试文件都**不是** golden 用例，可以放心改。golden 三件套（`test_capsule_golden.py` / `test_dispatch_fold_golden.py` / `test_golden_conformance.py`）仍然不许碰。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_lifecycle.py -q`
Expected: 24 passed

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: 除 golden 三件套外全绿。

- [ ] **Step 6: Commit**

```bash
git add -A src/ctx_weft tests/unit
git commit -m "refactor(session): 4 个 SESSION_* 停发进 L 档，删除会话状态机（枚举与 reducer 分支保留）"
```

---

## Phase F · 外部 API

### Task 17: `AgentSummary` / `AgentDetail` 与 `list_agents` / `get_agent`

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py` 同级新建或就近放置视图类型——建议 `src/ctx_weft/protocols/agent.py`（新建）
- Modify: `src/ctx_weft/core/runtime.py`
- Test: `tests/unit/test_runtime_agent_api.py`（新建）

**Interfaces:**
- Consumes: Task 11 的索引与 `status_of`、Task 15 的 `agent_ids_of`
- Produces:
  - `AgentSummary`（frozen dataclass）：`agent_id` `parent_agent_id` `status` `current_task_id` `spawn_depth` `created_at`
  - `AgentDetail`：继承 `AgentSummary` 字段 + `template_id` `session_id` `current_task_status`
  - `CtxWeftRuntime.list_agents(session_id, *, parent_agent_id=None, include_terminated=False) -> list[AgentSummary]`
  - `CtxWeftRuntime.get_agent(agent_id) -> AgentDetail`

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_runtime_agent_api.py
from __future__ import annotations

import pytest

from ctx_weft.core.errors import UnknownAgentError
from ctx_weft.protocols.agent import AgentDetail, AgentSummary
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


def _rt():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


def _plant(rt, agent_id, parent, session_id="s1", status="idle"):
    from ctx_weft.core.orchestrator.agent_registry import _AgentRecord

    reg = rt._agent_registry
    reg._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=None, loop_config=None, status=status,
    )
    if parent is not None:
        reg._children.setdefault(parent, set()).add(agent_id)


async def test_list_agents_returns_flat_list():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid", "root")

    out = rt.list_agents("s1")
    assert {a.agent_id for a in out} == {"root", "kid"}
    assert all(isinstance(a, AgentSummary) for a in out)
    kid = next(a for a in out if a.agent_id == "kid")
    assert kid.parent_agent_id == "root"


async def test_list_agents_filtered_by_parent():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid1", "root")
    _plant(rt, "grandkid", "kid1")

    out = rt.list_agents("s1", parent_agent_id="root")
    assert {a.agent_id for a in out} == {"kid1"}, "只返回直接子 agent"


async def test_list_agents_excludes_terminated_by_default():
    rt = _rt()
    _plant(rt, "alive", None)
    _plant(rt, "dead", None, status="terminated")

    assert {a.agent_id for a in rt.list_agents("s1")} == {"alive"}
    assert {a.agent_id for a in rt.list_agents("s1", include_terminated=True)} == {"alive", "dead"}


async def test_get_agent_returns_detail():
    rt = _rt()
    _plant(rt, "root", None)
    d = rt.get_agent("root")
    assert isinstance(d, AgentDetail)
    assert d.template_id == "tpl"
    assert d.session_id == "s1"


async def test_get_agent_unknown_raises():
    rt = _rt()
    with pytest.raises(UnknownAgentError):
        rt.get_agent("ghost")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q`
Expected: FAIL — `ModuleNotFoundError: ctx_weft.protocols.agent`

- [ ] **Step 3: 实现**

```python
# src/ctx_weft/protocols/agent.py
"""agent 发现接口的 host-facing 视图类型（spec 5）。

层级关系不在接口层嵌套——各条自带 parent_agent_id，调用方按需还原成树。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AgentSummary:
    agent_id: str
    parent_agent_id: str | None
    status: str
    current_task_id: str | None
    spawn_depth: int
    created_at: datetime | None = None


@dataclass(frozen=True)
class AgentDetail:
    agent_id: str
    parent_agent_id: str | None
    status: str
    current_task_id: str | None
    spawn_depth: int
    session_id: str
    template_id: str
    created_at: datetime | None = None
    current_task_status: str | None = None
```

`runtime.py`：

```python
    def list_agents(
        self,
        session_id: str,
        *,
        parent_agent_id: str | None = None,
        include_terminated: bool = False,
    ) -> "list[AgentSummary]":
        """列出该 session 下的 agent（spec 5）。

        不传 parent_agent_id 返回全部（扁平）；传了只返回其**直接**子 agent。
        include_terminated 默认 False，避免列表随时间无限膨胀。
        """
        reg = self._agent_registry
        ids = reg.agent_ids_of_session(session_id)
        if parent_agent_id is not None:
            ids = [i for i in ids if reg._agents[i].parent_agent_id == parent_agent_id]
        out: list[AgentSummary] = []
        for aid in ids:
            rec = reg._agents[aid]
            if not include_terminated and rec.status == "terminated":
                continue
            out.append(AgentSummary(
                agent_id=aid,
                parent_agent_id=rec.parent_agent_id,
                status=rec.status,
                current_task_id=rec.current_task_id,
                spawn_depth=rec.spawn_depth,
            ))
        return out

    def get_agent(self, agent_id: str) -> "AgentDetail":
        reg = self._agent_registry
        rec = reg._agents.get(agent_id)
        if rec is None:
            raise UnknownAgentError(f"unknown agent: {agent_id}")
        task_status: str | None = None
        if rec.current_task_id:
            tm = self._task_managers.get(rec.session_id)
            task = tm.get_task(rec.current_task_id) if tm is not None else None
            task_status = task.status if task is not None else None
        return AgentDetail(
            agent_id=agent_id,
            parent_agent_id=rec.parent_agent_id,
            status=rec.status,
            current_task_id=rec.current_task_id,
            spawn_depth=rec.spawn_depth,
            session_id=rec.session_id,
            template_id=rec.template_id,
            current_task_status=task_status,
        )
```

> `tm.get_task` 若不存在，用 TaskManager 实际的取 task 方法（`grep -n "def get_task\|self\._tasks" src/ctx_weft/core/orchestrator/task_manager.py`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q`
Expected: 5 passed

- [ ] **Step 5: lint**

Run: `python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/agent.py src/ctx_weft/core/runtime.py tests/unit/test_runtime_agent_api.py
git commit -m "feat(api): AgentSummary/AgentDetail + list_agents/get_agent 发现接口"
```

---

### Task 18: `send_message`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`
- Test: `tests/unit/test_runtime_agent_api.py`（追加）

**Interfaces:**
- Consumes: Task 13 的守卫、Task 11 的 `current_task_id`
- Produces: `CtxWeftRuntime.send_message(agent_id, content, *, session_id=None) -> str`（返回本次消息落到的 `task_id`）

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_runtime_agent_api.py
from ctx_weft.core.errors import AgentBusyError, AgentTerminatedError

_TERMINAL_TASK_STATUSES = {"FINISHED", "FAILED", "CANCELED"}


async def test_send_message_rejects_running_agent():
    """spec 4.1：忙碌直接报错，不排队。"""
    rt = _rt()
    _plant(rt, "a1", None, status="running")
    with pytest.raises(AgentBusyError):
        await rt.send_message("a1", "hi")


async def test_send_message_rejects_terminated_agent():
    rt = _rt()
    _plant(rt, "a1", None, status="terminated")
    with pytest.raises(AgentTerminatedError):
        await rt.send_message("a1", "hi")


async def test_send_message_validates_session_id_when_given():
    rt = _rt()
    _plant(rt, "a1", None, session_id="s1")
    with pytest.raises(ValueError):
        await rt.send_message("a1", "hi", session_id="s-other")


async def test_send_message_reuses_live_task(monkeypatch):
    """current_task 未终态 -> 注入现有 task，不新建。"""
    rt = _rt()
    _plant(rt, "a1", None)
    rt._agent_registry._agents["a1"].current_task_id = "t-live"

    injected: list[tuple[str, object]] = []

    async def _fake_inject(task_id, content, **_kw):
        injected.append((task_id, content))

    monkeypatch.setattr(rt, "_inject_user_turn", _fake_inject, raising=False)
    monkeypatch.setattr(rt, "_task_is_terminal", lambda _s, _t: False, raising=False)

    tid = await rt.send_message("a1", "hello")
    assert tid == "t-live"
    assert injected == [("t-live", "hello")]


async def test_send_message_creates_new_task_when_current_is_terminal(monkeypatch):
    rt = _rt()
    _plant(rt, "a1", None)
    rt._agent_registry._agents["a1"].current_task_id = "t-done"

    created: list[str] = []

    async def _fake_new_task(agent_id, content, **_kw):
        created.append(agent_id)
        return "t-new"

    monkeypatch.setattr(rt, "_start_task_for_agent", _fake_new_task, raising=False)
    monkeypatch.setattr(rt, "_task_is_terminal", lambda _s, _t: True, raising=False)

    tid = await rt.send_message("a1", "hello")
    assert tid == "t-new"
    assert created == ["a1"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q -k send_message`
Expected: FAIL — `AttributeError: send_message`

- [ ] **Step 3: 实现**

```python
    async def send_message(
        self,
        agent_id: str,
        content: "str | list[ContentPart]",
        *,
        session_id: str | None = None,
    ) -> str:
        """向指定 agent 发消息，返回本次消息落到的 task_id（spec 4.1）。

        守卫：不存在 / terminated / running 一律抛错，不排队。
        路由：current_task 已终态 -> 新建 task；未终态 -> 注入现有 task。
        session_id 仅用于校验归属，不参与路由（agent_id 全局唯一）。
        """
        reg = self._agent_registry
        reg.assert_can_receive(agent_id)
        rec = reg._agents[agent_id]
        if session_id is not None and session_id != rec.session_id:
            raise ValueError(
                f"agent {agent_id} belongs to session {rec.session_id}, not {session_id}"
            )

        current = rec.current_task_id
        if current and not self._task_is_terminal(rec.session_id, current):
            await self._inject_user_turn(current, content)
            return current
        return await self._start_task_for_agent(agent_id, content)

    def _task_is_terminal(self, session_id: str, task_id: str) -> bool:
        tm = self._task_managers.get(session_id)
        if tm is None:
            return True
        task = tm.get_task(task_id)
        if task is None:
            return True
        return task.status in ("FINISHED", "FAILED", "CANCELED")
```

`_inject_user_turn` / `_start_task_for_agent` 两个私有方法：**复用现有通路，不新造**。
- `_inject_user_turn(task_id, content)`：走 HITL 回复已经在用的 `UserTurnDelivery` 注入路径——实施前 `grep -n "UserTurnDelivery" src/ctx_weft/core/runtime.py` 找到现成的调用点抽出来。
- `_start_task_for_agent(agent_id, content)`：造一个 `Task(assigned_agent_id=agent_id, ...)` 走 `tm.push_task`，然后 `asyncio.create_task(tm.drain())`；建 task 后把 `rec.current_task_id` 更新为新 id。参考 `SessionManager._make_root_task_manager` 里造 root task 的写法。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q`
Expected: 10 passed

- [ ] **Step 5: 端到端**

Run: `python -m pytest tests/integration -q`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_runtime_agent_api.py
git commit -m "feat(api): send_message —— 外部消息按 agent 显式寻址"
```

---

### Task 19: `cancel_agent` 级联

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`
- Test: `tests/unit/test_agent_cascade.py`（新建）

**Interfaces:**
- Consumes: Task 11 的 `descendants_of`、Task 12 的 `apply_input`
- Produces: `CtxWeftRuntime.cancel_agent(agent_id, *, reason=None) -> list[str]`（返回被终结的 agent id 列表）

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_agent_cascade.py
from __future__ import annotations

import pytest

from ctx_weft.protocols.events import EventType
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
from tests.unit.test_runtime_agent_api import _plant

pytestmark = pytest.mark.asyncio


def _rt():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())


async def test_cancel_cascades_to_all_descendants():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid1", "root")
    _plant(rt, "kid2", "root")
    _plant(rt, "grandkid", "kid1")

    killed = await rt.cancel_agent("root", reason="user")
    assert set(killed) == {"root", "kid1", "kid2", "grandkid"}
    for a in killed:
        assert rt._agent_registry.status_of(a) == "terminated"


async def test_cancel_marks_cascade_source_in_payload():
    rt = _rt()
    _plant(rt, "root", None)
    _plant(rt, "kid", "root")
    seen = []
    rt._event_bus.subscribe(None, lambda ev: seen.append(ev) or _noop())

    await rt.cancel_agent("root", reason="user")
    term = {e.agent_id: e.payload for e in seen if e.type == EventType.AGENT_TERMINATED}
    assert term["root"]["cascaded_from"] is None
    assert term["kid"]["cascaded_from"] == "root"


async def _noop():
    return None


async def test_cancel_finalizes_pending_hitl(monkeypatch):
    """waiting_human 的 agent：先终局未决 ask_user，再终态化。"""
    rt = _rt()
    _plant(rt, "a1", None, status="waiting_human")

    canceled: list[str] = []

    def _fake_list(session_id=None):
        class _V:
            id = "h1"
            agent_id = "a1"
            resolved = False
        return [_V()]

    async def _fake_cancel(hitl_id, **_kw):
        canceled.append(hitl_id)

    monkeypatch.setattr(rt, "list_pending_hitl", _fake_list, raising=False)
    monkeypatch.setattr(rt.hitl, "cancel", _fake_cancel, raising=False)

    await rt.cancel_agent("a1")
    assert canceled == ["h1"]


async def test_cancel_unknown_agent_is_noop():
    rt = _rt()
    assert await rt.cancel_agent("ghost") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_cascade.py -q`
Expected: FAIL — `AttributeError: cancel_agent`

- [ ] **Step 3: 实现**

```python
    async def cancel_agent(self, agent_id: str, *, reason: str | None = None) -> list[str]:
        """终止 agent 及其全部子孙（spec 6）。返回被终结的 agent id 列表。

        级联向下，避免孤儿 agent 永远挂着无人管。每个按当前状态分别处理：
        running -> 复用 TaskManager 的单任务取消；waiting_human -> 先终局未决 HITL；
        idle -> 直接终态化。
        """
        reg = self._agent_registry
        if agent_id not in reg._agents:
            return []

        targets = [agent_id] + reg.descendants_of(agent_id)
        killed: list[str] = []
        for aid in targets:
            rec = reg._agents.get(aid)
            if rec is None or rec.status == "terminated":
                continue

            if rec.status == "waiting_human":
                await self._cancel_pending_hitl_of(aid)
            if rec.status == "running" and rec.current_task_id:
                tm = self._task_managers.get(rec.session_id)
                if tm is not None:
                    await tm.cancel_task(rec.current_task_id, reason="agent_canceled")

            ok = await reg.apply_input(
                aid,
                AgentInput.CANCEL,
                task_id=rec.current_task_id,
                reason=reason or "canceled",
                cascaded_from=None if aid == agent_id else agent_id,
            )
            if ok:
                killed.append(aid)
        return killed

    async def _cancel_pending_hitl_of(self, agent_id: str) -> None:
        """与「用户取消会话时一并终局未决 ask_user」同一模式。"""
        for v in self.list_pending_hitl():
            if getattr(v, "agent_id", "") == agent_id and not v.resolved:
                await self.hitl.cancel(v.id)
```

> `tm.cancel_task` 的真实方法名以 `task_manager.py` 为准（`grep -n "async def cancel" src/ctx_weft/core/orchestrator/task_manager.py`）；`self.hitl.cancel` 的签名以 `core/hitl/service.py` 为准。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_cascade.py -q`
Expected: 5 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_agent_cascade.py
git commit -m "feat(api): cancel_agent 级联终止子孙，并终局未决 HITL"
```

---

### Task 20: `pause_agent` / `resume_agent` + session 级 API 改广播

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`（`cancel` 改广播）
- Test: `tests/unit/test_agent_cascade.py`（追加）

**Interfaces:**
- Consumes: Task 19 的级联模式
- Produces:
  - `pause_agent(agent_id, *, reason=None) -> list[str]`
  - `resume_agent(agent_id) -> list[str]`
  - `pause_session` / `cancel_session` 改为逐个调用 agent 级接口

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_agent_cascade.py
from ctx_weft.core.errors import CtxWeftError


async def test_pause_only_affects_running_agents():
    rt = _rt()
    _plant(rt, "root", None, status="running")
    _plant(rt, "busy_kid", "root", status="running")
    _plant(rt, "idle_kid", "root", status="idle")

    paused = await rt.pause_agent("root")
    assert set(paused) == {"root", "busy_kid"}, "idle 的子孙不动"
    assert rt._agent_registry.status_of("idle_kid") == "idle"
    assert rt._agent_registry.status_of("busy_kid") == "interrupted"


async def test_pause_non_running_raises():
    """spec 7：只对 running 生效，其余状态直接报错。"""
    rt = _rt()
    _plant(rt, "a1", None, status="idle")
    with pytest.raises(CtxWeftError):
        await rt.pause_agent("a1")


async def test_resume_recovers_all_interrupted_descendants():
    rt = _rt()
    _plant(rt, "root", None, status="interrupted")
    _plant(rt, "kid", "root", status="interrupted")
    _plant(rt, "other", "root", status="idle")

    resumed = await rt.resume_agent("root")
    assert set(resumed) == {"root", "kid"}
    assert rt._agent_registry.status_of("other") == "idle"


async def test_cancel_session_broadcasts_to_every_agent():
    rt = _rt()
    _plant(rt, "root", None, session_id="s1")
    _plant(rt, "kid", "root", session_id="s1")
    rt._session_manager.register_session("s1")
    rt._session_manager._states["s1"].agent_ids.update({"root", "kid"})

    await rt.cancel_session("s1")
    assert rt._agent_registry.status_of("root") == "terminated"
    assert rt._agent_registry.status_of("kid") == "terminated"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_agent_cascade.py -q -k "pause or resume or broadcast"`
Expected: FAIL — `AttributeError: pause_agent`

- [ ] **Step 3: 实现**

```python
    async def pause_agent(self, agent_id: str, *, reason: str | None = None) -> list[str]:
        """暂停 agent 及其全部**正在 running** 的子孙（spec 7）。

        只对 running 生效——非 running 直接报错。底层复用 TaskManager 的 interrupt，
        TASK_INTERRUPTED 经 ALM 映射成 running -> interrupted，本方法只是外壳。
        """
        reg = self._agent_registry
        rec = reg._agents.get(agent_id)
        if rec is None:
            raise UnknownAgentError(f"unknown agent: {agent_id}")
        if rec.status != "running":
            raise CtxWeftError(
                f"agent {agent_id} is {rec.status}, not running; nothing to pause"
            )

        paused: list[str] = []
        for aid in [agent_id] + reg.descendants_of(agent_id):
            r = reg._agents.get(aid)
            if r is None or r.status != "running":
                continue
            if r.current_task_id:
                tm = self._task_managers.get(r.session_id)
                if tm is not None:
                    await tm.interrupt_task(r.current_task_id, reason=reason or "paused")
            if await reg.apply_input(
                aid, AgentInput.INTERRUPTED,
                task_id=r.current_task_id, reason=reason or "paused",
            ):
                paused.append(aid)
        return paused

    async def resume_agent(self, agent_id: str) -> list[str]:
        """恢复 agent 及其全部**当前处于 interrupted** 的子孙（spec 7）。

        不区分这些子孙是否由同一次 pause 带下去——只要现在是 interrupted 就一并恢复，
        与 pause 的级联对象保持对称。
        """
        reg = self._agent_registry
        if agent_id not in reg._agents:
            raise UnknownAgentError(f"unknown agent: {agent_id}")

        resumed: list[str] = []
        for aid in [agent_id] + reg.descendants_of(agent_id):
            r = reg._agents.get(aid)
            if r is None or r.status != "interrupted":
                continue
            if await reg.apply_input(aid, AgentInput.RESUMED, task_id=r.current_task_id):
                resumed.append(aid)
                await self.recover_session(r.session_id, resumed_task_id=r.current_task_id)
        return resumed
```

session 级 API 改广播（spec 8）：

```python
    async def cancel_session(self, session_id: str) -> bool:
        """广播便捷入口：对该 session 每个 agent 逐个 cancel_agent。"""
        ids = self._session_manager.agent_ids_of(session_id) or set(
            self._agent_registry.agent_ids_of_session(session_id)
        )
        for aid in sorted(ids):
            await self.cancel_agent(aid, reason="session_canceled")
        return bool(ids)

    async def pause_session(self, session_id: str) -> bool:
        """广播便捷入口：只暂停该 session 下当前 running 的 agent。"""
        reg = self._agent_registry
        ids = self._session_manager.agent_ids_of(session_id) or set(
            reg.agent_ids_of_session(session_id)
        )
        hit = False
        for aid in sorted(ids):
            if reg.status_of(aid) == "running":
                await self.pause_agent(aid, reason="session_paused")
                hit = True
        return hit
```

`SessionManager.cancel` 删除（会话级取消改由 runtime 广播；若有其他调用方，改调 `runtime.cancel_session`）。
`tm.interrupt_task` 的真实方法名以 `task_manager.py` 为准。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_agent_cascade.py -q`
Expected: 9 passed

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: `test_runtime_pause_wiring.py` 可能需改断言。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/ tests/unit/test_agent_cascade.py
git commit -m "feat(api): pause_agent/resume_agent 级联，session 级 API 降为广播入口"
```

---

### Task 21: `HitlReply` 加 `agent_id` 防呆校验

**Files:**
- Modify: `src/ctx_weft/protocols/hitl.py`
- Modify: `src/ctx_weft/core/runtime.py`（`reply_to_hitl` 约 1888）
- Test: `tests/unit/test_runtime_hitl_wiring.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces: `HitlReply.agent_id: str`（必填，放在 `outcome` 之后、`message` 之前）；`reply_to_hitl` 不匹配时抛 `ValueError`

> **字段顺序**：`HitlReply` 现有 `hitl_id`（无默认）、`outcome`（无默认）、`message=""`、`modified_arguments=None`。必填的 `agent_id` 必须插在 `outcome` 之后、`message` 之前，否则 dataclass 编译报错。**这是破坏性变更**：所有位置参数调用方需同步改。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_runtime_hitl_wiring.py
async def test_reply_to_hitl_rejects_agent_id_mismatch():
    """spec 4.3：防呆——调用方声明的 agent 与系统记录不符则拒绝，不静默走掉。"""
    rt = _runtime()
    pending = rt.list_pending_hitl()
    if not pending:
        pytest.skip("需要一个未决 HITL；沿用本文件既有的造 HITL 辅助")
    v = pending[0]
    with pytest.raises(ValueError):
        await rt.reply_to_hitl(
            HitlReply(hitl_id=v.id, outcome="answered", agent_id="wrong-agent")
        )


def test_hitl_reply_requires_agent_id():
    import dataclasses

    f = {x.name: x for x in dataclasses.fields(HitlReply)}
    assert "agent_id" in f
    assert f["agent_id"].default is dataclasses.MISSING, "agent_id 必填"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_hitl_wiring.py -q -k agent_id`
Expected: FAIL — `HitlReply` 无 `agent_id`

- [ ] **Step 3: 实现**

```python
@dataclass
class HitlReply:
    hitl_id: str
    outcome: HitlOutcome
    agent_id: str  # 必填：调用方声明「我以为在回复哪个 agent」，与记录不符则拒绝（spec 4.3）
    message: "str | list[ContentPart]" = ""
    modified_arguments: dict[str, Any] | None = None
```

`reply_to_hitl` 开头加校验（路由逻辑不变，仍按 `hitl_id` 定位）：

```python
        pending = self.hitl_registry.get(reply.hitl_id)
        if pending is not None and reply.agent_id != pending.agent_id:
            raise ValueError(
                f"agent_id mismatch: reply says {reply.agent_id!r}, "
                f"hitl {reply.hitl_id} belongs to {pending.agent_id!r}"
            )
```

> `hitl_registry.get` 的真实方法名以 `core/hitl/registry.py` 为准。

全仓修所有 `HitlReply(...)` 构造点：`grep -rn "HitlReply(" src/ tests/`，逐个补 `agent_id=`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_hitl_wiring.py -q`

- [ ] **Step 5: 回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/protocols/hitl.py src/ctx_weft/core/runtime.py tests/
git commit -m "feat(hitl): HitlReply 加必填 agent_id 防呆校验"
```

---

### Task 22: `start_session` 暴露 `root_agent_id`

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`RunHandle` 定义处 + `start_session` 约 1037）
- Test: `tests/unit/test_runtime_agent_api.py`（追加）

**Interfaces:**
- Consumes: Task 17/18
- Produces: `RunHandle.root_agent_id: str`——外部拿到 session 的同时拿到可寻址的第一个 agent

> spec 4.4 原写 `SessionHandle`，但现有返回类型是 `RunHandle`。**给 `RunHandle` 加字段**而不是新造一个类型——少一个概念，且既有调用方不破。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/unit/test_runtime_agent_api.py
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.core.state.models import NormalTaskSettings


async def test_start_session_exposes_root_agent_id():
    rt = _rt()
    handle = await rt.start_session(SessionStartParams.create(
        template_id="inline",
        user_prompt="hi",
        initial_task_settings=NormalTaskSettings(),
        context_limit=8000,
    ))
    assert handle.root_agent_id
    detail = rt.get_agent(handle.root_agent_id)
    assert detail.session_id == handle.session_id
    assert detail.parent_agent_id is None
```

> `SessionStartParams.create` 的必填参数以 `runtime.py` 里的实际签名为准。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q -k root_agent_id`
Expected: FAIL — `RunHandle` 无 `root_agent_id`

- [ ] **Step 3: 实现**

`RunHandle` 加字段：

```python
    root_agent_id: str = ""  # 外部据此对 root agent 调 send_message（spec 4.4）
```

`start_session` 构造 `RunHandle` 时填入——`create_session` 返回的 `Session` 上已有 root agent id（`session_manager.py:212` 在 `SESSION_CREATED` 之前就 mint 好了），直接取用。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_runtime_agent_api.py -q`

- [ ] **Step 5: 全量回归 + lint**

Run: `python -m pytest -q && python -m ruff check src tests`
Expected: 除 golden 三件套外全绿。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_runtime_agent_api.py
git commit -m "feat(api): start_session 返回 root_agent_id"
```

---

## 收尾核对（全部任务完成后一次性执行）

- [ ] **不变式自检**

```bash
python -m pytest tests/unit/test_event_origin.py tests/unit/test_llm_event_convergence.py \
                 tests/unit/test_agent_lifecycle.py tests/unit/test_agent_state_machine.py \
                 tests/unit/test_agent_cascade.py tests/unit/test_runtime_agent_api.py -q
```

- [ ] **确认 L 档规模**：应为 18 个（原 9 + 本次 9）。
- [ ] **确认没有删除任何 `EventType` 枚举值**：`git diff master -- src/ctx_weft/protocols/events.py | grep '^-.*= "'` 应无输出。
- [ ] **golden 三件套状态**：跑一次记录结果，**不修**，把红/绿情况报告给用户。
- [ ] **host 侧待办清单**（写进 `docs/upgrade/2026-09-03-agent-centric-interaction.md`）：
  - `projection_updater.py` 需折叠 5 个新 S 档 `AGENT_*` 事件（不变式 3）
  - 渲染分流从 `BACKGROUND_OBSERVE_*` / `RECOGNIZE_INTENT_LLM_PROMPT` 改为按 `origin` 前缀匹配
  - `HitlReply` 新增必填 `agent_id`
  - 4 个 `SESSION_*` 停发，会话整体状态改由聚合 agent 状态得出
  - `start_session` 返回值多 `root_agent_id`；新增 6 个 agent 级 API

---

## Self-Review 记录

**Spec 覆盖核对**（spec 各节 → 任务）

| spec 节 | 任务 |
|---|---|
| §2 SessionManager 新职责 | Task 15、16 |
| §3.1 状态机 | Task 9 |
| §3.2 消费 TaskManager 事件 | Task 12（含 Task 8 的信封前置） |
| §3.3 新 `AGENT_*` 事件 | Task 10、12 |
| §3.4 内部索引 | Task 11 |
| §3.5 同步守卫 | Task 13 |
| §4.1 `send_message` | Task 18 |
| §4.2 并发边界（子任务结果排队） | **见下方缺口** |
| §4.3 `reply_to_hitl` 防呆 | Task 21 |
| §4.4 `start_session` 形状 | Task 22 |
| §5 发现/查询 | Task 17 |
| §6 `cancel_agent` | Task 19 |
| §7 `pause_agent`/`resume_agent` | Task 20 |
| §8 session 级 API 定位 | Task 20 |
| §9 LLM 事件收敛 | Task 4、5、6、7 |
| §10 事件类型变更汇总 | Task 7、10、16 |
| §11 组件职责对照表 | 全程 |
| §12 破坏性变更范围 | 收尾核对的 host 清单 |
| §13 待实现阶段处理 | 见下方 |

**已知缺口（实施时须回报，不要静默跳过）**

1. **§4.2 的「子任务结果永远排队」没有独立任务。** 现状是 `delegate_task` 完成后由 `on_task_finished` 回调重新入队父 task——这条路径本身已经是「排队」语义，本计划未改动它，因此**大概率无需新代码**。Task 12 的 `TASK_SUSPENDED -> idle` 与 Task 18 的「未终态则注入现有 task」合起来覆盖了这个场景。**实施 Task 18 时须实际验证一次**：父 agent 处于 `idle`（等子任务）时 `send_message` 走注入分支、子任务完成后仍能正常唤醒父 task。若验证失败，停下来新增一个任务。
2. **`TASK_RESUMED` 的发射点未定位**（spec §13 已记）。Task 12 把它映射成 `AgentInput.RESUMED`，但若该事件实际从未发射，`resume_agent` 就只能靠 Task 20 里显式调 `apply_input` 驱动——Task 20 的实现正是这么写的，因此不阻塞。实施 Task 12 时顺手 `grep -rn "TASK_RESUMED" src/` 确认并回报。
3. **`origin` 的 17 值白名单可能不够用**（Task 3 已标注）。`AgentRegistry` 归入 `runtime` 是本计划的判断；若实施时发现语义不合，停下来交用户裁定，**不要擅自加第 18 个值**。
4. **异步 bus 下 ALM 状态最终一致**（spec §13 已记）。本计划全部基于内置 `InProcessEventBus` 的同步 drain 语义；`assert_can_receive` 在异步 bus 下可能读到陈旧状态。不在本计划范围，已记在 spec。

**类型一致性核对**：`AgentInput` / `next_agent_transition` / `apply_input` / `assert_can_receive` / `descendants_of` / `agent_ids_of_session` / `status_of` 在 Task 9→11→12→13→17→18→19→20 中命名一致，已逐个对过。`_AgentRecord.status` 与 `AgentView.status` 取值域相同（五态字符串）。
