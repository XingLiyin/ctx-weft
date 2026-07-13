# Task Recap 崩溃恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让被崩溃打断的 background observe 在恢复时重跑，并把因此卡在非终态的 session 收干净。

**Architecture:** 给 background observe 套一对持久化生命周期事件（`TaskRecapStarted`/`TaskRecapDone`）；`recover_session` 折出未完成集合，仿 `compact_session` 重建 `LoopState` 重跑 observe；对所有 task 已终态但 session 未落终态的会话，显式驱动 `finalize_idle_session` 发 `SESSION_FINISHED`。

**Tech Stack:** Python 3.11+ / asyncio / pytest / 事件溯源（EventStore + reducers 投影）。

## Global Constraints

- 新事件必须落盘：**不得**加入 `TRANSIENT_EVENT_TYPES`（`core/events/types.py:166`）。
- 新事件对状态机无意义：**不得**加入 `TASK_STATUS_BY_EVENT`（`core/events/types.py:177`），reducer `_apply` 不为其加分支（默认 no-op）。
- 所有 background observe 相关改动保持 **best-effort**：单点失败记 `logger.exception` 并继续，绝不阻塞恢复或收尾（沿用 spec §3.6「段保 raw」降级语义）。
- 事件构造统一走 `make_event(state, type, payload=...)`（`core/loop/driver.py:142`）或 `TaskManager._emit`（`core/orchestrator/task_manager.py:659`）——不裸构造 `Event`。
- 提交信息结尾附：`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 分支：`feat/task-recap-recovery`（已存在，spec 已在其上提交）。

---

### Task 1: 新增 TaskRecap 生命周期事件类型

**Files:**
- Modify: `src/ctx_weft/core/events/types.py:146-151`（`BackgroundObserve 域` 之后加两个成员）
- Test: `tests/unit/test_task_recap_events.py`

**Interfaces:**
- Produces: `EventType.TASK_RECAP_STARTED == "TaskRecapStarted"`、`EventType.TASK_RECAP_DONE == "TaskRecapDone"`；二者进 `EVENT_TYPES`，不进 `TRANSIENT_EVENT_TYPES` / `TASK_STATUS_BY_EVENT`；reducer 对其 no-op。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_task_recap_events.py
from ctx_weft.core.events.types import (
    EVENT_TYPES, TRANSIENT_EVENT_TYPES, TASK_STATUS_BY_EVENT, EventType,
)
from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.events import Event
from ctx_weft.core.utils import generate_id, now_utc


def _ev(type_, session_id="ses1", payload=None):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id=session_id,
        type=type_, timestamp=now_utc(), tenant_id="default", payload=payload or {},
    )


def test_task_recap_events_registered_and_persisted():
    assert EventType.TASK_RECAP_STARTED in EVENT_TYPES
    assert EventType.TASK_RECAP_DONE in EVENT_TYPES
    # 必须落盘：不得是 transient
    assert EventType.TASK_RECAP_STARTED not in TRANSIENT_EVENT_TYPES
    assert EventType.TASK_RECAP_DONE not in TRANSIENT_EVENT_TYPES
    # 无状态机含义
    assert EventType.TASK_RECAP_STARTED not in TASK_STATUS_BY_EVENT
    assert EventType.TASK_RECAP_DONE not in TASK_STATUS_BY_EVENT


def test_task_recap_events_are_reducer_noops():
    events = [
        _ev(EventType.SESSION_CREATED, payload={"template_id": "t", "root_agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_STARTED, payload={"task_id": "tsk1", "boundary": "finish", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, payload={"task_id": "tsk1"}),
    ]
    view = reduce_events(events, run_id="ses1")
    # session 仍是 RUNNING（recap 事件不改会话/任务状态）
    assert view.sessions["ses1"].status == "RUNNING"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_task_recap_events.py -v`
Expected: FAIL — `AttributeError: TASK_RECAP_STARTED`（成员未定义）。

- [ ] **Step 3: 加事件成员**

在 `src/ctx_weft/core/events/types.py` 的 `BACKGROUND_OBSERVE_RESPONSE_FINISHED = "BackgroundObserveResponseFinished"`（`:151`）之后、`# ── System / 元事件 ──`（`:152`）之前插入：

```python
    # ── TaskRecap 域（background observe 的持久化生命周期标记；崩溃恢复据此重跑，
    #     与逐轮 BACKGROUND_OBSERVE_* 流式事件不同——这两条是"整段 recap 起/止"的记账）──
    TASK_RECAP_STARTED = "TaskRecapStarted"   # payload: {task_id, boundary, agent_id}
    TASK_RECAP_DONE = "TaskRecapDone"         # payload: {task_id}
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_task_recap_events.py -v`
Expected: PASS（两测试均绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/events/types.py tests/unit/test_task_recap_events.py
git commit -m "feat(events): 新增 TaskRecapStarted/Done 生命周期事件

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `fold_pending_task_recap` 折叠函数

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（在 `unresolved_hitl_ids` 之后，约 `:53`）
- Test: `tests/unit/test_fold_pending_task_recap.py`

**Interfaces:**
- Consumes: `EventType.TASK_RECAP_STARTED` / `TASK_RECAP_DONE`（Task 1）。
- Produces: `fold_pending_task_recap(events: list[Event]) -> dict[str, dict]`，返回 `{task_id: {"boundary": str, "agent_id": str}}`，语义 = started − done，同 task_id last-write-wins。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_fold_pending_task_recap.py
from ctx_weft.core.control.reducers import fold_pending_task_recap
from ctx_weft.core.events import Event
from ctx_weft.core.events.types import EventType
from ctx_weft.core.utils import generate_id, now_utc


def _ev(type_, payload):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="ses1",
        type=type_, timestamp=now_utc(), tenant_id="default",
        task_id=payload.get("task_id"), payload=payload,
    )


def test_started_without_done_is_pending():
    events = [_ev(EventType.TASK_RECAP_STARTED,
                  {"task_id": "t1", "boundary": "finish", "agent_id": "a1"})]
    assert fold_pending_task_recap(events) == {"t1": {"boundary": "finish", "agent_id": "a1"}}


def test_started_then_done_is_empty():
    events = [
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
    ]
    assert fold_pending_task_recap(events) == {}


def test_last_write_wins_per_task():
    # 同 task 二次 started（如 recover 又崩一次）：以最后一次 boundary 为准
    events = [
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "interrupt", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
    ]
    assert fold_pending_task_recap(events) == {"t1": {"boundary": "finish", "agent_id": "a1"}}
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_fold_pending_task_recap.py -v`
Expected: FAIL — `ImportError: cannot import name 'fold_pending_task_recap'`。

- [ ] **Step 3: 实现折叠函数**

在 `src/ctx_weft/core/control/reducers.py` 的 `unresolved_hitl_ids`（`:50-52`）之后插入：

```python
def fold_pending_task_recap(events: list[Event]) -> dict[str, dict]:
    """折叠 TaskRecap 事件 → 仍未完成的 {task_id: {"boundary", "agent_id"}}（started 减去 done）。

    某 task 有 TASK_RECAP_STARTED 而无其后的 TASK_RECAP_DONE，说明该段 background observe 的
    memory 写未持久完成（崩溃在中途）——恢复据此重跑。同 task_id last-write-wins（仿 fold_pending_hitl）。
    """
    pending: dict[str, dict] = {}
    for ev in events:
        p = ev.payload or {}
        tid = p.get("task_id", "")
        if not tid:
            continue
        if ev.type == EventType.TASK_RECAP_STARTED:
            pending[tid] = {"boundary": p.get("boundary", ""), "agent_id": p.get("agent_id", "")}
        elif ev.type == EventType.TASK_RECAP_DONE:
            pending.pop(tid, None)
    return pending
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_fold_pending_task_recap.py -v`
Expected: PASS（3 测试均绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/control/reducers.py tests/unit/test_fold_pending_task_recap.py
git commit -m "feat(reducers): fold_pending_task_recap 折出未完成的段 recap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `_run_background_observe` 发 STARTED/DONE 标记

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:111-185`（`_run_background_observe`）
- Test: `tests/unit/test_task_recap_markers.py`

**Interfaces:**
- Consumes: `EventType.TASK_RECAP_STARTED/DONE`（Task 1）、`make_event`（`core/loop/driver.py:142`）、`ctx.event_bus`（`LoopContext`，`runtime.py:1448`）。
- Produces: 每次 `_run_background_observe` 在函数入口发 `TASK_RECAP_STARTED{task_id, boundary, agent_id}`，在 `finally`（memory 写之后）发 `TASK_RECAP_DONE{task_id}`——成功 / 空报告 / 异常三条出口都发；唯崩溃不发。

> STARTED 放在协程内（而非 sync 的 `launch_background_observe`）以保持 launch 签名不变（多处非 await 调用）。协程 created-但-未跑就崩的极小窗口由 Task 6 的会话收尾兜底（那时无 memory 写、无 recap 需重跑）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_task_recap_markers.py
import pytest
from ctx_weft.core.events.types import EventType
import ctx_weft.core.loop.steps.background_observe as bo


def _recap_types(bus_events):
    return [e.type for e in bus_events if e.type in (
        EventType.TASK_RECAP_STARTED, EventType.TASK_RECAP_DONE)]


@pytest.mark.asyncio
async def test_started_and_done_emitted_on_success(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx  # ctx.event_bus 收集 emit 到 ctx.event_bus.emitted
    async def _fake_react(*a, **k):
        from ctx_weft.core.orchestrator.control_capability import ControlResult
        return ControlResult(content="recap text", metadata={"task_summary": "sum"}), ""
    monkeypatch.setattr(bo, "run_observe_react", _fake_react)

    await bo._run_background_observe(state, ctx, boundary="interrupt")

    kinds = _recap_types(ctx.event_bus.emitted)
    assert kinds == [EventType.TASK_RECAP_STARTED, EventType.TASK_RECAP_DONE]


@pytest.mark.asyncio
async def test_done_emitted_even_on_exception(fake_state_ctx, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("observe blew up")
    monkeypatch.setattr(bo, "run_observe_react", _boom)

    await bo._run_background_observe(state=fake_state_ctx[0], ctx=fake_state_ctx[1], boundary="interrupt")

    kinds = _recap_types(fake_state_ctx[1].event_bus.emitted)
    assert EventType.TASK_RECAP_STARTED in kinds
    assert EventType.TASK_RECAP_DONE in kinds  # 异常出口也发 DONE
```

> 若 `fake_state_ctx` 的 `event_bus` 不暴露 `.emitted`，在 `tests/unit/conftest.py` 给该 fixture 的 fake event_bus 加一个 `self.emitted: list = []`、`async def emit(self, ev): self.emitted.append(ev)`（若已有则复用）。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_task_recap_markers.py -v`
Expected: FAIL — 断言 `kinds == [STARTED, DONE]` 不成立（当前不发 recap 标记）。

- [ ] **Step 3: 加标记发送**

在 `src/ctx_weft/core/loop/steps/background_observe.py` 顶部 import 处加：

```python
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import make_event
```

把 `_run_background_observe`（`:111`）的函数体改为「入口发 STARTED、`try/finally` 发 DONE」：

```python
async def _run_background_observe(state: "LoopState", ctx: "LoopContext", boundary: str) -> None:
    from ctx_weft.core.assembler import ContextRequest
    from ctx_weft.core.loop.steps.observe import (
        BACKGROUND_OBSERVE_REACT_EVENTS, run_observe_react,
    )
    from ctx_weft.core.orchestrator.control_capability import BACKGROUND_PROCESS_REPORT_NAME

    await ctx.event_bus.emit(make_event(
        state, EventType.TASK_RECAP_STARTED,
        payload={"task_id": state.task.id, "boundary": boundary, "agent_id": state.agent.id},
    ))
    try:
        async with _lock_for(state.task.id):
            try:
                # ... 现有函数体原样保留（agent/bound_caps/request/prompt/run_observe_react/分流写 memory）...
            except Exception:
                logger.exception("background observe failed (ignored); segment kept raw")
    finally:
        await ctx.event_bus.emit(make_event(
            state, EventType.TASK_RECAP_DONE, payload={"task_id": state.task.id},
        ))
```

> 只包裹，不改内部逻辑：把原 `async with _lock_for(...)：try：...except：...` 整块移到新的外层 `try:` 内，`finally` 发 DONE。原 `except Exception` 分支不动。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_task_recap_markers.py tests/unit/test_background_observe.py -v`
Expected: PASS（新测试绿；`test_background_observe.py` 存量测试仍绿——它们不断言 recap 标记）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_task_recap_markers.py tests/unit/conftest.py
git commit -m "feat(background-observe): 段 recap 起/止发 TaskRecapStarted/Done

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: compact 边界重跑幂等护栏

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（`_run_background_observe` 锁内、LLM 工作之前）
- Test: `tests/unit/test_task_recap_refold_guard.py`

**Interfaces:**
- Consumes: `ctx.memory.count_recent(scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx)`（存量，见 `finalize.py:129`）。
- Produces: 非 close 边界（`interrupt`/`plain_text`）重跑时，若该段已无 active raw（`LLM_RESPONSE` 计数为 0）→ 跳过 LLM 工作直接返回（`finally` 仍发 DONE），避免对已折叠段产冗余第二段胶囊。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_task_recap_refold_guard.py
import pytest
import ctx_weft.core.loop.steps.background_observe as bo


@pytest.mark.asyncio
async def test_compact_boundary_skips_when_no_active_raw(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    # 该段已折叠：active LLM_RESPONSE 计数为 0
    async def _count(scope, types, pctx):
        return 0
    monkeypatch.setattr(ctx.memory, "count_recent", _count)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        return None, ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="interrupt")

    assert called["react"] is False  # 护栏跳过，未跑 LLM observe


@pytest.mark.asyncio
async def test_close_boundary_not_guarded(fake_state_ctx, monkeypatch):
    state, ctx = fake_state_ctx
    async def _count(scope, types, pctx):
        return 0  # 即便为 0，close 边界也不受护栏影响
    monkeypatch.setattr(ctx.memory, "count_recent", _count)
    called = {"react": False}
    async def _react(*a, **k):
        called["react"] = True
        from ctx_weft.core.orchestrator.control_capability import ControlResult
        return ControlResult(content="r", metadata={}), ""
    monkeypatch.setattr(bo, "run_observe_react", _react)

    await bo._run_background_observe(state, ctx, boundary="finish")

    assert called["react"] is True  # close 边界照常跑
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_task_recap_refold_guard.py -v`
Expected: FAIL — `test_compact_boundary_skips_when_no_active_raw` 失败（当前无护栏，`react` 被调用）。

- [ ] **Step 3: 加护栏**

在 `_run_background_observe` 的 `async with _lock_for(state.task.id):` 之内、原 `try:` 之前插入（`from ctx_weft.protocols import MemoryEventType` 文件已 import）：

```python
        # 重跑幂等护栏（恢复重跑时才生效）：非 close 边界若该段已无 active raw，说明上次
        # 崩溃前已折叠（raw 被 supersede），再折会产冗余胶囊 → 跳过（finally 仍发 DONE）。
        # 正常运行时该段刚产生 raw、计数 > 0，护栏为 no-op。
        if boundary not in _CLOSE_BOUNDARIES:
            n_raw = await ctx.memory.count_recent(
                state.scope, [MemoryEventType.LLM_RESPONSE], ctx.provider_ctx,
            )
            if n_raw == 0:
                logger.info(
                    "task recap re-fold guard: segment already folded (task=%s); skip",
                    state.task.id,
                )
                return
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_task_recap_refold_guard.py tests/unit/test_background_observe.py -v`
Expected: PASS（护栏生效；存量绿——正常路径 raw 计数 > 0）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_task_recap_refold_guard.py
git commit -m "feat(background-observe): compact 边界重跑幂等护栏

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `TaskManager.finalize_idle_session`

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`is_done` 之后，约 `:772`）
- Test: `tests/unit/test_finalize_idle_session.py`

**Interfaces:**
- Consumes: `self._fire_session_done`（`:692`，含 `await asyncio.gather(background)` + 发 `SESSION_FINISHED` + 回调）、`self._emit`、`EventType.SESSION_STATUS_CHANGED`。
- Produces: `async def finalize_idle_session(self, status: str) -> None`——设 `session.status = status`、发 `SESSION_STATUS_CHANGED{new_status}`、调 `_fire_session_done()`（gather 重跑的后台 recap → 发 `SESSION_FINISHED`）。幂等（`_fire_session_done` 有 `_session_done_fired` 守卫）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_finalize_idle_session.py
import pytest
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session
from ctx_weft.core.events.types import EventType


class _Bus:
    def __init__(self):
        self.emitted = []
    async def emit(self, ev):
        self.emitted.append(ev)


@pytest.mark.asyncio
async def test_finalize_idle_session_emits_status_and_finished():
    bus = _Bus()
    tm = TaskManager(session_id="ses1", event_bus=bus)
    tm.set_session(Session(id="ses1", user_prompt="", status="RUNNING", tenant_id="default"))
    tm.set_is_current(lambda: True)

    await tm.finalize_idle_session("SUCCEEDED")

    kinds = [(e.type, (e.payload or {}).get("new_status") or (e.payload or {}).get("final_status"))
             for e in bus.emitted]
    assert (EventType.SESSION_STATUS_CHANGED, "SUCCEEDED") in kinds
    assert (EventType.SESSION_FINISHED, "SUCCEEDED") in kinds
    assert tm.session.status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_finalize_idle_session_gathers_background_recap():
    import asyncio
    bus = _Bus()
    tm = TaskManager(session_id="ses1", event_bus=bus)
    tm.set_session(Session(id="ses1", user_prompt="", status="RUNNING", tenant_id="default"))
    tm.set_is_current(lambda: True)
    done = {"bg": False}
    async def _bg():
        await asyncio.sleep(0.01)
        done["bg"] = True
    tm.track_background(asyncio.create_task(_bg()))

    await tm.finalize_idle_session("SUCCEEDED")

    assert done["bg"] is True  # SESSION_FINISHED 前 gather 了后台 recap
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_finalize_idle_session.py -v`
Expected: FAIL — `AttributeError: 'TaskManager' object has no attribute 'finalize_idle_session'`。

- [ ] **Step 3: 实现方法**

在 `src/ctx_weft/core/orchestrator/task_manager.py` 的 `is_done`（`:769-771`）之后插入：

```python
    async def finalize_idle_session(self, status: str) -> None:
        """恢复专用：会话所有 task 已终态但 session 因崩溃未落终态 —— 设终态并复用
        _fire_session_done（先 gather 重跑的后台 recap，再发 SESSION_FINISHED + 回调）。

        镜像 on_task_finished 的会话收尾：先 SESSION_STATUS_CHANGED，再 _fire_session_done。
        幂等：_fire_session_done 的 _session_done_fired 守卫保证只发一次。
        """
        if self._session is not None:
            self._session.status = status
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": status})
        await self._fire_session_done()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_finalize_idle_session.py -v`
Expected: PASS（2 测试均绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_finalize_idle_session.py
git commit -m "feat(task-manager): finalize_idle_session 收尾崩溃遗留的非终态会话

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: runtime 重跑 recap 辅助方法

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`_recover_session_locked` 之后加两个辅助；复用 `_build_*`/`_resolve_llm` 现有 helper `:1372-1460`）
- Test: `tests/unit/test_relaunch_task_recap.py`

**Interfaces:**
- Consumes: `LifecycleManager.instantiate_agent(existing_agent_id=...)`（`:35`）、`task_from_projection`、`_build_provider_ctx/_build_assembler/_build_gateway/_resolve_llm/_build_loop_ctx`、`launch_background_observe`、`register_close_synth`（`background_observe.py:43`）、`qualify("control:finish_task")`。
- Produces:
  - `async def _find_finish_pair_tool_call_id(self, memory, scope, task_id, pctx) -> str | None`
  - `async def _relaunch_task_recap(self, *, session, template, task_manager, task, agent_id, boundary) -> None`——重建 `LoopState`+`LoopContext(task_manager=task_manager)`，close 边界补 `register_close_synth`，调 `launch_background_observe(state, ctx, boundary=boundary)`（登记到传入 TM）。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_relaunch_task_recap.py
import pytest
import ctx_weft.core.runtime as rt_mod


@pytest.mark.asyncio
async def test_relaunch_registers_close_synth_for_finish(minimal_runtime_with_session, monkeypatch):
    """close 边界重跑：从 memory 读到 finish 对 tool_call_id 后 register_close_synth。"""
    runtime, session, template, task_manager, task, agent_id = minimal_runtime_with_session
    # memory 里预置一条 finish 对 assistant turn（带 finish_task tool_call）
    tcid = await _seed_finish_pair(runtime, session, task, agent_id)  # helper：见下

    captured = {}
    def _fake_register(task_id, tool_call_id, scope, outcome):
        captured.update(task_id=task_id, tool_call_id=tool_call_id, outcome=outcome)
    monkeypatch.setattr(rt_mod, "register_close_synth", _fake_register, raising=False)
    launched = {}
    def _fake_launch(state, ctx, *, boundary):
        launched["boundary"] = boundary
        import asyncio
        return asyncio.create_task(asyncio.sleep(0))
    monkeypatch.setattr(rt_mod, "launch_background_observe", _fake_launch, raising=False)

    await runtime._relaunch_task_recap(
        session=session, template=template, task_manager=task_manager,
        task=task, agent_id=agent_id, boundary="finish",
    )

    assert captured["tool_call_id"] == tcid
    assert captured["outcome"] == "success"
    assert launched["boundary"] == "finish"
```

> `minimal_runtime_with_session` / `_seed_finish_pair`：在 `tests/unit/conftest.py` 加一个 fixture，用 `InMemoryMemoryProvider` + 一个最小 `CtxWeftRuntime`（复用现有 `test_hitl_recovery.py` / `test_recover_routing.py` 的构造样板），`_seed_finish_pair` 用 `memory.ingest` 写一条 `AGENT_CONVERSATION_TURN`（role=assistant, metadata={"origin_task_id": task.id, "tool_calls":[{"id": tcid, "name": qualify("control:finish_task"), "input":{}}]}）并返回 `tcid`。参照 `background_observe._replace_finish_report` 的 turn 结构（`:61-64`）。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/unit/test_relaunch_task_recap.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_relaunch_task_recap'`。

- [ ] **Step 3: 实现两个辅助方法**

在 `src/ctx_weft/core/runtime.py` 顶部 import 处加：

```python
from ctx_weft.core.loop.steps.background_observe import launch_background_observe, register_close_synth
```

在 `_recover_session_locked`（`:917-1027`）之后插入：

```python
    async def _find_finish_pair_tool_call_id(
        self, memory, scope: "MemoryScope", task_id: str, pctx: ProviderContext,
    ) -> str | None:
        """从 memory 找该 task close 时写的占位 finish 对 assistant turn，返回其 finish_task tool_call id。"""
        from ctx_weft.protocols import MemoryEventType
        from ctx_weft.protocols.capability import qualify
        fin = qualify("control:finish_task")
        turns = await memory.recall_recent(scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 500, pctx)
        for r in turns:
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id:
                for tc in (r.metadata.get("tool_calls") or []):
                    if tc.get("name") == fin and tc.get("id"):
                        return tc["id"]
        return None

    async def _relaunch_task_recap(
        self, *, session: "Session", template: "AgentTemplate",
        task_manager: "TaskManager", task: "Task", agent_id: str, boundary: str,
    ) -> None:
        """恢复：重建 LoopState 重跑一个被崩溃打断的段 recap，登记到传入 TM（track_background）。

        close 边界（finish/normal）：先从 memory 读占位 finish 对 tool_call_id + 据 task 状态定 outcome，
        register_close_synth，使重跑经 _replace_finish_report 替换占位对。best-effort：任何一步失败记日志、跳过。
        """
        from ctx_weft.core.loop.steps.background_observe import _CLOSE_BOUNDARIES
        from ctx_weft.protocols import MemoryScope
        try:
            lm = LifecycleManager(template_resolver=self._template_resolver)
            pctx0 = ProviderContext(session_id=session.id, tenant_id=session.tenant_id)
            agent, _tmpl = await lm.instantiate_agent(
                template_id=template.id if hasattr(template, "id") else session.template_id,
                session_id=session.id, tenant_id=session.tenant_id,
                existing_agent_id=agent_id, ctx=pctx0,
            )
            memory = self.providers.get_memory()
            scope = MemoryScope(session_id=session.id, task_id=task.id, agent_id=agent.id)
            provider_ctx = self._build_provider_ctx(session, task, agent)
            skill_index = self._skill_provider_index()
            assembler = self._build_assembler(memory, provider_ctx, skill_index)
            gateway = self._build_gateway(memory)
            llm = self._resolve_llm(session.llm_provider, session.llm_model)
            loop_ctx = self._build_loop_ctx(
                assembler, llm, memory, provider_ctx, gateway, skill_index, None, task_manager,
            )
            state = LoopState(
                run_id=generate_id("run"), session=session, task=task, agent=agent,
                scope=scope, extra={"template": template},
            )
            if boundary in _CLOSE_BOUNDARIES:
                tcid = await self._find_finish_pair_tool_call_id(memory, scope, task.id, provider_ctx)
                if tcid is not None:
                    outcome = "fail" if task.status == "FAILED" else "success"
                    register_close_synth(task.id, tcid, scope, outcome)
            launch_background_observe(state, loop_ctx, boundary=boundary)
        except Exception:
            logger.exception("recover: failed to relaunch task recap for task=%s", task.id)
```

> `template.id`：`AgentTemplate` 若无 `id` 属性则回退 `session.template_id`（`_relaunch_task_recap` 的调用方 Task 7 已解析出真实 `template_id`，可改为直接传参——见 Task 7 wiring，届时用传入的 `template_id`）。为避免歧义，Task 7 传 `template_id` 字符串而非从 template 取。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/unit/test_relaunch_task_recap.py -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_relaunch_task_recap.py tests/unit/conftest.py
git commit -m "feat(runtime): _relaunch_task_recap 恢复时重建 LoopState 重跑段 recap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 接线 `_recover_session_locked` —— 重跑 + 收尾

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:960-1027`（`_recover_session_locked` 的 resumable 判定与结尾）
- Test: `tests/integration/test_task_recap_recovery.py`

**Interfaces:**
- Consumes: `fold_pending_task_recap`（Task 2）、`self._relaunch_task_recap`（Task 6）、`task_manager.finalize_idle_session`（Task 5）、`task_from_projection`。
- Produces: 恢复流程新语义——(a) 折 `pending_recap` 逐个 `_relaunch_task_recap`（传本次重建的 `template_id`）；(b) 无 resumable 但**有 task** 时不再抛错，改为 `_register_and_drain` 后 `finalize_idle_session`；(c) 仅当**完全无 task** 才保留 `RuntimeError`。

- [ ] **Step 1: 写失败测试（复现报告的 bug）**

```python
# tests/integration/test_task_recap_recovery.py
import pytest
from ctx_weft.core.events.types import EventType


@pytest.mark.asyncio
async def test_stuck_finish_session_recovers_and_finalizes(recovery_harness):
    """root task FINISHED + TaskRecapStarted(无 Done) + session 投影 RUNNING（无 SESSION_FINISHED）
    → recover_session 重跑 recap 并把 session 收成 SUCCEEDED。"""
    runtime, event_store, session_id, task_id = recovery_harness  # 见 fixture 说明
    seen = []
    async def _collect(ev):
        seen.append((ev.type, (ev.payload or {}).get("final_status") or (ev.payload or {}).get("new_status")))
    runtime.event_bus.subscribe(_collect)  # 或用现有 event_store/bus 收集机制

    await runtime.recover_session(session_id)
    await _drain_background(runtime, session_id)  # 等 finalize_idle_session 的 gather 完成

    assert (EventType.SESSION_FINISHED, "SUCCEEDED") in seen
    # 幂等重复恢复不再卡：session 投影已终态
    from ctx_weft.core.control.reducers import rebuild_view
    view = await rebuild_view(event_store, session_id)
    assert view.sessions[session_id].status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_no_tasks_at_all_still_raises(recovery_harness_empty):
    runtime, _es, session_id = recovery_harness_empty
    with pytest.raises(RuntimeError, match="no resumable tasks"):
        await runtime.recover_session(session_id)
```

> `recovery_harness` fixture（放 `tests/integration/conftest.py` 或复用 `test_crash_recovery_reconcile.py` 的样板）：构造带 `InMemoryEventStore` 的 runtime，向 event store 写入事件序列 = `SESSION_CREATED{template_id, root_agent_id}` → `TASK_CREATED{root task}` → `TASK_STARTED` → `TASK_FINISHED` → `TASK_FINALIZED{outcome:success}` → `TASK_RECAP_STARTED{task_id, boundary:"finish", agent_id}`（**故意无 `TASK_RECAP_DONE`、无 `SESSION_FINISHED`**）。memory 预置该 task 的占位 finish 对（供 `_relaunch` 找 tool_call_id）。LLM 用返回固定 recap 的 fake（参照 `test_crash_recovery_reconcile.py:98` monkeypatch `launch_background_observe` 的做法——但本测试要真跑，故注入 fake LLM 而非 mock launch）。`_drain_background`：`await asyncio.gather(*runtime._task_managers[session_id]._background_asyncio_tasks)` 或轮询 session 投影至终态。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/integration/test_task_recap_recovery.py -v`
Expected: FAIL — `test_stuck_finish_session_recovers_and_finalizes` 抛 `RuntimeError("has no resumable tasks")`（当前行为）。

- [ ] **Step 3: 改写 `_recover_session_locked` 结尾**

在 `src/ctx_weft/core/runtime.py`，把 `resumable` 判定块（`:968-973`）与结尾（`:1024-1027`）改为：

现状：
```python
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        terminal_ids = {t.id for t in all_tasks if t.status in _TERMINAL}
        resumable = [t for t in all_tasks if t.status not in _TERMINAL]

        if not resumable:
            raise RuntimeError(f"Session {session_id!r} has no resumable tasks")
```
改为：
```python
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        terminal_ids = {t.id for t in all_tasks if t.status in _TERMINAL}
        resumable = [t for t in all_tasks if t.status not in _TERMINAL]

        # 折出被崩溃打断的段 recap（started 无 done）——覆盖全部 observe 段边界。
        from ctx_weft.core.control.reducers import fold_pending_task_recap
        events_all = await self.event_store.read_by_session(session_id)
        pending_recap = fold_pending_task_recap(events_all)

        # 既无可恢复 task 又无 task（空/损坏投影）→ 确无事可做，保留原抛错。
        if not resumable and not all_tasks:
            raise RuntimeError(f"Session {session_id!r} has no resumable tasks")
```

在结尾（现 `self._register_and_drain(session, task_manager)`，`:1027`）之前先重跑 recap、之后按需收尾。把结尾块改为：

```python
        # act 纯文本暂停（wait_for_user）冷应答注入（现有逻辑，位置不变）
        if user_reply is not None:
            await self._inject_user_reply(user_reply, session, task_manager)

        # 重跑被崩溃打断的段 recap（登记到新 TM；close 边界补 register_close_synth 替换占位 finish 对）
        tasks_by_id = {t.id: t for t in all_tasks}
        for tid, info in pending_recap.items():
            t = tasks_by_id.get(tid)
            if t is None:
                continue
            await self._relaunch_task_recap(
                session=session, template=template, task_manager=task_manager,
                task=t, agent_id=info.get("agent_id") or t.assigned_agent_id or "",
                boundary=info.get("boundary") or "finish",
            )

        self._register_and_drain(session, task_manager)

        # 无可恢复 task（所有 task 已终态）但 session 因崩溃未落终态 → 显式收尾：
        # gather 重跑的后台 recap 后发 SESSION_FINISHED（终态镜像 on_task_finished）。
        if not resumable:
            final_status = "FAILED" if session.failure_counter > 0 else "SUCCEEDED"
            await task_manager.finalize_idle_session(final_status)
```

> `_relaunch_task_recap` 的 `template` 形参在 Task 6 已用 `session.template_id` 兜底；此处传 `template`（本次已解析的 `AgentTemplate`）即可，`instantiate_agent` 用 `existing_agent_id` 不依赖 `template.id`。若类型检查报 `template.id` 不存在，改 Task 6 内 `template_id=session.template_id`（更稳）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/integration/test_task_recap_recovery.py -v`
Expected: PASS（stuck session 收成 SUCCEEDED；空 session 仍抛 `RuntimeError`）。

- [ ] **Step 5: 全量回归**

Run: `pytest tests/unit/test_background_observe.py tests/unit/test_recover_routing.py tests/unit/test_hitl_recovery.py tests/integration/test_crash_recovery_reconcile.py tests/integration/test_outage_resume.py -v`
Expected: PASS（全绿——恢复既有路径不受影响）。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/runtime.py tests/integration/test_task_recap_recovery.py tests/integration/conftest.py
git commit -m "feat(runtime): recover_session 重跑段 recap 并收尾崩溃遗留的非终态会话

被崩溃打断在 finish 收尾段 background observe 的 session 不再卡死：折出 pending
recap 逐个重跑（全边界），对所有 task 已终态的会话显式 finalize_idle_session 发
SESSION_FINISHED。闭合报告的"点恢复卡死"根因。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: 全量测试套件回归

**Files:** 无（仅运行）

- [ ] **Step 1: 跑全量**

Run: `pytest -q`
Expected: 全绿。若有失败，定位是否本方案引入（对照 Task 7 Step 5 的既有恢复测试）；修复后重跑。

- [ ] **Step 2: 提交（若有修复）**

```bash
git add -A
git commit -m "test: 对齐 Task Recap 恢复方案的存量测试

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 附：与 spec 的一处刻意偏离

spec §5.2 把「显式收尾」门控在 `pending_recap` 非空。本 plan 改为门控在**「无 resumable task 但 all_tasks 非空」**（Task 7 Step 3）——严格更稳健：它同时闭合了「observe 协程已 create_task 但崩在发 `TaskRecapStarted` 之前」的极小窗口（那时无 recap 标记，但 session 同样卡在非终态）。recap 标记仍**只**驱动是否重跑 observe，与收尾解耦。
