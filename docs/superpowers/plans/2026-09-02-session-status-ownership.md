# 会话状态所有权重构 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `SessionManager` 成为会话状态的唯一持有者与唯一改写者，输入只来自 `TaskManager` 的聚合信号。

**Architecture:** 严格分层，每层只跟下一层说话——HITL 决定 task 状态（`TaskAwaitingHuman`），loop 决定 run 是否被打断（`RunInterrupted`），TaskManager 聚合出会话级信号（`TaskQueueBlocked` / `TaskQueueInterrupted` / `TaskQueueDrained`），SessionManager 据信号转移并发会话状态事件。每一层的判据都是**类型**而不是 payload 里的字符串，所以没有任何一处 `reason` 分流。会话状态回答的是「这个会话健康吗」而不是「卡在哪」，故只有 `RUNNING` / `WAITING` / `INTERRUPTED` + 三个终态；`PAUSED` 与 `PAUSED_HITL` 合并，通用 setter `SessionStatusChanged` 退役。

**Tech Stack:** Python 3.11+、pytest（`asyncio_mode = "auto"`）、ruff（line-length 100）。

**Spec:** `docs/events-v2.md` §2.1（所有权 / 严格分层 / 状态机）、§2.3（Task 域）、§2.4（`RunInterrupted`）、§3.3（TM 信号）、§5.2（`SessionStatusChanged` 进 L 档）

## Global Constraints

- **判据只能是类型，不能是 payload 里的字符串。** 任何 `if payload["reason"] == ...` 形式的
  分流都是本次重构要消灭的东西——它是 HITL 旧实现按 `form == "wait"` 判暂停态的同一种病。
  `reason` 字段只作溯源与展示，不作路由。
- **禁止复用任何曾经发射过的事件类型字符串**（`docs/events-v2.md` §7）。本计划的 6 个新类型
  全仓从未出现过，已核。
- **只删发射，不删枚举值。** `SessionStatusChanged` 与带 legacy `reason` 的 `TaskSuspended`
  都要继续被 reducer 认识——存量日志靠它们重建（L 档，`docs/events-v2.md` §5）。
- **存量事件不重写、不迁移。**
- 既有测试的验收口径是「不新增失败」。本仓已有 3 条既有失败，跑全量时以此为基线：
  `test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`、
  `test_golden_conformance.py::test_golden_dir_present`、
  `test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`。
  Task 9 会修好第二条（裁定 R4），此后基线为 2 条。
- **顺序不可调换。** Task 7 之前会话状态仍由旧路径写，Task 7 之后由 SM 写，中间不留真空。
- ruff line-length 100；新文件一律 `from __future__ import annotations`。

---

## 已定的设计决定（实施时不要重新发明）

**1. 热等待窗口期间会话是 `RUNNING`，不是 `WAITING`。** 这是 2026-09-02 定案的**行为变更**。
热等待时 task 真的还在跑（阻塞在一个 `await` 里，与阻塞在 LLM 调用上无异），没有 park、
没有 `TaskAwaitingHuman`、TM 什么都不知道。只有热窗口耗尽降级成冷 park 时会话才变 `WAITING`。
前端的审批面板由 `HitlOpened` 驱动，**不受影响**；变的只是会话徽标会晚一点。

**2. 「重新开跑」用已有的 `TaskStarted`，不新造类型。** 有 task 开始跑，会话就在跑。

**3. SM 的查询依赖为零。** 它不读 `HitlRegistry`、不问 `TaskManager`、不 import `hitl` 任何东西。
需要的结论全部由 TM 的信号带过来（`final_status` / `reason`）。
**没有任何组件调用 SM 改状态**；SM 的方法只剩外部命令（`create` / `resume` / `cancel`）。

**4. 状态值域的过载是事件过载的根。** 只拆事件不拆 `TaskStatus`，消费方仍然分不清。
故 Task 2 同时拆两者。

**5. `PAUSED` / `PAUSED_HITL` 合并成 `WAITING`，`needs_panel` 整条传递链不存在。**
「要不要出审批面板」是 `HitlOpened.delivery` 的性质，前端渲染面板时已经拿到；
让它经 `TaskAwaitingHuman` → TM 聚合 → SM 三层传递，是同一份信息的三个副本。
删掉它同时消掉一个真实缺陷：`task.awaiting_needs_panel` 是单个 bool，同一 task
若 park 两次会后写覆盖前面的——等于把「降级污染」从会话层搬到 task 层。

**6. 一个 task 同时最多一个「挡住它」的 HITL**，由 `act.py` 的串行 tool call 循环保证
（第一个 park 就 `raise HitlPark` unwind 整个 run）。`TaskAwaitingHuman` 描述的是
**挡住这个 task 的那一个请求**。同一 task 在 registry 里仍可能有多条未决（热等待中的、
background observe 并发开的），但它们不挡这个 task。**这条不变式是结构性巧合，
必须有测试钉住**——哪天 tool call 改成 `asyncio.gather` 并发，它就静默破坏。

---

## Task 1: 会话状态机（纯函数）

**Files:**
- Create: `src/ctx_weft/core/orchestrator/session_state.py`
- Test: `tests/unit/test_session_state_machine.py`

**Interfaces:**
- Consumes: 无（纯 stdlib）
- Produces:
  - `SessionInput` —— `StrEnum`，SM 的全部输入
  - `Transition` dataclass：`{status: str, event_type: str, payload: dict}`
  - `next_transition(current: str, inp: SessionInput, *, reason: str = "", final_status: str = "") -> Transition | None`
  - `TERMINAL_SESSION_STATUSES: frozenset[str]`

- [ ] **Step 1: 写失败的测试**

```python
"""会话状态机：状态 × 输入 → 转移。纯函数，无 IO（Task 1）。

判据来自 docs/events-v2.md §2.1.3 的转移表。输入全部来自 TaskManager 的信号
或外部命令——这里不出现任何 HITL 概念，那是 SM 看不见的层。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.session_state import (
    SessionInput,
    TERMINAL_SESSION_STATUSES,
    next_transition,
)


def test_queue_blocked_goes_waiting():
    t = next_transition("RUNNING", SessionInput.QUEUE_BLOCKED)
    assert t is not None
    assert t.status == "WAITING"
    assert t.event_type == "SessionWaiting"


def test_blocked_twice_emits_nothing():
    """TM 每次聚合都可能重发信号；状态没变就不该刷前端。"""
    assert next_transition("WAITING", SessionInput.QUEUE_BLOCKED) is None


def test_interrupted_beats_waiting():
    """等人的时候又断了：异常压过正常，会话该显示「出事了」。"""
    t = next_transition("WAITING", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    assert t.status == "INTERRUPTED"


def test_waiting_after_interrupted_is_allowed():
    """断的那个被解决了，只剩等人的 → 回落到 WAITING。"""
    t = next_transition("INTERRUPTED", SessionInput.QUEUE_BLOCKED)
    assert t.status == "WAITING"


def test_task_started_returns_a_paused_session_to_running():
    t = next_transition("WAITING", SessionInput.TASK_STARTED)
    assert t.status == "RUNNING"
    assert t.event_type == "SessionRunning"
    assert t.payload == {"reason": "human_replied"}


def test_task_started_returns_an_interrupted_session_to_running():
    t = next_transition("INTERRUPTED", SessionInput.TASK_STARTED)
    assert t.status == "RUNNING" and t.payload == {"reason": "resumed"}


def test_task_started_while_already_running_emits_nothing():
    assert next_transition("RUNNING", SessionInput.TASK_STARTED) is None


def test_queue_interrupted_goes_interrupted():
    t = next_transition("RUNNING", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    assert t.status == "INTERRUPTED"
    assert t.event_type == "SessionInterrupted"
    assert t.payload == {"reason": "llm_outage"}


def test_queue_drained_finishes_with_the_given_final_status():
    t = next_transition("RUNNING", SessionInput.QUEUE_DRAINED, final_status="SUCCEEDED")
    assert t.status == "SUCCEEDED"
    assert t.event_type == "SessionFinished"
    assert t.payload == {"final_status": "SUCCEEDED"}


def test_cancel_finishes_from_paused_too():
    t = next_transition("WAITING", SessionInput.CANCEL)
    assert t.status == "CANCELED"
    assert t.event_type == "SessionFinished"
    assert t.payload == {"final_status": "CANCELED"}


def test_drained_while_blocked_on_human_does_not_finish():
    """TM 不该在还有人等回话时报 drained；万一报了，状态机也不许把会话终结掉
    ——绝不把 parked 任务孤立（spec/07 §9.1）。"""
    assert next_transition("WAITING", SessionInput.QUEUE_DRAINED,
                           final_status="SUCCEEDED") is None


def test_drained_while_interrupted_does_not_finish():
    assert next_transition("INTERRUPTED", SessionInput.QUEUE_DRAINED,
                           final_status="SUCCEEDED") is None


@pytest.mark.parametrize("terminal", sorted(TERMINAL_SESSION_STATUSES))
@pytest.mark.parametrize("inp", list(SessionInput))
def test_terminal_states_never_transition(terminal, inp):
    """「已终态不被覆盖」的唯一落点。今天这条守卫在 5 个地方各写一遍。"""
    assert next_transition(terminal, inp, final_status="SUCCEEDED") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_session_state_machine.py -v`
Expected: FAIL —— `ModuleNotFoundError: ctx_weft.core.orchestrator.session_state`

- [ ] **Step 3: 实现状态机**

```python
"""会话状态机：**唯一**一份「什么状态下、什么输入、转到哪」的判据。

会话状态回答的是「这个会话健康吗、还能不能自己往前走」，不是「卡在哪」——
后者是 task 的事。故只有 5 个值：RUNNING（有活在跑）/ WAITING（停着但正常）/
INTERRUPTED（停着且异常）/ 三个终态。

为什么单独成模块、且是纯函数：今天这份判据散在 5 个地方各写一遍——
`task_manager.py:818` / `:799` / `:804`、`reducers.py:562`、host 投影里的
「不得覆盖已落终态」守卫。它们口径不同、位置分散，正是通用 setter
`SessionStatusChanged` 存在的土壤（docs/events-v2.md §2.1.1）。

**这里看不见 HITL。** 会话不知道有没有人在等回话，只知道 TaskManager 报了
「我没有能跑的了，因为有人在等」。分层见 docs/events-v2.md §2.1.1。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "SessionInput",
    "TERMINAL_SESSION_STATUSES",
    "Transition",
    "WAITING",
    "next_transition",
]

#: 会话终态。到达之后任何输入都不再引发转移。
TERMINAL_SESSION_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED", "CANCELED"})

#: 「停着但正常」的那个状态。**只有一个**——「等的是审批面板还是一句话」是
#: `HitlOpened.delivery` 的性质，前端渲染面板时已经拿到，会话状态不复制它。
WAITING: str = "WAITING"


class SessionInput(StrEnum):
    """SM 的全部输入。前四个来自 TaskManager 的事件，`CANCEL` 是外部命令。

    **每一个都是一个独立的输入，不靠 payload 里的字段区分**——收到哪个就转到哪，
    这是本次重构的核心约束（见计划的 Global Constraints）。
    """

    QUEUE_BLOCKED = "queue_blocked"           # TaskQueueBlocked：都停着，正常
    QUEUE_INTERRUPTED = "queue_interrupted"   # TaskQueueInterrupted：都停着，异常
    QUEUE_DRAINED = "queue_drained"           # TaskQueueDrained：全部终态
    TASK_STARTED = "task_started"             # TaskStarted：有活在跑 = 会话在跑
    CANCEL = "cancel"                         # 外部硬取消命令


@dataclass(frozen=True)
class Transition:
    """一次转移的完整结果：新状态 + SM 该发哪条事件 + 该事件的 payload。"""

    status: str
    event_type: str
    payload: dict


def next_transition(
    current: str,
    inp: SessionInput,
    *,
    reason: str = "",
    final_status: str = "",
) -> Transition | None:
    """当前状态 + 一个输入 → 转移；`None` = 不转移、不发事件。

    返回 `None` 而不是「转到自己」是刻意的：SM 据此决定**发不发事件**。TM 每次
    聚合都可能重发同一条信号，状态没变就不该刷前端。
    """
    if current in TERMINAL_SESSION_STATUSES:
        return None                                  # 已终态：任何输入都不转移

    if inp is SessionInput.CANCEL:
        return Transition("CANCELED", "SessionFinished", {"final_status": "CANCELED"})

    if inp is SessionInput.QUEUE_BLOCKED:
        if current == WAITING:
            return None
        return Transition(WAITING, "SessionWaiting", {})

    if inp is SessionInput.QUEUE_INTERRUPTED:
        if current == "INTERRUPTED":
            return None
        return Transition("INTERRUPTED", "SessionInterrupted", {"reason": reason})

    if inp is SessionInput.TASK_STARTED:
        if current == "RUNNING":
            return None
        # 从哪种停顿里回来，决定 reason——这是溯源，不是判据：转移到 RUNNING
        # 这件事本身不依赖它。
        return Transition("RUNNING", "SessionRunning",
                          {"reason": "human_replied" if current == WAITING else "resumed"})

    if inp is SessionInput.QUEUE_DRAINED:
        # 还有人在等回话、或会话处于中断态时不终结——绝不把 parked / 挂起的任务孤立
        # （spec/07 §9.1）。TM 本就不该在这两种情况下报 drained，这里是第二道闸。
        if current in (WAITING, "INTERRUPTED"):
            return None
        return Transition(final_status, "SessionFinished", {"final_status": final_status})

    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_session_state_machine.py -v`
Expected: PASS（含 `3 终态 × 5 输入` 的 15 个参数化用例）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/orchestrator/session_state.py tests/unit/test_session_state_machine.py
git commit -m "feat(session): 会话状态机独立成纯函数模块，输入只有 TM 信号与外部命令"
```

---

## Task 2: 拆 `TaskStatus` 值域 + 两个 task/run 级事件

**Files:**
- Modify: `src/ctx_weft/core/state/models.py:96`（`TaskStatus` 加两个值）
- Modify: `src/ctx_weft/protocols/events.py`（加 `TASK_AWAITING_HUMAN` / `RUN_INTERRUPTED`）
- Modify: `src/ctx_weft/core/control/reducers.py`（`TASK_STATUS_BY_EVENT` 加两项）
- Test: `tests/unit/test_task_status_split.py`

**Interfaces:**
- Consumes: 无
- Produces: `EventType.TASK_AWAITING_HUMAN` / `EventType.RUN_INTERRUPTED`；
  `TaskStatus` 新增 `"AWAITING_HUMAN"` / `"INTERRUPTED"`

本任务**纯新增**：两个新类型还没有发射者，`TaskSuspended` 的三义仍然并存，行为不变。

- [ ] **Step 1: 写失败的测试**

```python
"""task 层把「为什么停」变成类型，而不是 reason 字符串（Task 2）。

状态值域的过载是事件过载的根：一个 SUSPENDED 盖住「等子任务」/「等人」/「被打断」，
消费方只能去匹配 TaskSuspended.reason。拆值域 + 拆类型必须一起做。
"""

from __future__ import annotations

import typing

from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT, reduce_events
from ctx_weft.core.state.models import TaskStatus
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int, *, task_id: str = "task_1") -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), task_id=task_id or None, payload=payload)


def _seed() -> list[Event]:
    return [
        _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0, task_id=""),
        _ev(EventType.TASK_CREATED, {"task": {"id": "task_1", "status": "PENDING"}}, 1),
    ]


def test_task_status_domain_distinguishes_the_three_reasons_to_stop():
    d = set(typing.get_args(TaskStatus))
    assert {"SUSPENDED", "AWAITING_HUMAN", "INTERRUPTED"} <= d


def test_task_awaiting_human_projects_to_awaiting_human():
    view = reduce_events(_seed() + [
        _ev(EventType.TASK_AWAITING_HUMAN, {"hitl_id": "hit_1"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "AWAITING_HUMAN"


def test_run_interrupted_projects_the_task_to_interrupted():
    view = reduce_events(_seed() + [
        _ev(EventType.RUN_INTERRUPTED, {"reason": "llm_outage"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "INTERRUPTED"


def test_both_new_types_are_in_the_status_map():
    """漏进这张表 = 事件发了但投影不动，冷重建看不见。"""
    assert TASK_STATUS_BY_EVENT[EventType.TASK_AWAITING_HUMAN] == "AWAITING_HUMAN"
    assert TASK_STATUS_BY_EVENT[EventType.RUN_INTERRUPTED] == "INTERRUPTED"


def test_legacy_task_suspended_still_projects_to_suspended():
    """L 档：旧事件带着 reason=hitl_park / run_crash，reducer 要继续认。"""
    view = reduce_events(_seed() + [
        _ev(EventType.TASK_SUSPENDED, {"reason": "hitl_park"}, 2),
    ], "run_1")
    assert view.tasks["task_1"].status == "SUSPENDED"


def test_new_types_do_not_reuse_any_legacy_string():
    assert EventType.TASK_AWAITING_HUMAN == "TaskAwaitingHuman"
    assert EventType.RUN_INTERRUPTED == "RunInterrupted"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_task_status_split.py -v`
Expected: FAIL —— `AttributeError: TASK_AWAITING_HUMAN`

- [ ] **Step 3: 拆 `TaskStatus` 值域**

`src/ctx_weft/core/state/models.py:96`：

```python
TaskStatus = Literal[
    "PENDING",
    "ACTIVE",
    "SUSPENDED",         # 等子任务完成——**只剩这一个语义**
    "AWAITING_HUMAN",    # 被 HITL 挂起，需要人来解决
    "INTERRUPTED",       # 被外部打断（LLM outage / run 崩溃），等 /resume
    "TO_BE_OBSERVED",
    "FINISHED",
    "FAILED",
    "CANCELED",
]
```

- [ ] **Step 4: 加两个事件类型**

`src/ctx_weft/protocols/events.py`，在 `TASK_SUSPENDED` 之后：

```python
    # ── task/run 层「为什么停」（2026-09-02 所有权重构）──
    # 从前三件事都压在 TASK_SUSPENDED 的 reason 字面量里，消费方只能匹配字符串。
    # 现在各有类型：TASK_SUSPENDED（等子任务）/ TASK_AWAITING_HUMAN（等人）/
    # RUN_INTERRUPTED（被外部打断）。判据是类型，不是 payload。
    TASK_AWAITING_HUMAN = "TaskAwaitingHuman"   # payload: {hitl_id}
    RUN_INTERRUPTED = "RunInterrupted"          # payload: {reason, error_code?, error_message?}
```

- [ ] **Step 5: 加进 `TASK_STATUS_BY_EVENT`**

`src/ctx_weft/core/control/reducers.py`：

```python
TASK_STATUS_BY_EVENT: dict[EventType, TaskStatus] = {
    EventType.TASK_STARTED: "ACTIVE",
    EventType.TASK_SUSPENDED: "SUSPENDED",
    EventType.TASK_AWAITING_HUMAN: "AWAITING_HUMAN",
    EventType.RUN_INTERRUPTED: "INTERRUPTED",
    EventType.TASK_FINISHED: "FINISHED",
    EventType.TASK_FAILED: "FAILED",
    EventType.TASK_CANCELED: "CANCELED",
    EventType.TASK_RESUMED: "ACTIVE",
    EventType.TASK_REQUEUED: "PENDING",
}
```

> `RUN_INTERRUPTED` 是 run 级事件却进 task 状态表——这是对的：run 塌了，它承载的那个
> task 就停在 `INTERRUPTED`。envelope 的 `task_id` 指明是哪个。

- [ ] **Step 6: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_task_status_split.py -v && uv run pytest tests -q`
Expected: 新测试全过；全量无新增失败（纯新增，无发射者）

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/state/models.py src/ctx_weft/protocols/events.py \
        src/ctx_weft/core/control/reducers.py tests/unit/test_task_status_split.py
git commit -m "feat(task): 拆 TaskStatus 值域与 task 级停顿事件，为什么停由类型承载"
```

---

## Task 3: 三个 TM 信号 + 三个会话状态事件（纯新增）

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`
- Modify: `src/ctx_weft/core/control/reducers.py`（`_apply` 加三个会话分支 + `_set_session_status`）
- Test: `tests/unit/test_session_status_events.py`

**Interfaces:**
- Consumes: Task 1 的 `TERMINAL_SESSION_STATUSES`
- Produces: `EventType.TASK_QUEUE_BLOCKED` / `TASK_QUEUE_INTERRUPTED` /
  `TASK_QUEUE_DRAINED` / `SESSION_INTERRUPTED` / `SESSION_WAITING` / `SESSION_RUNNING`；
  `reducers._set_session_status(view, session_id, status)`

- [ ] **Step 1: 写失败的测试**

```python
"""会话状态事件在 reducer 里的折叠；TM 信号不折叠（Task 3）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int) -> Event:
    return Event(id=generate_id("evt"), run_id=None, sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), payload=payload)


def _created() -> Event:
    return _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0)


def test_session_interrupted_sets_interrupted():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_INTERRUPTED, {"reason": "llm_outage"}, 1)],
                         "run_1")
    assert view.session_status == "INTERRUPTED"
    assert view.sessions["sess_1"].status == "INTERRUPTED"


def test_awaiting_human_with_panel_is_paused_hitl():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_WAITING, {}, 1)],
                         "run_1")
    assert view.session_status == "WAITING"


def test_session_running_returns_to_running():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.SESSION_RUNNING, {"reason": "human_replied"}, 2),
    ], "run_1")
    assert view.session_status == "RUNNING"
    assert view.sessions["sess_1"].status == "RUNNING"


def test_session_running_does_not_resurrect_a_finished_session():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_FINISHED, {"final_status": "SUCCEEDED"}, 1),
        _ev(EventType.SESSION_RUNNING, {"reason": "resumed"}, 2),
    ], "run_1")
    assert view.session_status == "SUCCEEDED"


def test_task_manager_signals_do_not_touch_the_projection():
    """TM 信号是 SM 的输入，不是投影的输入（O 档）。reducer 折叠它们就等于
    会话状态有了第二个写入者，正是本次要消灭的东西。"""
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_QUEUE_BLOCKED, {"count": 1}, 1),
        _ev(EventType.TASK_QUEUE_INTERRUPTED, {"reason": "llm_outage"}, 2),
        _ev(EventType.TASK_QUEUE_DRAINED, {"final_status": "SUCCEEDED"}, 3),
    ], "run_1")
    assert view.session_status == "RUNNING"


def test_none_of_the_six_new_types_reuse_a_legacy_string():
    for member, value in [
        (EventType.TASK_QUEUE_BLOCKED, "TaskQueueBlocked"),
        (EventType.TASK_QUEUE_INTERRUPTED, "TaskQueueInterrupted"),
        (EventType.TASK_QUEUE_DRAINED, "TaskQueueDrained"),
        (EventType.SESSION_INTERRUPTED, "SessionInterrupted"),
        (EventType.SESSION_WAITING, "SessionWaiting"),
        (EventType.SESSION_RUNNING, "SessionRunning"),
    ]:
        assert member == value
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_session_status_events.py -v`
Expected: FAIL —— `AttributeError: TASK_QUEUE_BLOCKED`

- [ ] **Step 3: 加六个枚举成员**

`src/ctx_weft/protocols/events.py`，`SESSION_PAUSED_HITL` 之后：

```python
    # ── 会话状态 v2（2026-09-02 所有权重构）──
    # 只有 SessionManager 发这三条 + SESSION_FINISHED。通用 setter
    # SESSION_STATUS_CHANGED 就此退役（L 档，只读存量）。
    SESSION_INTERRUPTED = "SessionInterrupted"        # 断了，等 /resume，非终态
    SESSION_WAITING = "SessionWaiting"                # 停着但正常：都在等人 / 等外部输入（payload 空）
    SESSION_RUNNING = "SessionRunning"                # 重新开跑：human_replied / resumed
```

`TASK_REQUEUED` 之后：

```python
    # ── TaskManager 的聚合信号（SM 的唯一输入）──
    # 三个独立类型而不是一个带 discriminator 的类型：SM 收到哪条就转到哪个状态，
    # 不读任何字面量。O 档——reducer 不折叠，会话状态由 SM 发的事件承载。
    TASK_QUEUE_BLOCKED = "TaskQueueBlocked"                  # payload: {count}
    TASK_QUEUE_INTERRUPTED = "TaskQueueInterrupted"          # payload: {reason}
    TASK_QUEUE_DRAINED = "TaskQueueDrained"                  # payload: {final_status}
```

- [ ] **Step 4: 加 reducer 的三个会话分支**

`reducers.py` 的 `_apply`，在 `elif t == EventType.SESSION_PAUSED_HITL:` **之前**插入：

```python
    elif t == EventType.SESSION_INTERRUPTED:
        _set_session_status(view, ev.session_id, "INTERRUPTED")

    elif t == EventType.SESSION_WAITING:
        _set_session_status(view, ev.session_id, "WAITING")

    elif t == EventType.SESSION_RUNNING:
        # 迟到的续跑事件不得复活已终结的会话。判据与 session_state 同源。
        if view.session_status not in TERMINAL_SESSION_STATUSES:
            _set_session_status(view, ev.session_id, "RUNNING")
```

文件末尾 helper 区加：

```python
def _set_session_status(view: RunStateView, session_id: str, status: str) -> None:
    """把状态同时写进 run 级标量与 SessionView。两处必须同写——只写一处是
    「投影和视图对不上」那类 bug 的来源。"""
    view.session_status = status
    sess = view.sessions.get(session_id)
    if sess is not None:
        sess.status = status
```

顶部加导入：

```python
from ctx_weft.core.orchestrator.session_state import TERMINAL_SESSION_STATUSES
```

Task 7 改写 legacy HITL 分支时会用到「暂停态」这个概念——那里读的是**存量日志**里的
`PAUSED` / `WAITING` 两个旧值，与新值域无关，保留 `reducers.py` 现有的
`PAUSED_STATUSES` 常量即可，**不要**把它换成新模型的 `WAITING`。

> ⚠️ **检查层序**：`core.control` 导入 `core.orchestrator` 是否引入循环？
> 若 ruff 或 `tests/unit/test_protocols_events_relocation.py` 报层序违规，把这两个常量
> 挪到 `core/state/models.py`（`SessionStatus` 的同居处），两边各从那里导入。
> **不要**在 reducers 里复制字面量。

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_session_status_events.py -v && uv run pytest tests -q`
Expected: 新测试全过；全量无新增失败（纯新增）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/core/control/reducers.py \
        tests/unit/test_session_status_events.py
git commit -m "feat(session): 加 TM 信号与会话状态事件类型及 reducer 读分支（尚无发射者）"
```

---

## Task 4: SessionManager 变长生命周期，持有会话状态

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`
- Modify: `src/ctx_weft/core/runtime.py:602` 附近（构造期建）、`:1020`（不再每次 `new`）
- Create: `tests/unit/_session_helpers.py`
- Test: `tests/unit/test_session_manager_state.py`

**Interfaces:**
- Consumes: Task 1 的 `next_transition` / `SessionInput` / `Transition`
- Produces:
  - `SessionManager.status_of(session_id) -> str`（未知 → `""`）
  - `SessionManager.is_terminal(session_id) -> bool`
  - `SessionManager.register_session(session_id, *, tenant_id="default")`
  - `SessionManager.forget_session(session_id)`
  - `SessionManager._apply(session_id, inp, **kw)`（内部，Task 5 用）
  - `CtxWeftRuntime._session_manager: SessionManager`

- [ ] **Step 1: 写共享替身**

`tests/unit/_session_helpers.py`：

```python
"""Task 4/5 共用的事件总线替身。

单独成模块而不是放在某个 test_*.py 里：跨测试文件 import 会让删掉一个文件
连带打断另外两个，而 pytest 的收集顺序不保证被 import 的那个先被收集。
"""

from __future__ import annotations

from ctx_weft.protocols.events import Event


class RecordingBus:
    """记录所有 emit 的事件。`subscribe` 记下 handler 供测试手动投递。"""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.handlers: list = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def subscribe(self, event_type, handler):        # noqa: ANN001 - 测试替身
        self.handlers.append(handler)
        return None

    def stream(self, filter):                        # noqa: A002 - 对齐协议签名
        raise NotImplementedError

    async def _unsubscribe(self, subscriber_id: str) -> None:
        return None

    def types(self) -> list[str]:
        return [e.type for e in self.events]
```

- [ ] **Step 2: 写失败的测试**

```python
"""SessionManager 持有会话状态并按状态机转移（Task 4）。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.session_state import SessionInput
from ctx_weft.protocols.events import EventType

from tests.unit._session_helpers import RecordingBus


def _sm(bus: RecordingBus) -> SessionManager:
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    sm.register_session("sess_1", tenant_id="t1")
    return sm


async def test_register_session_starts_running():
    assert _sm(RecordingBus()).status_of("sess_1") == "RUNNING"


async def test_unknown_session_has_empty_status_not_an_exception():
    """host 会拿任意 id 来问；抛异常会把一次查询变成一次 500。"""
    sm = SessionManager(lifecycle_manager=None, event_bus=RecordingBus())
    assert sm.status_of("nope") == ""


async def test_blocked_on_human_emits_awaiting_and_moves_state():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    assert sm.status_of("sess_1") == "WAITING"
    assert bus.types() == [EventType.SESSION_WAITING]
    assert bus.events[0].payload == {}          # 会话事件不带展示数据（裁定 R3）
    assert bus.events[0].session_id == "sess_1"
    assert bus.events[0].run_id is None          # 会话级事件不属于任何 run
    assert bus.events[0].tenant_id == "t1"


async def test_repeating_the_same_signal_emits_nothing():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    assert bus.types() == [EventType.SESSION_WAITING]


async def test_terminal_state_absorbs_every_later_input():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.CANCEL)
    before = len(bus.events)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    await sm._apply("sess_1", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    await sm._apply("sess_1", SessionInput.TASK_STARTED)
    assert sm.status_of("sess_1") == "CANCELED"
    assert len(bus.events) == before
    assert sm.is_terminal("sess_1") is True


async def test_applying_to_an_unregistered_session_is_a_noop():
    bus = RecordingBus()
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    await sm._apply("nope", SessionInput.QUEUE_DRAINED, final_status="SUCCEEDED")
    assert bus.types() == []


async def test_forget_session_releases_the_state():
    sm = _sm(RecordingBus())
    sm.forget_session("sess_1")
    assert sm.status_of("sess_1") == ""
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_session_manager_state.py -v`
Expected: FAIL —— `AttributeError: register_session`

- [ ] **Step 4: 给 SessionManager 加状态**

`session_manager.py` 顶部：

```python
from dataclasses import dataclass, field
from typing import Any

from ctx_weft.core.orchestrator.session_state import (
    SessionInput, TERMINAL_SESSION_STATUSES, Transition, next_transition,
)


@dataclass
class _SessionState:
    """SM 为每个 session 持有的全部东西——**只有状态本身**。

    没有未决 HITL 集合、没有任务表：那些是 HitlRegistry 和 TaskManager 的，
    SM 需要的结论由 TM 的信号带过来（docs/events-v2.md §2.1.1）。
    """

    status: str = "RUNNING"
    tenant_id: str = "default"
```

类体里（`create_session` 之前）：

```python
    #: session_id → 状态。**会话状态的唯一住所。**
    _states: dict[str, _SessionState] = field(default_factory=dict, init=False, repr=False)

    # ── 查询：TaskManager / host 都从这里读，不再各自维护判断 ────────────

    def status_of(self, session_id: str) -> str:
        """当前会话状态；未知 session 返回 `""` 而不是抛——host 会拿任意 id 来问。"""
        st = self._states.get(session_id)
        return st.status if st is not None else ""

    def is_terminal(self, session_id: str) -> bool:
        return self.status_of(session_id) in TERMINAL_SESSION_STATUSES

    # ── 登记 ─────────────────────────────────────────────────────────────

    def register_session(self, session_id: str, *, tenant_id: str = "default") -> None:
        """纳入管理。已存在则保留原状态（重入安全）。"""
        self._states.setdefault(session_id, _SessionState(tenant_id=tenant_id))

    def forget_session(self, session_id: str) -> None:
        """会话彻底收口后释放内存。此后查询返回 `""`，调用方须先读后忘。"""
        self._states.pop(session_id, None)

    # ── 转移：改状态与发事件**只在这里** ──────────────────────────────────

    async def _apply(self, session_id: str, inp: SessionInput, **kw: Any) -> None:
        st = self._states.get(session_id)
        if st is None:
            return
        transition = next_transition(st.status, inp, **kw)
        if transition is None:
            return                      # 不转移就不发事件（否则每条信号都刷前端）
        st.status = transition.status
        await self._emit_session_event(session_id, st, transition)

    async def _emit_session_event(
        self, session_id: str, st: _SessionState, transition: Transition,
    ) -> None:
        await self.event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,                 # 会话级事件不属于任何一次 run
            sequence=0,
            session_id=session_id,
            type=transition.event_type,
            timestamp=now_utc(),
            tenant_id=st.tenant_id,
            payload=dict(transition.payload),
        ))
```

`create_session` 里发 `SESSION_CREATED` 之后加 `self.register_session(sid, tenant_id=tenant_id)`。
`resume_session` 里发 `SESSION_RESUMED` 之后加：

```python
        self.register_session(session_id, tenant_id=tenant_id)
        self._states[session_id].status = "RUNNING"   # 新一轮：显式回到 RUNNING
```

- [ ] **Step 5: runtime 改成持一个 SM**

`runtime.py` 构造期（`self._task_managers: dict[str, TaskManager] = {}` 附近）加：

```python
        # 会话状态的唯一住所。从前 SessionManager 是每次调用 new 一个的临时对象
        # （无状态、用完即弃），状态因此无处可放，被 TaskManager / runtime / reducer
        # 各写一份。见 docs/events-v2.md §2.1.1。
        self._session_manager = SessionManager(
            lifecycle_manager=LifecycleManager(template_lookup=self._template_lookup),
            event_bus=self._event_bus,
            task_max_concurrent=self._config.task_max_concurrent,
            task_max_retries=self._config.task_max_retries,
            default_task_timeout_ms=self._config.default_task_timeout_ms,
        )
```

`runtime.py:1020` 的局部构造换成 `sm = self._session_manager`，删掉紧邻的局部
`lm = LifecycleManager(...)`。

> **检查**：`self._template_lookup` 在构造期是否已就绪？若否，改为第一次使用时懒加载，
> **但仍只建一次**。

- [ ] **Step 6: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_session_manager_state.py -v && uv run pytest tests -q`
Expected: 新测试全过；全量无新增失败（SM 持了状态但还没人喂它）

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/orchestrator/session_manager.py src/ctx_weft/core/runtime.py \
        tests/unit/_session_helpers.py tests/unit/test_session_manager_state.py
git commit -m "feat(session): SessionManager 变长生命周期并持有会话状态"
```

---

## Task 5: SM 订阅 TM 信号 + 外部命令入口

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`
- Modify: `src/ctx_weft/core/runtime.py`（构造期 `attach_to_bus`）
- Test: `tests/unit/test_session_manager_inputs.py`

**Interfaces:**
- Consumes: Task 4 的 `_apply`
- Produces: `attach_to_bus()`、`handle_event(ev)`、`cancel(session_id)`

- [ ] **Step 1: 写失败的测试**

```python
"""SM 的输入只有 TM 的四类事件 + 外部命令（Task 5）。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType

from tests.unit._session_helpers import RecordingBus


def _sm(bus: RecordingBus) -> SessionManager:
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    sm.register_session("sess_1", tenant_id="t1")
    return sm


def _ev(t: EventType, payload: dict) -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=1, session_id="sess_1",
                 type=t, timestamp=now_utc(), payload=payload)


async def test_blocked_on_human_signal_pauses_the_session():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 2}))
    assert sm.status_of("sess_1") == "WAITING"
    assert bus.types() == [EventType.SESSION_WAITING]


async def test_blocked_without_panel_pauses_without_panel():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 1}))
    assert sm.status_of("sess_1") == "WAITING"


async def test_interrupted_signal_interrupts():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_INTERRUPTED, {"reason": "llm_outage"}))
    assert sm.status_of("sess_1") == "INTERRUPTED"
    assert bus.events[0].payload == {"reason": "llm_outage"}


async def test_drained_signal_finishes():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_DRAINED, {"final_status": "SUCCEEDED"}))
    assert bus.types() == [EventType.SESSION_FINISHED]
    assert bus.events[0].payload == {"final_status": "SUCCEEDED"}


async def test_task_started_brings_a_paused_session_back():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 1}))
    await sm.handle_event(_ev(EventType.TASK_STARTED, {"assigned_agent_id": "ag_1"}))
    assert sm.status_of("sess_1") == "RUNNING"
    assert bus.types()[-1] == EventType.SESSION_RUNNING
    assert bus.events[-1].payload == {"reason": "human_replied"}


async def test_lower_layer_events_are_not_subscribed():
    """HITL 决定 task 状态、loop 决定 run 是否被打断——SM 看不见这两层
    （docs/events-v2.md §2.1.1 严格分层）。"""
    bus = RecordingBus()
    sm = _sm(bus)
    for t, p in ((EventType.HITL_OPENED, {"hitl_id": "hit_1",
                                          "delivery": {"kind": "tool_result"}}),
                 (EventType.HITL_RESOLVED, {"hitl_id": "hit_1", "outcome": "accepted"}),
                 (EventType.TASK_AWAITING_HUMAN, {"hitl_id": "hit_1"}),
                 (EventType.RUN_INTERRUPTED, {"reason": "llm_outage"}),
                 (EventType.TASK_SUSPENDED, {"summary": "waiting"})):
        await sm.handle_event(_ev(t, p))
    assert sm.status_of("sess_1") == "RUNNING"
    assert bus.types() == []


async def test_session_status_events_are_ignored_by_the_handler():
    """SM 自己发的事件会经总线回流到自己（in-process bus 在 emit 内同步 drain）。
    handler 必须对它们 no-op，否则一次转移会引发无穷递归。"""
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.SESSION_WAITING, {}))
    assert bus.types() == []


async def test_cancel_command_finishes_the_session():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.cancel("sess_1")
    assert bus.types() == [EventType.SESSION_FINISHED]
    assert bus.events[0].payload == {"final_status": "CANCELED"}


async def test_attach_to_bus_registers_one_handler():
    bus = RecordingBus()
    SessionManager(lifecycle_manager=None, event_bus=bus).attach_to_bus()
    assert len(bus.handlers) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_session_manager_inputs.py -v`
Expected: FAIL —— `AttributeError: handle_event`

- [ ] **Step 3: 实现**

```python
    #: SM 的全部事件输入 → 状态机输入。**这张表就是分层的边界**：
    #: HITL 事件、task 级停顿事件、run 级事件一个都不在里面——那些是下层的事，
    #: 由 TaskManager 聚合成这四条（docs/events-v2.md §2.1.1）。
    #:
    #: 会话状态事件（SM 自己发的）也必须不在其中：in-process bus 在 `emit()` 内
    #: 同步 drain，会把它回流给自己，一次转移变成递归。
    _INPUT_BY_EVENT: dict[str, SessionInput] = {
        EventType.TASK_QUEUE_BLOCKED: SessionInput.QUEUE_BLOCKED,
        EventType.TASK_QUEUE_INTERRUPTED: SessionInput.QUEUE_INTERRUPTED,
        EventType.TASK_QUEUE_DRAINED: SessionInput.QUEUE_DRAINED,
        EventType.TASK_STARTED: SessionInput.TASK_STARTED,
    }

    def attach_to_bus(self) -> None:
        """订阅。runtime 构造期调一次。"""
        self.event_bus.subscribe(None, self.handle_event)

    async def handle_event(self, ev: Event) -> None:
        """总线回调。**只读事件、只喂状态机**，不碰其他组件。"""
        inp = self._INPUT_BY_EVENT.get(ev.type)
        if inp is None:
            return
        p = ev.payload or {}
        # 两个 kwargs 一律传下去，由状态机各分支自取——这样加一种输入时不必改
        # 本方法，也不会出现「某分支忘了传参」的静默 bug。
        await self._apply(
            ev.session_id, inp,
            reason=str(p.get("reason", "")),
            final_status=str(p.get("final_status", "")),
        )

    # ── 外部命令：host 经 runtime 进来的请求，不是组件在驱动 SM ────────────

    async def cancel(self, session_id: str) -> None:
        """硬取消。终态，`SessionFinished(CANCELED)`。"""
        await self._apply(session_id, SessionInput.CANCEL)
```

`runtime.py` 构造期、建完 `self._session_manager` 之后加 `self._session_manager.attach_to_bus()`。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_session_manager_inputs.py -v && uv run pytest tests -q`
Expected: 新测试全过；全量无新增失败（TM 还没发信号，SM 收不到任何东西）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/orchestrator/session_manager.py src/ctx_weft/core/runtime.py \
        tests/unit/test_session_manager_inputs.py
git commit -m "feat(session): SM 订阅 TM 的四类信号，下层事件不在其视野内"
```

---

## Task 6: 发射侧改造——三层各发各的

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:2428-2453`、`:2247-2278`、`:2095-2110`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Modify: `tests/unit/test_run_loop_outage.py`、`tests/unit/test_hitl_park.py`（改断言，见 Step 1）
- Test: `tests/unit/test_layered_signals.py`

**Interfaces:**
- Consumes: Task 2/3 的六个类型、Task 5 的 `SessionManager.cancel`
- Produces: `TaskManager.set_session_manager(sm)`、`TaskManager.announce_queue_state()`

- [ ] **Step 1: 写失败的测试**

```python
"""三层各发各的：HITL→task，loop→run，TM→会话级信号（Task 6）。"""

from __future__ import annotations

import pathlib


def test_no_component_emits_session_status_changed_any_more():
    """SessionStatusChanged 进 L 档：只读存量，不得再发射
    （docs/events-v2.md §5.2、§6 不变式 6）。"""
    src = pathlib.Path("src/ctx_weft")
    offenders = [
        str(p) for p in src.rglob("*.py")
        if "SESSION_STATUS_CHANGED" in p.read_text(encoding="utf-8")
        and p.name not in ("reducers.py", "events.py", "_lifecycle.py")
    ]
    assert offenders == []


def test_no_component_dispatches_on_a_task_suspended_reason():
    """判据只能是类型，不能是 payload 里的字符串（Global Constraints）。
    reducers 读存量事件时可以认这两个字符串；别处不许拿它们做路由。"""
    src = pathlib.Path("src/ctx_weft")
    offenders = []
    for p in src.rglob("*.py"):
        if p.name == "reducers.py":
            continue
        text = p.read_text(encoding="utf-8")
        for needle in ('"hitl_park"', '"run_crash"'):
            if needle in text:
                offenders.append(f"{p}:{needle}")
    assert offenders == []
```

配套的行为测试**不新写搭台代码**——改造两条既有测试即可，它们已经把整条链路搭起来了
（都是自包含的、没有 fixture，把 runtime 与替身建在函数体里）：

| 既有测试 | 怎么改 |
|---|---|
| `tests/unit/test_run_loop_outage.py::test_outage_marks_session_interrupted_not_failed` | 断言从 `SessionStatusChanged(INTERRUPTED)` 改成三条：`RunInterrupted` + `TaskQueueInterrupted` + `SessionInterrupted`；并加一条 `SESSION_STATUS_CHANGED not in types` |
| `tests/unit/test_hitl_park.py::test_run_loop_catches_park_returns_suspended` | 断言从 `TaskSuspended` + `task.status == "SUSPENDED"` 改成 `TaskAwaitingHuman{hitl_id}` + `task.status == "AWAITING_HUMAN"`；测试名里的 `returns_suspended` 一并改成 `returns_awaiting_human` |

改完之后它们就是本任务的行为验收，`test_layered_signals.py` 只放上面两条静态守卫。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_layered_signals.py -v`
Expected: FAIL —— 守卫测试列出 `task_manager.py` / `runtime.py`

- [ ] **Step 3: park 改发 `TaskAwaitingHuman`**

`HitlPark` **一个字段都不用加**——它今天已经带 `hitl_id`（`core/loop/park.py`，
`tests/unit/test_hitl_park.py::test_hitl_park_carries_ids` 钉着），而新模型里
`TaskAwaitingHuman` 只需要这一个字段：「要不要出审批面板」是 `delivery` 的性质、
只有前端需要，不上升到任何状态（docs/events-v2.md §2.8）。

`runtime.py:2428` 的 `except HitlPark:` 分支：

```python
        except HitlPark as park:
            # 热→冷降级 / 显式挂起：干净挂起，不算失败。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "AWAITING_HUMAN"
                # 这条描述的是**挡住这个 task 的那一个请求**。act 的 tool call 循环
                # 是串行的，第一个 park 就 unwind 整个 run，所以「挡住它的」唯一确定。
                await self._event_bus.emit(make_event(
                    state, EventType.TASK_AWAITING_HUMAN,
                    payload={"hitl_id": park.hitl_id},
                ))
            logger.info("_run_loop: task %s parked on HITL", task.id)
```

- [ ] **Step 4: outage 与崩溃改发 `RunInterrupted`**

`runtime.py:2445` 的 `except LLMOutageError`：

```python
        except LLMOutageError as exc:
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "INTERRUPTED"
            logger.warning("_run_loop: task %s interrupted by LLM outage: %s", task.id, exc)
            # run 级事实。会话状态由 TM 聚合后交给 SM 判定——这里不宣布会话怎么了。
            await self._event_bus.emit(make_event(state, EventType.RUN_INTERRUPTED, payload={
                "reason": "llm_outage", "error_message": str(exc)}))
```

`task_manager.py:717-726`（`_suspend_task_interrupted`）里，把
`TASK_SUSPENDED(reason="run_crash", ...)` 与 `SESSION_STATUS_CHANGED(INTERRUPTED)` 两条
换成一条，并把 task 置成新状态：

```python
        if task is not None:
            task.status = "INTERRUPTED"
        await self._emit(EventType.RUN_INTERRUPTED, task_id=task_id, payload={
            "reason": "run_crash",
            "error_code": error_code,
            "error_message": error,
        })
```

删掉 `runtime.py:2247-2278` 的 `_emit_session_interrupted` 与 `_emit_session_status`。

- [ ] **Step 5: TM 发三条聚合信号**

`task_manager.py` 加：

```python
    async def announce_queue_state(self) -> None:
        """把「我这边现在什么情况」告诉外界。**SM 的唯一输入。**

        三个独立事件而不是一个带 discriminator 的：消费方收到哪条就知道怎么办，
        不必读 payload 分流（本次重构的核心约束）。

        调用点：每次可能改变「有没有能跑的任务」的地方——`drain()` 走完、
        `on_task_finished` 收尾、恢复期重建完成之后。多调无害：状态没变时
        SM 不会发事件（`next_transition` 返回 `None`）。
        """
        if self._queue or self._running_tasks:
            return                                   # 还有活干，没什么好报的
        interrupted = [t for t in self._tasks.values() if t.status == "INTERRUPTED"]
        blocked = [t for t in self._tasks.values()
                   if t.status in ("AWAITING_HUMAN", "SUSPENDED")]
        if interrupted:
            # 优先级判据是「解开它需要谁」：INTERRUPTED 要运维介入（/resume），
            # AWAITING_HUMAN 只要用户答一句。一个 task 断了、另一个在等人，先报
            # 「断了」——人答完了那个断的还是断的，而且它需要更重的介入。
            await self._emit(EventType.TASK_QUEUE_INTERRUPTED,
                             payload={"reason": interrupted[0].error or "interrupted"})
        elif blocked:
            await self._emit(EventType.TASK_QUEUE_BLOCKED, payload={"count": len(blocked)})
        else:
            await self._emit(EventType.TASK_QUEUE_DRAINED,
                             payload={"final_status": self._final_status()})

    def _final_status(self) -> str:
        """全部终态时的会话结论。failure_counter > 0 表示本轮有任务失败。"""
        if self._session is not None and self._session.failure_counter > 0:
            return "FAILED"
        return "SUCCEEDED"

    def set_session_manager(self, sm: "SessionManager") -> None:
        """注入会话状态的持有者。TM 对它**只查询、只发事实**；唯一的方法调用是
        `cancel`，那是外部命令的透传，不是 TM 在驱动 SM。"""
        self._session_manager = sm
```

`__init__` 里加 `self._session_manager: "SessionManager | None" = None`。

把 `_fire_session_idle` 与 `_fire_session_done` 的主体替换成
`await self.announce_queue_state()`（`gather` 后台协程与 `_is_current()` 归属权判定保留），
删掉 `_session_done_fired` 闩与 `_has_pending_hitl` 回调及其 setter——幂等由状态机的
「已终态吸收一切」承担，「有人在等」由 `AWAITING_HUMAN` 的任务表达。
`runtime.py:1111` 那处 `task_manager.set_has_pending_hitl(...)` 一并删。

- [ ] **Step 6: 删 TM 剩下的三个旧发射点**

1. `:822`（`on_task_finished`）——删早报的 `SESSION_STATUS_CHANGED`
2. `:959`（`_trip_failure_threshold`）——删 `SESSION_STATUS_CHANGED(FAILED)`
3. `:995`（`cancel_all`）——换成
   ```python
        if self._session_manager is not None:
            await self._session_manager.cancel(self._session_id)
   ```

`runtime.py:1099` 与 `:1448` 两处建 TM 的地方各补
`task_manager.set_session_manager(self._session_manager)`。

- [ ] **Step 7: `recover()` 走同一条链**

`runtime.py:2095-2110` 换成：

```python
                await self.rebuild_hitl(session_id)
                self._session_manager.register_session(session_id)
                tm = self._task_managers.get(session_id)
                if tm is not None:
                    await tm.announce_queue_state()   # 恢复路径和正常路径走同一条链
```

> 恢复期不再有专门的分支判断「有没有未决 HITL」——TM 重建任务集合后照常聚合，
> 该报什么报什么。这正是「复活不是一种状态」的落地。

- [ ] **Step 8: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_layered_signals.py -v && uv run pytest tests -q`
Expected: 新测试全过。全量会有一批失败——它们断言的是旧事件序列。**逐条改断言，不改行为**：

| 旧断言 | 改成 |
|---|---|
| `SessionStatusChanged(INTERRUPTED)` | `RunInterrupted` + `TaskQueueInterrupted` + `SessionInterrupted` |
| `TaskSuspended(reason="hitl_park")` | `TaskAwaitingHuman`，task 状态 `AWAITING_HUMAN` |
| `TaskSuspended(reason="run_crash")` | `RunInterrupted`，task 状态 `INTERRUPTED` |
| `SessionStatusChanged(PAUSED_HITL)` | `TaskQueueBlocked` + `SessionWaiting` |
| 早报终态那条 | **直接删** |

受影响文件（已核）：`test_outage_resume.py` `test_failure_threshold_trip.py`
`test_hitl_recovery_v2.py` `test_hitl_paused_status.py` `test_hitl_recovery.py`
`test_run_loop_outage.py` `test_run_crash_suspend.py` `test_overflow_routing.py`
`test_recover_routing.py` `test_outage_interrupt_reason.py` `test_superseded_task_manager.py`
`test_pause_abandon_tm.py` `test_snapshot_recovery.py` `test_finalize_idle_session.py`
`test_hitl_park.py` `test_hitl_e2e_v2.py`。

- [ ] **Step 9: 提交**

```bash
git add -A src tests
git commit -m "refactor(session)!: 三层各发各的事实，会话状态改写权收归 SessionManager"
```

---

## Task 7: reducer 停止让领域事实写会话状态

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（删四处越界写入 + 本地化 legacy 常量）
- Test: `tests/unit/test_domain_facts_do_not_write_session_status.py`

- [ ] **Step 1: 写失败的测试**

```python
"""领域事实不写会话状态（Task 7 · docs/events-v2.md §2.1.1）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int, *, task_id: str | None = None) -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), task_id=task_id, payload=payload)


def _created() -> Event:
    return _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0)


def test_hitl_opened_no_longer_pauses_the_session():
    view = reduce_events([_created(), _ev(EventType.HITL_OPENED, {
        "hitl_id": "hit_1",
        "delivery": {"kind": "tool_result", "tool_call_id": "c1"}}, 1)], "run_1")
    assert view.session_status == "RUNNING"


def test_hitl_resolved_no_longer_returns_the_session_to_running():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.HITL_RESOLVED, {"hitl_id": "hit_1", "outcome": "accepted"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_run_finished_no_longer_writes_the_session_status():
    view = reduce_events([_created(),
                          _ev(EventType.RUN_FINISHED, {"final_status": "FINISHED"}, 1)],
                         "run_1")
    assert view.session_status == "RUNNING"


def test_run_started_no_longer_writes_the_session_status():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.RUN_STARTED, {"run_id": "run_1", "initial_step": "prepare"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_paused_hitl_still_folds_for_old_logs():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_PAUSED_HITL, {"form": "approval"}, 1)],
                         "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_status_changed_still_folds_for_old_logs():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": "INTERRUPTED"}, 1)], "run_1")
    assert view.session_status == "INTERRUPTED"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_domain_facts_do_not_write_session_status.py -v`
Expected: 前 4 条 FAIL，后 2 条 PASS

- [ ] **Step 3: 删掉四处越界写入**

```python
    elif t == EventType.RUN_STARTED:
        view.task_status = "ACTIVE"
        # 会话状态不在此写：run 是任务级的，会话状态归 SessionManager
        # （docs/events-v2.md §2.1.1）。
    elif t == EventType.RUN_FINISHED:
        pass   # run 的记账，不承载状态——它在 §3.2 已是 O 档
```

`HITL_OPENED` 分支整个删掉。`HITL_RESOLVED` 从那个 `elif t in (...)` 元组里移除，
只留 legacy 五类：

```python
    elif t in (
        # L 档：这五个不再发射，保留只为读存量日志。HITL_RESOLVED（新模型）**不在其中**
        # ——新流量里会话状态由 SM 的 SessionRunning 承载。
        EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
        EventType.HITL_REJECTED, EventType.HITL_CANCELLED,
    ):
        if view.session_status in _LEGACY_PAUSED_STATUSES:
            _set_session_status(view, ev.session_id, "RUNNING")
```

**同时把那对旧值搬进本文件**（裁定 R2）。它今天来自
`ctx_weft.core.hitl.status`，而 Task 9 要删掉整个模块；但这一对是**存量日志的历史常量**，
与新值域 `WAITING` 无关，归宿就是这里——它旁边全是 L 档折叠逻辑：

```python
#: 存量日志里的会话暂停态。新模型只有一个 `WAITING`；这一对是 L 档，
#: 只用于读升级点之前的事件（`SessionPausedHitl` 与 5 个旧 HITL 终态）。
_LEGACY_PAUSED_STATUSES: tuple[str, str] = ("PAUSED", "PAUSED_HITL")
```

**并删掉 `reducers.py` 顶部整行**
`from ctx_weft.core.hitl.status import PAUSED_STATUSES, paused_status_for`——
`paused_status_for` 在本任务删掉 `HITL_OPENED` 分支后已无人使用，
`PAUSED_STATUSES` 由上面的本地常量取代。`core.control` 从此不再 import `core.hitl`。

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_domain_facts_do_not_write_session_status.py -v && uv run pytest tests -q`
Expected: 6 条全过；全量无新增失败（Task 6 已让 SM 补上所有写入）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/control/reducers.py \
        tests/unit/test_domain_facts_do_not_write_session_status.py
git commit -m "refactor(reducers): 领域事实不再写会话状态"
```

---

## Task 8: 会话活跃判据迁移

**Files:**
- Modify: `src/ctx_weft/providers/events/_lifecycle.py`
- Test: `tests/unit/test_lifecycle_active_sessions.py`

`list_active_session_ids()` 决定崩溃恢复要捞哪些会话。**两个 EventStore 实现共用这台状态机**，
漏改的表现是「重启后某些会话不弹恢复」或「已结束的会话反复被恢复」。

- [ ] **Step 1: 写失败的测试**

```python
"""会话活跃判据认识新的会话状态事件（Task 8）。"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.providers.events._lifecycle import LIFECYCLE_EVENT_TYPES, apply_lifecycle


def _ev(t: str, payload: dict | None = None, sid: str = "sess_1") -> SimpleNamespace:
    return SimpleNamespace(session_id=sid, type=t, payload=payload or {})


def test_session_interrupted_removes_the_session_from_active():
    """与旧的 SessionStatusChanged(INTERRUPTED) 同语义：已标中断的会话等 /resume。"""
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionInterrupted", {"reason": "process_restart"}))
    assert active == set()


def test_waiting_keeps_the_session_active():
    """停着但正常的会话必须留在活跃集——重启后要重新装填它的未决 HITL。"""
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionWaiting", {}))
    assert active == {"sess_1"}


def test_session_running_re_activates():
    active: set[str] = set()
    apply_lifecycle(active, _ev("SessionRunning", {"reason": "resumed"}))
    assert active == {"sess_1"}


def test_legacy_status_changed_still_recognised():
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionStatusChanged", {"new_status": "CANCELED"}))
    assert active == set()


def test_new_types_are_in_the_narrowing_tuple():
    """SQL 侧据此收窄查询范围；漏一个就等于这条事件对活跃判定不存在。"""
    for t in ("SessionInterrupted", "SessionWaiting", "SessionRunning"):
        assert t in LIFECYCLE_EVENT_TYPES
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_lifecycle_active_sessions.py -v`
Expected: FAIL —— `SessionInterrupted` 是 no-op

- [ ] **Step 3: 改判据**

```python
LIFECYCLE_EVENT_TYPES = (
    "SessionCreated",
    "SessionResumed",
    "SessionFinished",
    "SessionInterrupted",      # 取代 SessionStatusChanged(INTERRUPTED)
    "SessionWaiting",          # 留在活跃集，但须列出（SQL 侧据此收窄查询）
    "SessionRunning",          # 重新激活
    "SessionStatusChanged",    # L 档：只为读存量日志
)


def apply_lifecycle(active: set[str], event: Any) -> None:
    sid = event.session_id
    t = event.type
    if t in ("SessionFinished", "SessionInterrupted"):
        # 终结与中断都不必在下次重启时再捞：前者已结束，后者等显式 /resume。
        active.discard(sid)
    elif t in ("SessionResumed", "SessionRunning", "SessionWaiting"):
        # 在等人 = 还活着，重启后要重新装填它的未决 HITL。
        active.add(sid)
    elif t == "SessionStatusChanged":
        if (event.payload or {}).get("new_status", "") in TERMINAL_STATUSES:
            active.discard(sid)
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_lifecycle_active_sessions.py tests/unit/test_event_store_conformance.py tests/unit/test_active_after_resume.py -v && uv run pytest tests -q`
Expected: 全过，无新增失败

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/events/_lifecycle.py tests/unit/test_lifecycle_active_sessions.py
git commit -m "fix(events): 会话活跃判据认识新的会话状态事件"
```

---

## Task 9: 死值清除、golden 与文档收口

**Files:**
- Modify: `src/ctx_weft/core/state/models.py:84`（删 `QUEUED` / `TIMEOUT`）
- Modify: `docs/spec/golden/06-session-status-transitions.json`、`07-session-resumed.json`、
  `13-task-canceled.json`
- Modify: `docs/spec/01-events.md`、`05-authz-and-hitl.md`、`07-hitl-suspend-resume.md`、
  `docs/events-v2.md`
- Create: `docs/upgrade/2026-09-02-session-status-ownership.md`
- Test: `tests/unit/test_session_status_domain.py`、既有 `test_golden_conformance.py`

- [ ] **Step 1: 写失败的测试**

```python
"""SessionStatus 值域与状态机对齐（Task 9）。"""

from __future__ import annotations

import typing

from ctx_weft.core.orchestrator.session_state import (
    TERMINAL_SESSION_STATUSES, WAITING,
)
from ctx_weft.core.state.models import SessionStatus


def test_every_status_value_is_reachable_from_the_state_machine():
    """状态机表里没有的状态就不该在值域里——同 §6 不变式 1 对事件的要求。"""
    reachable = {"RUNNING", "INTERRUPTED", WAITING} | set(TERMINAL_SESSION_STATUSES)
    assert set(typing.get_args(SessionStatus)) == reachable


def test_the_paused_pair_collapsed_into_one_waiting_value():
    """PAUSED / PAUSED_HITL 的差别是「前端要不要出面板」——那是 delivery 的性质，
    前端渲染面板时已经拿到，会话状态不该复制它（docs/events-v2.md §2.1.3）。"""
    d = set(typing.get_args(SessionStatus))
    assert "PAUSED" not in d and "WAITING" not in d


def test_queued_and_timeout_are_gone():
    d = set(typing.get_args(SessionStatus))
    assert "QUEUED" not in d and "TIMEOUT" not in d
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_session_status_domain.py -v`
Expected: FAIL —— 值域里仍有 `QUEUED` / `TIMEOUT`

- [ ] **Step 3: 删死值**

```python
SessionStatus = Literal[
    "RUNNING",          # 有 task 在跑
    "WAITING",          # 停着，但正常——都在等人 / 等外部输入
    "INTERRUPTED",      # 停着，异常——系统故障，等 /resume（非终态）
    "SUCCEEDED",
    "FAILED",
    "CANCELED",
]
```

三处删除：`QUEUED` / `TIMEOUT` 全仓从未被赋值（`grep` 只命中 capability provider
的错误码 `"TIMEOUT"`，是另一回事）；`PAUSED` / `WAITING` 合并成 `WAITING`。

**连带删除 `src/ctx_weft/core/hitl/status.py` 整个模块**（`paused_status_for` +
`PAUSED_STATUSES`）——它的唯一职责是从 delivery 集合推暂停态，新模型里这个推导不存在。
两个调用方各自处理：`runtime._derive_paused_status` 直接删；
`runtime.session_status_after_recover` 改成 `self._session_manager.status_of(session_id)`。

- [ ] **Step 4a: 先修 `_GOLDEN_DIR` 的路径解析**（裁定 R4）

`tests/unit/test_golden_conformance.py` 今天把 golden 目录解析到**比仓根高一层**的
`…/Loome-02/docs/spec/golden`，而 fixture 实际在 `…/Loome-02/ctx-weft/docs/spec/golden`。
于是 `_CASES` 为空：两个参数化测试被 skip、`test_golden_dir_present` 失败——
它是全量基线 3 条失败之一。

**加载不到 fixture 就没有「失败输出」可依**，Step 4b 无从谈起。先修这一行
（`parents[N]` 少了一级），确认 `test_golden_dir_present` 转绿、两个参数化测试真的跑起来。

修完之后**全量基线从 3 条失败降到 2 条**，后续「不新增失败」按 2 条算。

- [ ] **Step 4b: 重生成三份 golden**

```bash
uv run pytest tests/unit/test_golden_conformance.py -v
```

映射固定：

| golden 里的旧条目 | 换成 |
|---|---|
| `SessionStatusChanged{"new_status":"INTERRUPTED"}` | `RunInterrupted{reason}` + `TaskQueueInterrupted{reason}` + `SessionInterrupted{reason}` |
| `SessionStatusChanged{"new_status":"WAITING"}` 或 `{"PAUSED"}` | `TaskQueueBlocked{"count":N}` + `SessionWaiting{"count":N}`，会话终点 `WAITING` |
| `SessionStatusChanged{"new_status":"RUNNING"}` | `SessionRunning{"reason":…}` |
| `SessionStatusChanged{"new_status":<终态>}` | **删除该条**（早报终态已取消），保留其后的 `SessionFinished` |
| `cancel_all` 的 `SessionStatusChanged{"new_status":"CANCELED"}` | `SessionFinished{"final_status":"CANCELED"}` |
| `TaskSuspended{"reason":"hitl_park"}` | `TaskAwaitingHuman{"hitl_id":…}` |
| `TaskSuspended{"reason":"run_crash",…}` | `RunInterrupted{"reason":"run_crash",…}` |

**逐条比对新旧的最终 `session_status` 与各 task 的最终 `status`。** 两类差异是**预期内**的：
会话的 `PAUSED` / `WAITING` → `WAITING`（值域合并），task 的 `SUSPENDED` →
`AWAITING_HUMAN` / `INTERRUPTED`（值域拆分）。**除此之外不得有任何差异**——
终态（`SUCCEEDED` / `FAILED` / `CANCELED`）必须逐字相同。
若某条 golden 的终态变了，**停下来查清原因**，不要改 golden 迁就。

`06-session-status-transitions.json` 整条链路都是围绕通用 setter 设计的，重写它比修补更清楚。

- [ ] **Step 5: 更新 spec 文档**

- `docs/spec/01-events.md`：冻结清单加六个新类型；`SessionStatusChanged` 标注进 L 档
- `docs/spec/07-hitl-suspend-resume.md`：把「`HitlOpened` → `PAUSED`/`WAITING`」整段改写成
  分层链路（HITL → `TaskAwaitingHuman` → `TaskQueueBlocked` → `SessionWaiting`），
  并写明**热等待期间会话仍是 `RUNNING`** 这个行为变更
- `docs/spec/05-authz-and-hitl.md`：连带引用同步
- `docs/events-v2.md`：把 §2.1 / §2.3 / §2.4 / §3.3 的「新增」标注改成既成事实

- [ ] **Step 6: 写 host 升级须知**

`docs/upgrade/2026-09-02-session-status-ownership.md`，**第一段就写破坏性变更**：

```markdown
# 升级须知 · 会话状态所有权重构（2026-09-02）· **破坏性**

## 先读这一条

**`SessionStatusChanged` 不再被发出。** host 投影 / SSE 若按它更新会话状态，
升级后会话状态**永远停在崩溃前的值**。改订四条：

| 旧 | 新 |
|----|----|
| `SessionStatusChanged("INTERRUPTED")` | `SessionInterrupted{reason}` |
| `SessionStatusChanged("PAUSED"/"WAITING")` | `SessionWaiting{count}` → 状态 `WAITING`（**两个旧值合并成一个**） |
| `SessionStatusChanged("RUNNING")` | `SessionRunning{reason}` |
| `SessionStatusChanged(<终态>)` | `SessionFinished{final_status}` |

**旧分支不要删**——存量日志的回填仍需要它（L 档）。

## `TaskSuspended` 从三义收窄到一义

| 旧 | 新 |
|----|----|
| `TaskSuspended{reason:"hitl_park"}` | `TaskAwaitingHuman{hitl_id}`，task 状态 `AWAITING_HUMAN` |
| `TaskSuspended{reason:"run_crash",…}` | `RunInterrupted{reason,…}`，task 状态 `INTERRUPTED` |
| `TaskSuspended{summary, spawn_titles}` | 不变，task 状态仍是 `SUSPENDED`（等子任务） |

host 若按 `reason` 字面量分流，改为按类型分流。`TaskStatus` 相应多了
`AWAITING_HUMAN` / `INTERRUPTED` 两个值。

**「谁在等人」现在完全由 task 层回答**：会话级只说「停着且正常」，具体是哪个 task 在等、
等的是什么，看 task 状态与未决 HITL 列表。

## 会话终态不再有「早报」

从前 `SessionStatusChanged(final_status)` 会先于 `SessionFinished` 到达。**已删除。**
**前端改为监听 `TaskQueueDrained`**（TM 报「全部任务终态」）或 task 结束事件来提前反映
「这一轮跑完了」。`SessionFinished` 仍在后台协程收尾后到达，仍是关流的信号。

## `PAUSED` 与 `WAITING` 合并成 `WAITING`

两者的差别是**前端要不要出审批面板**，而那个信息的源头是 `HitlOpened.delivery`——
host 渲染面板时本来就拿到了。会话状态再复制一份只是让同一份信息有了第二个副本，
而副本会失步：多个未决请求时，「后到的 user_turn 把 PAUSED_HITL 降成 PAUSED」
和「解掉其中一个就回 RUNNING」这两个 bug 就是副本失步的表现。

**host 侧要做的**：会话徽标不再从会话状态区分「有没有面板」，改为从**未决 HITL 的
delivery** 判断（渲染面板的地方本来就在做这件事）。会话状态只回答三档：
在跑 / 正常地停着 / 异常地停着。

**时机也变了（行为变更）**：热等待窗口期间会话是 `RUNNING` 而不是「在等人」——
那时 task 真的还在跑。只有降级成冷 park 后会话才变 `WAITING`。
**审批面板由 `HitlOpened` 驱动，不受影响**，变的只是会话徽标会晚一点。

## `cancel_all` 现在发终态事件

从前它只发 `SessionStatusChanged(CANCELED)`，不发 `SessionFinished`。现在发
`SessionFinished{final_status:"CANCELED"}`。若 host 的关流逻辑依赖 `SessionFinished`，
取消路径的流从前不会关——这条一并修了。

## `SessionStatus` 少了两个值

`QUEUED` 与 `TIMEOUT` 从值域删除（core 从未赋过）；`PAUSED` / `WAITING` 合并成
`WAITING`。最终 6 个值：`RUNNING` / `WAITING` / `INTERRUPTED` / `SUCCEEDED` /
`FAILED` / `CANCELED`。host 若镜像了这份值域，同步改。

## 新增三条 TM 信号（可选订阅）

`TaskQueueBlocked` / `TaskQueueInterrupted` / `TaskQueueDrained` 是 core 内部
SM 的输入，host **不必**消费——会话状态已由上面四条承载。但它们比会话状态事件更早到达，
host 若想做「这一轮跑完了」的提前提示，订阅 `TaskQueueDrained` 是最准的信号。
```

- [ ] **Step 7: 全量回归 + lint**

Run: `uv run pytest tests -q && uv run ruff check src tests`
Expected: 无新增失败（基线 3 条）；ruff 干净

- [ ] **Step 8: 提交**

```bash
git add -A src tests docs
git commit -m "refactor(session): 清除 SessionStatus 死值、golden 与 spec 收口"
```

---

## 完成判据

- [ ] `src/ctx_weft` 里除 `reducers.py` / `events.py` / `_lifecycle.py` 外，
      `SESSION_STATUS_CHANGED` 零引用
- [ ] 全仓搜不到对 `"hitl_park"` / `"run_crash"` 的分流（`reducers.py` 读存量除外）
- [ ] 会话状态的写入者只有 `SessionManager._apply` 一处
- [ ] SM 的 `_INPUT_BY_EVENT` 只有 4 个键，且都是 `TaskQueue*` / `TaskStarted`
- [ ] `session_manager.py` 不 import `hitl` 任何东西
- [ ] `TaskManager` 不再持有 `_session_done_fired` 闩与 `_has_pending_hitl` 回调
- [ ] `needs_panel` 全仓零出现；`core/hitl/status.py` 已删除
- [ ] 多个等人的 task 时：解掉其一、队列仍空 → 会话仍 `WAITING`；有 task 重新开跑 → `RUNNING`
- [ ] 一个 task 同时最多一个「挡住它」的 HITL：有测试钉住 act 的 tool call 循环是串行的
      （两个都要审批 → 只有第一个执行）
- [ ] `cancel_all` 发 `SessionFinished{final_status:"CANCELED"}`
- [ ] 三份 golden 的最终 `session_status` 与重构前**逐字相同**
- [ ] `SessionStatus` 值域 = 状态机可达状态
- [ ] `reducers.py` 不再 import `ctx_weft.core.hitl` 任何东西（裁定 R2）
- [ ] `test_golden_dir_present` 转绿，两个参数化 golden 测试真的跑起来（裁定 R4）
- [ ] 全量测试无新增失败（基线 3 条既有失败）

---

## 留给下一次的尾巴

- **`TaskManager` 的其余注入回调**（`set_is_current` / `set_cancel_pending_hitl` /
  `set_cancel_inflight` / …）是同一种病的其他症状。本次只收了会话状态相关的两个。
- **`SessionResumed` 名不副实**：它是「在已有会话上开新一轮」，真正的恢复走
  `recover_session`。用户裁定本次不改名，记在案。
- **`TaskOutcomeRecorded` 发射侧与消费侧对不上**（reducer 读 `outputs`/`error`，
  `finalize.py` 只发 `{task_id, outcome}`），见 `docs/events-v2.md` §2.3。
- **一个 run 的 LLM outage 会把整个会话标成 INTERRUPTED**，哪怕另外 3 个 run
  （`task_max_concurrent` 默认 4）还在正常跑。本次的分层让它变得**可以修**了——
  判据现在集中在 `TaskManager.announce_queue_state()` 一个方法里，改成「所有在跑的 run
  都断了才报 `TaskQueueInterrupted`」是一处局部改动。但那是行为变更，不在本次范围。
- **`TaskStatus` 里的 `TO_BE_OBSERVED`** 本次没查它是否还在用。下次动 task 状态时一并核。
- **legacy 事件的最终退役**（8 个 HITL + `SessionStatusChanged` + `TaskSuspended` 的两个
  legacy reason）需要一整个归档周期，见 `docs/events-v2.md` §5。
