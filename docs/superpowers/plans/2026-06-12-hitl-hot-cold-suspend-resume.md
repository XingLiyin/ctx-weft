# HITL Hot/Cold Two-Layer Suspend-Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the "block-on-Future, timeout=failure" HITL model with a hot/cold two-layer model where a request is persisted at t=0, the coroutine hot-blocks within a timeout window, timeout *downgrades* (evicts memory, task → `SUSPENDED`) instead of failing, and any late answer resumes via deterministic dangling-tool_call reconcile — surviving process restarts.

**Architecture:** HITL writes are already events (`HitlRequired`/`HitlAnswered`/…); we fold them into `RunStateView.pending_hitl` via the reducer and rebuild the in-memory `HitlManager` on recover — **no new tables**. The hot path keeps today's in-place `invoke()` continuation. The cold path reuses the delegation suspend machinery: a new **`reconcile` step** scans the last assistant turn for tool_calls with no `TOOL_RESULT`, re-invokes only those through `gateway.invoke` (the single `TOOL_RESULT` write point), and a resolved HITL record keyed by `tool_call_id` short-circuits the gate. The unifying primitive is making `HitlManager.request()` **idempotent by `tool_call_id`**: an existing *pending* request re-waits (re-park on resume), an existing *resolved* request returns immediately (decision cache). Reconcile trigger lives in the `_resolve` resume funnel (runtime.py:660) and is memory-derived (no special marker), so it also repairs crash-mid-batch recovery for free.

**Tech Stack:** Python 3.12 (asyncio), dataclasses, event-sourcing reducer, pytest (asyncio). Core: `loomex-core/`. Host: `src/loomex_host/` (FastAPI + SQLAlchemy/Postgres projections).

---

## Phasing & Risk (from spec/07 §14)

Three phases, committed and reviewed separately, in increasing risk order:

- **Phase B — net-new, isolated, unit-testable (low/medium risk).** HITL event persistence (`pending_hitl` reducer fold + `HitlManager` rebuild), `tool_call_id` plumbing + idempotent-by-tool_call_id `request()`, the `reconcile` step, memory-based dangling detection in `_resolve`, and the **input-kind cold path** end-to-end. Adds `HitlCancelled`. Does **not** touch crash-recovery load-bearing paths or add eviction.
- **Phase C-1 — load-bearing recovery changes (high risk: regresses ALL session recovery).** Split `restore()` into SUSPENDED-on-children vs SUSPENDED-on-HITL; wire `recover_session` to rebuild the `HitlManager` from the replayed `pending_hitl`; make `recover()` skip `PAUSED_HITL` sessions; add the `projection_updater` `PAUSED_HITL`↔`RUNNING` maintenance (§9.1). Crash-mid-batch reconcile hookup falls out for free.
- **Phase C-2 / D — park/eviction + timeout downgrade + approval cold path (high risk + accepted semantic change).** `AuthorizationDecision.defer` + park signal that crosses `gateway`'s `except`, unwinds to `SUSPENDED` (not `FAILED`); timeout = hot→cold eviction (not failure); race single-authority transfer; approval-kind cold reconcile. Carries the D semantic change (deterministic at-least-once replay of crash-mid-batch tools).

**Hard rule between phases:** run the full `loomex-core` test suite green before starting the next phase. C-1 regresses *all* recovery; do not stack C-2 on an unverified C-1.

---

## File Structure

### Core (`loomex-core/src/loomex_core/`)
- `core/events/types.py` — **modify**: add `HITL_CANCELLED = "HitlCancelled"` to `EventType` (auto-joins `EVENT_TYPES`).
- `core/orchestrator/hitl_manager.py` — **modify (承重)**: `HitlRequest.tool_call_id`; `cancelled` status + `cancel()`; idempotent-by-`tool_call_id` `request()`; cold-resolve resume callback; `rebuild_from_pending()` classmethod/method; `find_for_tool_call()`.
- `core/control/types.py` — **modify**: add `HitlRequestView` dataclass + `RunStateView.pending_hitl: dict[str, HitlRequestView]`.
- `core/control/reducers.py` — **modify**: fold HITL events into `pending_hitl` (in `_apply`, `serialize_view`, `deserialize_view`).
- `core/loop/steps/reconcile.py` — **create**: `ReconcileStep` — scan last assistant turn, re-invoke dangling tool_calls via `gateway.invoke`, route to `act`.
- `core/loop/steps/__init__.py` — **modify**: export `ReconcileStep`.
- `core/runtime.py` — **modify (承重)**: register `reconcile` in `_build_step_driver`; memory-based dangling detection overriding `initial_step` in `_resolve`; rebuild `HitlManager` in `recover_session`; skip `PAUSED_HITL` in `recover`; `restore()` HITL-aware (C-1); park-signal SUSPENDED handling (C-2).
- `core/orchestrator/control_capability.py` — **modify**: `request_human_input` HITL request passes `tool_call_id` (from `ctx.extra`) and short-circuits via resolved HITL.
- `core/auth/authorizer.py` — **modify**: `AuthorizationDecision.defer: bool` (C-2); `HumanConfirmationAuthorizer` takes `tool_call_id`, short-circuits via resolved HITL.
- `core/loop/capability_gateway.py` — **modify (承重, C-2)**: pass `tool_call_id` into `authorize()`; park-signal / `defer` → don't invoke + raise park; reconcile re-use of memory `TOOL_RESULT`.
- `core/exceptions.py` (or `core/loop/driver.py`) — **create/modify (C-2)**: `HitlPark` exception that survives `gateway`'s `except Exception`.

### Host (`src/loomex_host/`)
- `persistence/postgres/projection_updater.py` — **modify (C-1)**: `SessionPausedHitl` → `PAUSED_HITL`; HITL resolve events → `RUNNING`.
- `api/sessions.py` — **modify (B/C-1)**: cold-resolve triggers session resume; `_submit_hitl_response` unchanged in signature.

### Tests
- `loomex-core/tests/unit/test_hitl.py` — **modify**: idempotency, `tool_call_id`, `cancel`, short-circuit.
- `loomex-core/tests/unit/test_hitl_reconcile.py` — **create**: reconcile step + dangling detection + reducer fold.
- `loomex-core/tests/unit/test_hitl_recovery.py` — **create (C-1)**: rebuild HitlManager from events, restore() SUSPENDED split.
- `loomex-core/tests/unit/test_hitl_park.py` — **create (C-2)**: park signal, eviction race, approval cold path.
- `loomex-core/tests/unit/test_snapshot_recovery.py` — **modify (C-1)**: assert PAUSED_HITL skipped by recover().

### Docs
- `docs/spec/01-events.md` — **modify**: add `HitlCancelled` to frozen HITL list.
- `docs/spec/05-authz-and-hitl.md` — **modify (after each phase)**: rewrite HITL lifecycle per spec/07 §13 delta.
- `docs/spec/07-hitl-suspend-resume.md` — **modify**: flip status banner from "设计提案，未实现" to implemented as phases land.

---

# PHASE B — Persistence + Reconcile + input-kind cold path

### Task B1: Register `HitlCancelled` event

**Files:**
- Modify: `loomex-core/src/loomex_core/core/events/types.py:124-129`
- Modify: `docs/spec/01-events.md:64`
- Test: `loomex-core/tests/unit/test_hitl.py`

- [ ] **Step 1: Write the failing test**

Add to `test_hitl.py`:

```python
def test_hitl_cancelled_is_registered_event() -> None:
    from loomex_core.core.events.types import EVENT_TYPES, EventType
    assert EventType.HITL_CANCELLED == "HitlCancelled"
    assert "HitlCancelled" in EVENT_TYPES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py::test_hitl_cancelled_is_registered_event -v`
Expected: FAIL — `AttributeError: HITL_CANCELLED`

- [ ] **Step 3: Add the enum member**

In `core/events/types.py`, in the `# ── HITL 域 ──` block after `HITL_TIMEOUT`:

```python
    HITL_TIMEOUT = "HitlTimeout"
    HITL_CANCELLED = "HitlCancelled"   # session 关闭 / interrupt / GC：收口悬挂 pending，不 requeue
```

(`EVENT_TYPES = frozenset(EventType)` picks it up automatically.)

- [ ] **Step 4: Update the frozen spec list**

In `docs/spec/01-events.md` line 64, append `` `HitlCancelled` ``:

```
`HitlRequired` `HitlApproved` `HitlAnswered` `HitlRejected` `HitlModified` `HitlTimeout` `HitlCancelled`
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py::test_hitl_cancelled_is_registered_event -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/events/types.py docs/spec/01-events.md loomex-core/tests/unit/test_hitl.py
git commit -m "feat(hitl): register HitlCancelled event (spec/07 §3)"
```

---

### Task B2: `HitlRequest.tool_call_id` + `cancelled` status + `cancel()`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py:38,41-67,80-106,148-185`
- Test: `loomex-core/tests/unit/test_hitl.py`

- [ ] **Step 1: Write the failing test**

Add to `test_hitl.py`:

```python
async def test_request_stores_tool_call_id() -> None:
    mgr = HitlManager()
    rid = await mgr.request(
        kind="approval", session_id="s1", task_id="t1",
        capability_id="fs:bash_exec", tool_call_id="tc_42", question="ok?",
    )
    assert mgr.get(rid).tool_call_id == "tc_42"


async def test_cancel_moves_to_cancelled_and_emits() -> None:
    bus = InProcessEventBus()
    seen = _collect(bus)
    mgr = HitlManager(event_bus=bus)
    rid = await _request(mgr, kind="input")
    await mgr.cancel(rid)
    assert mgr.get(rid).status == "cancelled"
    assert mgr.list_pending() == []
    assert "HitlCancelled" in seen


async def test_cancel_is_idempotent_after_resolve() -> None:
    mgr = HitlManager()
    rid = await _request(mgr, kind="input")
    await mgr.answer(rid, "done")
    await mgr.cancel(rid)                 # 已解决 → no-op
    assert mgr.get(rid).status == "accepted"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py::test_request_stores_tool_call_id tests/unit/test_hitl.py::test_cancel_moves_to_cancelled_and_emits tests/unit/test_hitl.py::test_cancel_is_idempotent_after_resolve -v`
Expected: FAIL — `tool_call_id` unexpected kwarg / no `cancel`.

- [ ] **Step 3: Implement**

In `hitl_manager.py`, extend `HitlStatus` and `HitlRequest`:

```python
HitlStatus = Literal["pending", "accepted", "rejected", "timeout", "cancelled"]
```

Add field to `HitlRequest` (after `task_id`/`agent_id` group, near `capability_id`):

```python
    tool_call_id: str = ""                        # 发起本次调用的 LLM tool_call id（§6 短路门控的键）
```

Extend `request()` signature + body (add `tool_call_id` param and store it + put it in the `HitlRequired` payload):

```python
    async def request(
        self,
        kind: HitlKind,
        session_id: str,
        task_id: str,
        *,
        capability_id: str = "",
        arguments: dict[str, Any] | None = None,
        question: str = "",
        context: str = "",
        agent_id: str = "",
        tool_call_id: str = "",
    ) -> str:
        """登记一个 HITL 请求，返回 request_id。发 HitlRequired + SessionPausedHitl。

        idempotent by tool_call_id（§6）：见 Task B3 覆写。此处先加字段透传。
        """
        rid = generate_id("hit")
        req = HitlRequest(
            id=rid, kind=kind, session_id=session_id, task_id=task_id, agent_id=agent_id,
            capability_id=capability_id, arguments=arguments or {}, question=question, context=context,
            tool_call_id=tool_call_id,
        )
        self._requests[rid] = req
        self._futures[rid] = asyncio.get_event_loop().create_future()
        logger.info("HITL requested [%s]: %s (%s)", kind, rid, question[:80])
        await self._emit(EventType.HITL_REQUIRED, req, payload={
            "approval_id": rid, "kind": kind, "capability_id": capability_id,
            "tool_call_id": tool_call_id,
            "question": question, "context": context,
        })
        await self._emit(EventType.SESSION_PAUSED_HITL, req, payload={})
        return rid
```

Add `cancel()` next to `reject()`:

```python
    async def cancel(self, request_id: str, *, message: str = "") -> HitlRequest:
        """收口一个悬挂 pending（session 关闭 / interrupt / GC）。cancelled + HitlCancelled。

        终态、不 requeue（§3）；已解决则幂等 no-op。
        """
        req = self._require(request_id)
        req.message = message
        return await self._resolve(req, "cancelled", EventType.HITL_CANCELLED)
```

`_resolve` already guards `if req.status != "pending": return req`, so cancel-after-resolve is a no-op (test 3 passes).

- [ ] **Step 4: Run to verify pass + no regression**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py -v`
Expected: PASS (all, including the 3 new + existing).

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py loomex-core/tests/unit/test_hitl.py
git commit -m "feat(hitl): add tool_call_id field + cancelled status/cancel() (spec/07 §3)"
```

---

### Task B3: Idempotent-by-`tool_call_id` `request()` (decision cache primitive)

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py` (`request`, add `find_for_tool_call`)
- Test: `loomex-core/tests/unit/test_hitl.py`

This is the linchpin of cold reconcile: re-requesting the same `tool_call_id` must NOT create a second request. Pending → return the same request id (re-arm a fresh future so the re-invoking coroutine can wait). Resolved → return the same id without arming a future (caller short-circuits via status).

- [ ] **Step 1: Write the failing test**

```python
async def test_request_idempotent_by_tool_call_id_pending() -> None:
    mgr = HitlManager()
    rid1 = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tcX")
    rid2 = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tcX")
    assert rid1 == rid2                      # 同一请求，不新建
    assert len(mgr.list_pending()) == 1


async def test_request_idempotent_by_tool_call_id_resolved_no_future() -> None:
    mgr = HitlManager()
    rid = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tcY")
    await mgr.answer(rid, "answered")
    # 重新请求同一 tool_call_id（cold reconcile 再入）：返回已解决记录、不重置状态
    rid2 = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tcY")
    assert rid2 == rid
    assert mgr.get(rid2).status == "accepted" and mgr.get(rid2).message == "answered"


def test_find_for_tool_call_returns_latest() -> None:
    # 无对应 tool_call_id → None
    mgr = HitlManager()
    assert mgr.find_for_tool_call("nope") is None


async def test_request_no_tool_call_id_not_deduped() -> None:
    mgr = HitlManager()
    r1 = await mgr.request(kind="input", session_id="s1", task_id="t1")  # 空 tool_call_id
    r2 = await mgr.request(kind="input", session_id="s1", task_id="t1")
    assert r1 != r2                          # 空 id 不去重（hot 多请求各自独立）
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py -k "idempotent_by_tool_call or find_for_tool_call or no_tool_call_id" -v`
Expected: FAIL — `rid1 != rid2` / no `find_for_tool_call`.

- [ ] **Step 3: Implement**

Add lookup helper:

```python
    def find_for_tool_call(self, tool_call_id: str) -> HitlRequest | None:
        """按 tool_call_id 取最近一条 HITL 请求（§6 权威决定缓存）；空 id → None。"""
        if not tool_call_id:
            return None
        matches = [r for r in self._requests.values() if r.tool_call_id == tool_call_id]
        if not matches:
            return None
        return max(matches, key=lambda r: r.created_at)
```

At the top of `request()`, before generating a new id, short-circuit on an existing `tool_call_id`:

```python
        # idempotent by tool_call_id（§6）：cold reconcile / resume 再入同一调用时不新建。
        existing = self.find_for_tool_call(tool_call_id)
        if existing is not None:
            if existing.status == "pending":
                # 仍未解决（resume 后 re-park）：补一个存活 future 供本次 await。
                fut = self._futures.get(existing.id)
                if fut is None or fut.done():
                    self._futures[existing.id] = asyncio.get_event_loop().create_future()
            # 已解决：保留终态，调用方据 status 短路（不补 future）。
            return existing.id
```

- [ ] **Step 4: Run to verify pass + full hitl suite**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py loomex-core/tests/unit/test_hitl.py
git commit -m "feat(hitl): idempotent request() by tool_call_id + find_for_tool_call (spec/07 §6)"
```

---

### Task B4: Reducer folds HITL events into `pending_hitl`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/control/types.py:59-95`
- Modify: `loomex-core/src/loomex_core/core/control/reducers.py` (`_apply`, `serialize_view`, `deserialize_view`)
- Test: `loomex-core/tests/unit/test_hitl_reconcile.py` (create)

- [ ] **Step 1: Write the failing test**

Create `loomex-core/tests/unit/test_hitl_reconcile.py`:

```python
"""HITL 持久化（事件折叠 pending_hitl）+ reconcile step（spec/07 §6/§9）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from loomex_core.core.control.reducers import reduce_events, serialize_view, deserialize_view
from loomex_core.core.events.types import Event, EventType

pytestmark = pytest.mark.asyncio

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ev(t: EventType, payload: dict, sec: int, *, task_id="t1", session_id="s1") -> Event:
    return Event(
        id=f"evt_{sec}", run_id="r1", sequence=sec, session_id=session_id,
        type=t, timestamp=_BASE + timedelta(seconds=sec), task_id=task_id, payload=payload,
    )


def test_reducer_folds_pending_then_removes_on_resolve() -> None:
    events = [
        _ev(EventType.SESSION_CREATED, {"template_id": "tpl"}, 0),
        _ev(EventType.HITL_REQUIRED, {
            "approval_id": "hit_1", "kind": "input", "capability_id": "control:rhi",
            "tool_call_id": "tc1", "question": "Which DB?",
        }, 1),
    ]
    view = reduce_events(events, run_id="s1")
    assert "hit_1" in view.pending_hitl
    hr = view.pending_hitl["hit_1"]
    assert hr.kind == "input" and hr.tool_call_id == "tc1" and hr.task_id == "t1"

    events.append(_ev(EventType.HITL_ANSWERED, {"approval_id": "hit_1"}, 2))
    view2 = reduce_events(events, run_id="s1")
    assert "hit_1" not in view2.pending_hitl   # 已解决 → 移除


def test_reducer_pending_survives_snapshot_roundtrip() -> None:
    events = [
        _ev(EventType.SESSION_CREATED, {"template_id": "tpl"}, 0),
        _ev(EventType.HITL_REQUIRED, {
            "approval_id": "hit_2", "kind": "approval", "capability_id": "fs:bash",
            "tool_call_id": "tc2", "question": "ok?",
        }, 1),
    ]
    view = reduce_events(events, run_id="s1")
    restored = deserialize_view(serialize_view(view))
    assert "hit_2" in restored.pending_hitl
    assert restored.pending_hitl["hit_2"].tool_call_id == "tc2"


def test_reducer_cancelled_removes_pending() -> None:
    events = [
        _ev(EventType.HITL_REQUIRED, {"approval_id": "h3", "kind": "input", "tool_call_id": "tc3"}, 0),
        _ev(EventType.HITL_CANCELLED, {"approval_id": "h3"}, 1),
    ]
    view = reduce_events(events, run_id="s1")
    assert "h3" not in view.pending_hitl
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py -v`
Expected: FAIL — `RunStateView` has no `pending_hitl`.

- [ ] **Step 3: Add `HitlRequestView` + `pending_hitl` to the view**

In `core/control/types.py`, add before `RunStateView`:

```python
@dataclass
class HitlRequestView:
    """未解决 HITL 请求的轻量投影（恢复用，spec/07 §9）。仅 pending 留存。"""

    id: str
    kind: str = "approval"        # approval | input
    session_id: str = ""
    task_id: str = ""
    capability_id: str = ""
    tool_call_id: str = ""
    question: str = ""
    context: str = ""
```

Add to `RunStateView` (after `agents`):

```python
    # Pending HITL requests folded from events (only unresolved; spec/07 §9)
    pending_hitl: dict[str, "HitlRequestView"] = field(default_factory=dict)
```

- [ ] **Step 4: Fold HITL events in `reducers.py`**

In `_apply`, add a HITL block (after the `# ── LLM / Context ──` block, before the function ends):

```python
    # ── HITL projection (spec/07 §9) ───────────────────────────────────────────
    elif t == EventType.HITL_REQUIRED:
        from loomex_core.core.control.types import HitlRequestView
        rid = p.get("approval_id", "")
        if rid:
            view.pending_hitl[rid] = HitlRequestView(
                id=rid,
                kind=p.get("kind", "approval"),
                session_id=ev.session_id,
                task_id=ev.task_id or "",
                capability_id=p.get("capability_id", ""),
                tool_call_id=p.get("tool_call_id", ""),
                question=p.get("question", ""),
                context=p.get("context", ""),
            )
    elif t in (
        EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
        EventType.HITL_REJECTED, EventType.HITL_TIMEOUT, EventType.HITL_CANCELLED,
    ):
        view.pending_hitl.pop(p.get("approval_id", ""), None)
```

> Note: `HITL_TIMEOUT` removes from `pending_hitl` here to keep Phase B reducer consistent with the *current* (still-failing) timeout semantics. Phase C-2 removes the timeout terminal entirely — at that point delete `HITL_TIMEOUT` from this tuple so a timeout no longer clears pending (it stays pending as a hot→cold downgrade). Flagged again in Task C2-x.

In `serialize_view`, add to the returned dict:

```python
        "pending_hitl": {
            rid: {
                "id": h.id, "kind": h.kind, "session_id": h.session_id,
                "task_id": h.task_id, "capability_id": h.capability_id,
                "tool_call_id": h.tool_call_id, "question": h.question, "context": h.context,
            }
            for rid, h in view.pending_hitl.items()
        },
```

In `deserialize_view`, rebuild it before constructing `RunStateView`:

```python
    from loomex_core.core.control.types import HitlRequestView
    pending_hitl: dict[str, HitlRequestView] = {}
    for rid, h in data.get("pending_hitl", {}).items():
        pending_hitl[rid] = HitlRequestView(
            id=h["id"], kind=h.get("kind", "approval"), session_id=h.get("session_id", ""),
            task_id=h.get("task_id", ""), capability_id=h.get("capability_id", ""),
            tool_call_id=h.get("tool_call_id", ""), question=h.get("question", ""),
            context=h.get("context", ""),
        )
```

…and pass `pending_hitl=pending_hitl` into the `RunStateView(...)` constructor call.

- [ ] **Step 5: Run to verify pass + reducer regression**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py tests/unit/test_snapshot_recovery.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/control/types.py loomex-core/src/loomex_core/core/control/reducers.py loomex-core/tests/unit/test_hitl_reconcile.py
git commit -m "feat(hitl): reducer folds HITL events into pending_hitl view (spec/07 §9)"
```

---

### Task B5: `HitlManager.rebuild_pending()` from view

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py`
- Test: `loomex-core/tests/unit/test_hitl_recovery.py` (create)

After recover, the in-memory `HitlManager` must be repopulated with the pending requests (futures empty → all subsequent answers go cold).

- [ ] **Step 1: Write the failing test**

Create `loomex-core/tests/unit/test_hitl_recovery.py`:

```python
"""HITL 跨重启恢复：从 pending_hitl 重建 HitlManager（spec/07 §9）。"""

from __future__ import annotations

import pytest

from loomex_core.core.control.types import HitlRequestView
from loomex_core.core.orchestrator.hitl_manager import HitlManager

pytestmark = pytest.mark.asyncio


def test_rebuild_pending_restores_requests_without_futures() -> None:
    mgr = HitlManager()
    mgr.rebuild_pending({
        "hit_1": HitlRequestView(
            id="hit_1", kind="input", session_id="s1", task_id="t1",
            capability_id="control:rhi", tool_call_id="tc1", question="Which DB?",
        ),
    })
    pend = mgr.list_pending(session_id="s1")
    assert len(pend) == 1 and pend[0].id == "hit_1"
    assert pend[0].tool_call_id == "tc1" and pend[0].status == "pending"
    # 无 future → 下一次 answer 走冷路径
    assert mgr.find_for_tool_call("tc1") is not None
    assert "hit_1" not in mgr._futures


async def test_answer_rebuilt_request_is_cold() -> None:
    mgr = HitlManager()
    mgr.rebuild_pending({
        "hit_1": HitlRequestView(id="hit_1", kind="input", session_id="s1",
                                 task_id="t1", tool_call_id="tc1"),
    })
    resolved, was_hot = await mgr.resolve_answer("hit_1", "use postgres")
    assert resolved.status == "accepted" and resolved.message == "use postgres"
    assert was_hot is False                  # 无 future → 冷
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_recovery.py -v`
Expected: FAIL — no `rebuild_pending` / `resolve_answer`.

- [ ] **Step 3: Implement**

Add to `HitlManager`:

```python
    def rebuild_pending(self, pending: dict[str, "HitlRequestView"]) -> None:
        """从 replayed view 的 pending_hitl 重建内存请求（spec/07 §9）。

        不建 future（_futures 空）→ 后续 answer/approve 自动走冷 resume；
        re-park（resume 后 reconcile 再 request 同一 tool_call_id）时由 request() 补 future。
        """
        for rid, h in pending.items():
            self._requests[rid] = HitlRequest(
                id=rid, kind=h.kind, session_id=h.session_id, task_id=h.task_id,
                capability_id=h.capability_id, tool_call_id=h.tool_call_id,
                question=h.question, context=h.context, status="pending",
            )
```

Add `HitlRequestView` to the `TYPE_CHECKING` import block:

```python
if TYPE_CHECKING:
    from loomex_core.core.events.bus import EventBus
    from loomex_core.core.control.types import HitlRequestView
```

Add a hot/cold-reporting resolve wrapper used by the host (returns `(req, was_hot)`):

```python
    async def resolve_answer(self, request_id: str, text: str) -> tuple[HitlRequest, bool]:
        """input-kind 应答，返回 (req, was_hot)。was_hot=False 时调用方须触发冷 resume。"""
        was_hot = self._is_hot(request_id)
        req = await self.answer(request_id, text)
        return req, was_hot

    async def resolve_approve(
        self, request_id: str, *, message: str = "",
        modified_arguments: dict[str, Any] | None = None,
    ) -> tuple[HitlRequest, bool]:
        was_hot = self._is_hot(request_id)
        req = await self.approve(request_id, message=message, modified_arguments=modified_arguments)
        return req, was_hot

    async def resolve_reject(self, request_id: str, *, message: str = "") -> tuple[HitlRequest, bool]:
        was_hot = self._is_hot(request_id)
        req = await self.reject(request_id, message=message)
        return req, was_hot

    def _is_hot(self, request_id: str) -> bool:
        """热 = 存在尚未完成的 future（活协程仍在 await）。"""
        fut = self._futures.get(request_id)
        return fut is not None and not fut.done()
```

- [ ] **Step 4: Run to verify pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_recovery.py tests/unit/test_hitl.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py loomex-core/tests/unit/test_hitl_recovery.py
git commit -m "feat(hitl): rebuild_pending() + hot/cold-reporting resolve wrappers (spec/07 §9)"
```

---

### Task B6: `request_human_input` short-circuits via resolved HITL + passes `tool_call_id`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/control_capability.py:602-626`
- Test: `loomex-core/tests/unit/test_hitl.py`

On cold reconcile, the dangling `request_human_input` tool_call is re-invoked. The control provider must reuse the already-resolved HITL (keyed by `tool_call_id` from `ctx.extra`) instead of opening a new one. The idempotent `request()` already returns the resolved request id; we must NOT `wait()` on a resolved one.

- [ ] **Step 1: Write the failing test**

Add to `test_hitl.py`. Extend `_invoke_control` to pass a `tool_call_id` via ctx.extra, then assert the short-circuit:

```python
async def _invoke_control_with_tcid(provider, name, args, tool_call_id):
    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1",
                          agent_id="agt_1", extra={"tool_call_id": tool_call_id})
    parts: list[str] = []
    async def drain():
        async for ev in provider.invoke(f"{PROVIDER_NAME}:{name}", args, ctx):
            if ev.kind == "result":
                parts.append(ev.payload.get("content", ""))
    return asyncio.create_task(drain()), parts


async def test_request_human_input_short_circuits_resolved_hitl() -> None:
    """cold reconcile 再入：tool_call_id 已有 answered HITL → 直接用答复、不再 park。"""
    mgr = HitlManager()
    provider, session, _ = _control_provider(mgr)
    # 预置一条已 answered 的请求（模拟冷应答已发生）
    rid = await mgr.request(kind="input", session_id="s1", task_id="tsk_1", tool_call_id="tc_re")
    await mgr.answer(rid, "use postgres")
    # reconcile 再入相同 tool_call_id
    task_h, parts = await _invoke_control_with_tcid(
        provider, "request_human_input", {"question": "Which DB?", "context": ""}, "tc_re",
    )
    await asyncio.wait_for(task_h, timeout=1.0)   # 不应阻塞等待
    assert parts and "use postgres" in parts[0]
    assert len(mgr.list_pending()) == 0           # 未新建 pending
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py::test_request_human_input_short_circuits_resolved_hitl -v`
Expected: FAIL — blocks (TimeoutError) because it `wait()`s on a resolved request / opens a new one.

- [ ] **Step 3: Implement**

In `control_capability.py`, replace the HITL request/wait block (lines 602-626) with a short-circuit-aware version:

```python
        # request_human_input：park 直到人类响应，把答复作为工具结果返回给 LLM。
        # cold reconcile 再入（tool_call_id 已有「已解决」HITL）时短路、不再 park（spec/07 §6）。
        if result.metadata.get(K.HITL_REQUESTED) and self._hitl_manager is not None:
            tool_call_id = (ctx.extra or {}).get("tool_call_id", "")
            existing = self._hitl_manager.find_for_tool_call(tool_call_id)
            if existing is not None and existing.status != "pending":
                approval = existing                       # 决定缓存命中：直接用
            else:
                approval_id = await self._hitl_manager.request(
                    kind="input",
                    session_id=ctx.session_id,
                    task_id=ctx.task_id or "",
                    agent_id=ctx.agent_id or "",
                    capability_id=capability_id,
                    arguments=arguments,
                    question=result.metadata.get("question", ""),
                    context=result.metadata.get("context", ""),
                    tool_call_id=tool_call_id,
                )
                approval = await self._hitl_manager.wait(approval_id)
            _, session = self._sessions.get(ctx.session_id, (None, None))
            if session is not None:
                session.status = "RUNNING"
            if approval.status == "rejected":
                content = f"Human declined: {approval.message}" if approval.message else "Human rejected the request."
            elif approval.status == "timeout":
                content = "Human did not respond in time (timeout)."
            else:
                content = approval.message or result.content
            yield CapabilityEvent(kind="result", payload={"content": content, "metadata": {}})
            return
```

> `ctx.extra` is populated by `CapabilityGateway.invoke` (capability_gateway.py:191) with `tool_call_id`. Verify `ProviderContext.extra` defaults to `{}` (it does per protocols/context).

- [ ] **Step 4: Run to verify pass + full hitl suite**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl.py -v`
Expected: PASS (all, including existing input-kind end-to-end tests which still park normally since their `tool_call_id` has no prior HITL).

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/control_capability.py loomex-core/tests/unit/test_hitl.py
git commit -m "feat(hitl): request_human_input short-circuits resolved HITL by tool_call_id (spec/07 §6)"
```

---

### Task B7: `ReconcileStep` — re-invoke dangling tool_calls

**Files:**
- Create: `loomex-core/src/loomex_core/core/loop/steps/reconcile.py`
- Modify: `loomex-core/src/loomex_core/core/loop/steps/__init__.py`
- Modify: `loomex-core/src/loomex_core/core/runtime.py:921-934` (register step)
- Test: `loomex-core/tests/unit/test_hitl_reconcile.py`

The step reads the last assistant turn's tool_calls from memory, finds those without a `TOOL_RESULT`, and re-invokes only those via `gateway.invoke` (which writes the missing `TOOL_RESULT`). Then routes to `act` so the assembler rebuilds the full, balanced turn.

- [ ] **Step 1: Write the failing test**

Add to `test_hitl_reconcile.py` (uses `InMemoryMemoryProvider` + a stub gateway):

```python
async def test_reconcile_invokes_only_dangling_tool_calls() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from loomex_core.core.loop.steps.reconcile import ReconcileStep
    from loomex_core.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
    from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")

    def ev(tp, content, sec, role, **md):
        return MemoryEvent(type=tp, scope=sc, content=content,
                           timestamp=base + timedelta(seconds=sec), role=role, metadata=md)

    # assistant turn with 2 tool_calls; only tc1 has a result → tc2 dangling
    await mem.ingest(ev(MemoryEventType.LLM_RESPONSE, "", 1, "assistant",
                        tool_calls=[{"id": "tc1", "name": "web", "input": {}},
                                    {"id": "tc2", "name": "request_human_input", "input": {"question": "?"}}]), pctx)
    await mem.ingest(ev(MemoryEventType.TOOL_RESULT, "web out", 2, "tool", tool_call_id="tc1"), pctx)

    invoked: list[str] = []
    async def fake_invoke(tool_name, arguments, state, ctx, tool_call_id=""):
        invoked.append(tool_call_id)
        return SimpleNamespace(content="human text", is_error=False)

    gateway = SimpleNamespace(invoke=fake_invoke)
    state = SimpleNamespace(
        scope=sc,
        agent=SimpleNamespace(id="ag1"),
        task=SimpleNamespace(id="t1", status="ACTIVE"),
        session=SimpleNamespace(id="s1"),
    )
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx, capability_gateway=gateway,
                          cancel_token=None, event_bus=_NullBus())

    outcome = await ReconcileStep().execute(state, ctx)
    assert invoked == ["tc2"]                 # 只补 dangling，tc1 跳过
    assert outcome.next_step == "act"


class _NullBus:
    async def emit(self, *_a, **_k): ...
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py::test_reconcile_invokes_only_dangling_tool_calls -v`
Expected: FAIL — `reconcile` module does not exist.

- [ ] **Step 3: Implement the step**

Create `core/loop/steps/reconcile.py`:

```python
"""ReconcileStep：resume 后、任何 LLM turn 之前，补完 dangling tool_call（spec/07 §6）。

被 park（HITL）或崩溃中途打断时，最近一个 assistant turn 的部分 tool_call 没有对应
TOOL_RESULT。直接把含 dangling 的消息序列喂给 LLM 会非法报错。本步对账：
  - 已有 TOOL_RESULT 的 tool_call → 跳过（复用持久结果，不重跑）
  - dangling 的 → gateway.invoke 执行（HITL 决定缓存按 tool_call_id 短路门控），
    由 gateway 写唯一 TOOL_RESULT
→ next_step="act"：assembler 重建出完整 turn，LLM 续跑。
"""

from __future__ import annotations

import logging

from loomex_core.core.loop.driver import LoopContext, LoopState, Step, StepOutcome
from loomex_core.protocols import MemoryEventType

logger = logging.getLogger(__name__)


class ReconcileStep(Step):
    name = "reconcile"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        dangling = await _dangling_tool_calls(state, ctx)
        if not dangling:
            logger.info("ReconcileStep: no dangling tool_calls for task %s", state.task.id)
            return StepOutcome(next_step="act")

        gateway = ctx.capability_gateway
        if gateway is None:
            raise RuntimeError("ReconcileStep requires a CapabilityGateway")

        for tc in dangling:
            if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                ctx.cancel_token.raise_if_cancelled()
            logger.info("ReconcileStep: re-invoking dangling tool_call %s (%s)", tc["id"], tc["name"])
            await gateway.invoke(
                tool_name=tc["name"],
                arguments=tc.get("input", {}) or {},
                state=state,
                ctx=ctx,
                tool_call_id=tc["id"],
            )

        return StepOutcome(next_step="act")


async def _dangling_tool_calls(state: LoopState, ctx: LoopContext) -> list[dict]:
    """最近一个 assistant turn 里，无对应 TOOL_RESULT 的 tool_call（按原顺序）。"""
    responses = await ctx.memory.recall_recent(
        scope=state.scope, types=[MemoryEventType.LLM_RESPONSE], limit=1, ctx=ctx.provider_ctx,
    )
    if not responses:
        return []
    tool_calls = responses[0].metadata.get("tool_calls") or []
    if not tool_calls:
        return []

    results = await ctx.memory.recall_recent(
        scope=state.scope, types=[MemoryEventType.TOOL_RESULT], limit=200, ctx=ctx.provider_ctx,
    )
    done_ids = {r.metadata.get("tool_call_id") for r in results}
    return [tc for tc in tool_calls if tc.get("id") not in done_ids]
```

> `recall_recent(LLM_RESPONSE, limit=1)` returns newest-first (per protocol docstring), so `[0]` is the last assistant turn. `metadata["tool_calls"]` carries `non_dispatch_tool_dicts` (act.py:172) shaped `{"id","name","input"}` — exactly what `gateway.invoke` needs. DISPATCH tools were excluded there, so they never appear as dangling (spec/07 §6).

- [ ] **Step 4: Export + register the step**

In `core/loop/steps/__init__.py`, add `ReconcileStep` to imports and `__all__` (match existing style).

In `runtime.py` `_build_step_driver` (line 922), add the import near the other step imports (top of file, with `from loomex_core.core.loop.steps.suspend import SuspendStep`):

```python
from loomex_core.core.loop.steps.reconcile import ReconcileStep
```

and add to the `steps={...}` dict:

```python
                "reconcile": ReconcileStep(),
```

- [ ] **Step 5: Run to verify pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/steps/reconcile.py loomex-core/src/loomex_core/core/loop/steps/__init__.py loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_hitl_reconcile.py
git commit -m "feat(loop): ReconcileStep re-invokes dangling tool_calls (spec/07 §6)"
```

---

### Task B8: Dangling detection in `_resolve` overrides `initial_step` → `reconcile`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/runtime.py:660-713` (the nested `_resolve`)
- Test: `loomex-core/tests/unit/test_hitl_reconcile.py`

All resume funnels through `_resolve` (normal drain, HITL answer, crash recover). After it picks the base `initial_step`, override to `"reconcile"` if the task's last assistant turn has dangling tool_calls.

- [ ] **Step 1: Write the failing test**

Add a focused unit test for the detection helper (extract it to a module-level function so it's testable without the whole runtime):

```python
async def test_resolve_reconcile_detection_helper() -> None:
    from datetime import UTC, datetime, timedelta
    from loomex_core.core.runtime import _task_has_dangling_tool_call
    from loomex_core.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
    from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")

    def ev(tp, sec, role, **md):
        return MemoryEvent(type=tp, scope=sc, content="", timestamp=base + timedelta(seconds=sec),
                           role=role, metadata=md)

    await mem.ingest(ev(MemoryEventType.LLM_RESPONSE, 1, "assistant",
                        tool_calls=[{"id": "tc1", "name": "web", "input": {}}]), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is True
    await mem.ingest(ev(MemoryEventType.TOOL_RESULT, 2, "tool", tool_call_id="tc1"), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py::test_resolve_reconcile_detection_helper -v`
Expected: FAIL — no `_task_has_dangling_tool_call`.

- [ ] **Step 3: Implement the module-level helper + wire into `_resolve`**

In `runtime.py`, add a module-level function (top-level, near other helpers). It reuses the same logic as the reconcile step's `_dangling_tool_calls` — extract that into the shared helper to keep one source of truth:

```python
async def _task_has_dangling_tool_call(memory, scope, provider_ctx) -> bool:
    """该 scope 最近一个 assistant turn 是否存在「有 tool_call、无 TOOL_RESULT」（spec/07 §6）。"""
    from loomex_core.protocols import MemoryEventType
    responses = await memory.recall_recent(
        scope=scope, types=[MemoryEventType.LLM_RESPONSE], limit=1, ctx=provider_ctx,
    )
    if not responses:
        return False
    tool_calls = responses[0].metadata.get("tool_calls") or []
    if not tool_calls:
        return False
    results = await memory.recall_recent(
        scope=scope, types=[MemoryEventType.TOOL_RESULT], limit=200, ctx=provider_ctx,
    )
    done_ids = {r.metadata.get("tool_call_id") for r in results}
    return any(tc.get("id") not in done_ids for tc in tool_calls)
```

Refactor `reconcile.py::_dangling_tool_calls` to import and not duplicate (optional but DRY): keep `_dangling_tool_calls` returning the list, and have `_task_has_dangling_tool_call` call it via `bool(await _dangling_tool_calls_for(memory, scope, ctx))`. Simplest: leave both; they share shape. (If you DRY it, put the list helper in `reconcile.py` and import it here.)

Wire into the nested `_resolve` (runtime.py:660). In the `case _:` normal branch and the `NormalTaskSettings(use_subagent=True)` branch (both return `"reason"`), compute the override. Cleanest: wrap the return at the end of `run_task`'s caller — but `_resolve` returns `initial_step`, so override there. Replace the two `return ..., "reason", ...` sites with a guarded value:

```python
                case NormalTaskSettings(use_subagent=True) as s:
                    ...
                    initial = await _reconcile_or(memory, sess_id, t, agent, "reason")
                    return agent, tmpl, initial, generate_id("run")

                case _:
                    agent = _default_agent(sess_id, t.assigned_agent_id or root_agent_id)
                    await _flush_tracking_memory(agent, t, task_manager, memory, sess_id, tenant_id)
                    initial = await _reconcile_or(memory, sess_id, t, agent, "reason")
                    return agent, template, initial, default_run_id
```

Add the small nested helper inside `_make_task_runner` (it has `tenant_id` in scope):

```python
        async def _reconcile_or(memory, sess_id: str, t: Task, agent: Agent, base: str) -> str:
            """base initial_step；若该 task 最近 assistant turn 有 dangling tool_call → reconcile。"""
            from loomex_core.protocols.context import ProviderContext as _PCtx
            from loomex_core.protocols.memory import MemoryScope as _Scope
            scope = _Scope(session_id=sess_id, task_id=t.id, agent_id=agent.id)
            pctx = _PCtx(session_id=sess_id, tenant_id=tenant_id, task_id=t.id, agent_id=agent.id)
            if await _task_has_dangling_tool_call(memory, scope, pctx):
                return "reconcile"
            return base
```

> Only `"reason"`-returning branches get reconcile; `compact`/`metadata_filler` tasks never have a task-layer act batch, so they're correctly excluded. On a brand-new task there's no LLM_RESPONSE yet → `False` → normal `"reason"` (zero overhead on the happy path beyond two cheap recall_recent calls; acceptable, and the spec accepts it as the unified funnel).

- [ ] **Step 4: Run to verify pass + scheduling regression**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_reconcile.py tests/unit/test_task_scheduling.py tests/unit/test_delegation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/runtime.py loomex-core/src/loomex_core/core/loop/steps/reconcile.py loomex-core/tests/unit/test_hitl_reconcile.py
git commit -m "feat(loop): _resolve overrides initial_step to reconcile on dangling tool_call (spec/07 §6)"
```

---

### Task B9: Phase B integration — input-kind cold path end-to-end (in-process)

**Files:**
- Test: `loomex-core/tests/integration/test_hitl_cold_input.py` (create)

Validate the whole Phase B chain in one process without restart: build a `HitlManager`, simulate a resolved-before-reconcile input request, drive a `ReconcileStep` through a real gateway + control provider, and assert a `TOOL_RESULT` with the human text lands in memory.

- [ ] **Step 1: Write the failing integration test**

Create `loomex-core/tests/integration/test_hitl_cold_input.py`. Model after `test_hitl.py`'s `_control_provider` + a real `CapabilityGateway` wired to the control provider and `InMemoryMemoryProvider`. Pre-ingest an assistant turn with a dangling `request_human_input` tool_call; pre-answer the HITL (cold); run `ReconcileStep`; assert memory now has a `TOOL_RESULT` for that tool_call_id containing the human text.

```python
async def test_cold_input_reconcile_writes_tool_result() -> None:
    # ... build mem, hitl mgr, control provider registered, capability cache + gateway ...
    # ingest LLM_RESPONSE with tool_calls=[{"id":"tcA","name":"<control:request_human_input>","input":{...}}]
    # rid = await mgr.request(kind="input", ..., tool_call_id="tcA"); await mgr.answer(rid, "use postgres")
    # run ReconcileStep().execute(state, ctx)
    # results = await mem.recall_recent(scope, [TOOL_RESULT], 10, pctx)
    # assert any(r.metadata.get("tool_call_id") == "tcA" and "use postgres" in r.content for r in results)
```

(Fill in concrete wiring mirroring `_invoke_control` + `_build_gateway`; the executor has both as references. Use the real capability name registered by the control provider for `request_human_input`.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd loomex-core && python -m pytest tests/integration/test_hitl_cold_input.py -v`
Expected: FAIL initially (wiring/asserts).

- [ ] **Step 3: Make it pass**

Wire the gateway + control provider exactly as in unit tests; no new production code should be needed if B1–B8 are correct. If the test reveals a gap (e.g., capability not in cache for reconcile invoke), fix the wiring in the test, not production — Phase B production code is complete.

- [ ] **Step 4: Run full core suite**

Run: `cd loomex-core && python -m pytest -q`
Expected: PASS (all). This is the **Phase B gate**.

- [ ] **Step 5: Commit + flip spec status**

Update `docs/spec/07-hitl-suspend-resume.md` banner to note Phase B (persistence + reconcile + input cold) is implemented; update `docs/spec/05-authz-and-hitl.md` per §13 deltas that Phase B covers (request-is-persisted; reducer pending_hitl; reconcile; HitlCancelled).

```bash
git add loomex-core/tests/integration/test_hitl_cold_input.py docs/spec/07-hitl-suspend-resume.md docs/spec/05-authz-and-hitl.md
git commit -m "test(hitl): input-kind cold reconcile integration + spec status (spec/07 phase B)"
```

---

# PHASE C-1 — Load-bearing recovery changes (HIGH RISK: all session recovery)

> Before starting: full core suite green. These changes route through paths every crash recovery uses. Add tests first; the empty-children-set trap (task_manager.py:119 `all(... for cid in <empty>)` is vacuously True) is the central hazard.

### Task C1-1: `recover_session` rebuilds `HitlManager` from `pending_hitl`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/runtime.py:743-812` (`recover_session`)
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

- [ ] **Step 1: Write the failing test**

Drive `recover_session` against an in-memory event store seeded with `SessionCreated` + a `TaskCreated`(ACTIVE) + `HitlRequired`(input, tool_call_id) and assert the runtime's `HitlManager` has the pending request after recover. (Use the test harness from `test_snapshot_recovery.py` for building a runtime with an in-memory event store; assert `runtime.hitl_manager.list_pending(session_id)` is non-empty.)

```python
async def test_recover_session_rebuilds_pending_hitl(runtime_with_event_store):
    runtime, store, session_id = runtime_with_event_store
    # seed events: SessionCreated, TaskCreated(ACTIVE t1), HitlRequired(input, tc1) ...
    await runtime.recover_session(session_id)
    pend = runtime.hitl_manager.list_pending(session_id=session_id)
    assert pend and pend[0].tool_call_id == "tc1"
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — pending empty (recover_session doesn't rebuild HITL).

- [ ] **Step 3: Implement**

In `recover_session`, after `view = await rebuild_view(...)` and before/after building the task_manager, repopulate the shared `HitlManager`:

```python
        # 重建内存 HitlManager（_futures 空 → 后续应答自动走冷 resume；spec/07 §9）
        if self.hitl_manager is not None and view.pending_hitl:
            self.hitl_manager.rebuild_pending(view.pending_hitl)
```

> Confirm the runtime exposes the shared `HitlManager` as `self.hitl_manager` (the same instance passed to `HumanConfirmationAuthorizer` / `ControlCapabilityProvider` at assembly, cli.py:95). If the attribute name differs, use the actual one; grep `hitl_manager` in `runtime.py`/`cli.py` to confirm the single shared instance.

- [ ] **Step 4: Run to verify pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_hitl_recovery.py
git commit -m "feat(hitl): recover_session rebuilds HitlManager from pending_hitl (spec/07 §9)"
```

---

### Task C1-2: `restore()` distinguishes SUSPENDED-on-children vs SUSPENDED-on-HITL

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/task_manager.py:94-133` (`restore`)
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

The bug (spec/07 §9.1): a HITL-parked task is `SUSPENDED` with **no children** → `all(cid in terminal_ids for cid in set())` is vacuously True → it gets re-queued as `PENDING` and runs before the human answers. Fix: a task with an unresolved pending HITL stays parked (not re-queued).

- [ ] **Step 1: Write the failing test**

```python
def test_restore_keeps_hitl_parked_task_suspended() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parked = Task(id="t1", session_id="s1", kind="reasoning", status="SUSPENDED")
    # 该 task 有未解决 pending HITL → 不应被重排
    tm.restore([parked], terminal_ids=set(), parked_task_ids={"t1"})
    assert tm.get_task("t1").status == "SUSPENDED"
    assert not tm._queue.has_pending()


def test_restore_requeues_suspended_on_children_when_all_terminal() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", kind="reasoning", status="SUSPENDED")
    child = Task(id="c", session_id="s1", kind="reasoning", status="FINISHED", parent_task_id="p")
    tm.restore([parent, child], terminal_ids={"c"}, parked_task_ids=set())
    assert tm.get_task("p").status == "PENDING"      # 子全终态 + 无 HITL → 重排
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — `restore()` takes no `parked_task_ids`; parked task gets PENDING.

- [ ] **Step 3: Implement**

Change `restore` signature and the `SUSPENDED` branch:

```python
    def restore(
        self,
        all_tasks: list[Task],
        terminal_ids: set[str],
        parked_task_ids: set[str] | None = None,
    ) -> list[Task]:
        """Rebuild task registry and re-queue resumable tasks after a crash.

        parked_task_ids：有未解决 pending HITL 的 task（spec/07 §9.1）——SUSPENDED 但须
        保持 parked、不重排，等 /answer 触发 resume。空集时退化为旧行为。
        """
        parked = parked_task_ids or set()
        ...
            if t.status == "SUSPENDED":
                if t.id in parked:
                    continue                       # HITL-park：保持挂起，不入队
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
```

Update the caller in `recover_session` (runtime.py:782) to pass parked ids derived from the replayed `pending_hitl` (filter by task_id):

```python
        parked_task_ids = {h.task_id for h in view.pending_hitl.values() if h.task_id}
        daemon_resumable = task_manager.restore(all_tasks, terminal_ids, parked_task_ids=parked_task_ids)
```

- [ ] **Step 4: Run to verify pass + recovery regression**

Run: `cd loomex-core && python -m pytest tests/unit/test_hitl_recovery.py tests/unit/test_snapshot_recovery.py tests/unit/test_task_scheduling.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/task_manager.py loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_hitl_recovery.py
git commit -m "fix(recovery): restore() distinguishes SUSPENDED-on-children vs on-HITL (spec/07 §9.1)"
```

---

### Task C1-3: `recover()` skips `PAUSED_HITL`; cold resolve triggers resume

**Files:**
- Modify: `loomex-core/src/loomex_core/core/runtime.py:814-837` (`recover`)
- Modify: `src/loomex_host/api/sessions.py` (cold-resolve → recover_session)
- Test: `loomex-core/tests/unit/test_snapshot_recovery.py`

- [ ] **Step 1: Write the failing test**

`recover()` currently marks every active session INTERRUPTED. A `PAUSED_HITL` session must be skipped so it stays resumable. Since `recover()` queries the event store, drive it with a session whose latest session-status event is `SessionPausedHitl` and assert the `on_session_interrupted` callback is NOT called for it. (Extend `list_active_session_ids` semantics or filter inside `recover` via the replayed/last status — see step 3.)

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — PAUSED_HITL session gets marked INTERRUPTED.

- [ ] **Step 3: Implement**

In `recover`, before invoking `on_session_interrupted`, check the session's last persisted status; skip if `PAUSED_HITL`. Cheapest correct approach without new queries: have the host's `on_session_interrupted` callback consult the persisted `sessions.status` (the projection, maintained in Task C1-4) and no-op for `PAUSED_HITL`. Implement the skip in the host callback (it owns the projection):

```python
# host side (startup recover wiring): skip PAUSED_HITL
async def _mark_interrupted(session_id: str) -> None:
    row_status = await state_store.get_session_status(session_id)
    if row_status == "PAUSED_HITL":
        return                      # 保持可恢复，不误标 INTERRUPTED（spec/07 §9）
    await state_store.update_session_status(session_id, "INTERRUPTED")
    ...
```

For the **cold-resolve resume trigger**: in `api/sessions.py::_submit_hitl_response`, switch to the hot/cold-reporting wrappers and trigger `recover_session` when cold:

```python
    req = pending[0]
    action, message = route_hitl_reply(req.kind, content)
    if action == "reject":
        _resolved, was_hot = await hitl.resolve_reject(req.id, message=message)
    elif action == "approve":
        _resolved, was_hot = await hitl.resolve_approve(req.id, message=message)
    else:
        _resolved, was_hot = await hitl.resolve_answer(req.id, message)
    if not was_hot:
        # 冷：活协程已驱逐/进程重启后 → 触发该 session resume（reconcile 续跑）
        runtime = deps.get_runtime()
        await runtime.recover_session(entry.session_id)
```

> In Phase B/C-1 (no eviction yet) `was_hot` is True for in-process answers, so this branch only fires after a restart — exactly the cold case. Phase C-2 (eviction) makes it fire in-process too.

- [ ] **Step 4: Run to verify pass**

Run: `cd loomex-core && python -m pytest tests/unit/test_snapshot_recovery.py -v` and host tests if present.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/runtime.py src/loomex_host/api/sessions.py loomex-core/tests/unit/test_snapshot_recovery.py
git commit -m "feat(hitl): recover skips PAUSED_HITL; cold resolve triggers recover_session (spec/07 §9)"
```

---

### Task C1-4: `projection_updater` maintains `PAUSED_HITL`↔`RUNNING`

**Files:**
- Modify: `src/loomex_host/persistence/postgres/projection_updater.py:47-105`
- Modify: `src/loomex_host/persistence/postgres/state_store.py` (add `get_session_status` if missing)
- Test: host test (create or extend persistence tests)

- [ ] **Step 1: Write the failing test**

Drive `ProjectionUpdater.on_event` with `SessionPausedHitl` then `HitlAnswered` and assert the `sessions` row status goes `PAUSED_HITL` then back to `RUNNING`. (Use the existing host persistence test harness / sqlite-or-pg fixture.)

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — no handler for `SessionPausedHitl` / HITL resolve.

- [ ] **Step 3: Implement**

In `projection_updater.py::_handle`, add handlers:

```python
        elif t == EventType.SESSION_PAUSED_HITL:
            await self._update_session(event.session_id, status="PAUSED_HITL")

        elif t in (
            EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
            EventType.HITL_REJECTED, EventType.HITL_CANCELLED,
        ):
            # HITL 解决 → session 回 RUNNING（若仍处暂停态）。
            await self._update_session_if_status(
                event.session_id, from_status="PAUSED_HITL", to_status="RUNNING",
            )
```

Add the conditional helper (avoid clobbering a terminal status that arrived first):

```python
    async def _update_session_if_status(self, session_id: str, from_status: str, to_status: str) -> None:
        async with self._factory() as db:
            async with db.begin():
                row = await db.get(SessionModel, session_id)
                if row is not None and row.status == from_status:
                    row.status = to_status
```

If `state_store` lacks `get_session_status` (needed by C1-3 host callback), add it:

```python
    async def get_session_status(self, session_id: str) -> str | None:
        async with self._factory() as db:
            row = await db.get(SessionModel, session_id)
            return row.status if row is not None else None
```

- [ ] **Step 4: Run to verify pass**

Run host persistence tests (e.g. `pytest tests/ -k projection or session_status`).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/loomex_host/persistence/postgres/projection_updater.py src/loomex_host/persistence/postgres/state_store.py
git commit -m "feat(host): projection maintains session PAUSED_HITL<->RUNNING (spec/07 §9.1)"
```

---

### Task C1-5: Phase C-1 gate — crash-mid-batch reconcile + full regression

**Files:**
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

- [ ] **Step 1: Add the "crash mid tool batch" recovery test**

Seed an event store / memory where a task's last assistant turn has 2 tool_calls, only 1 has a TOOL_RESULT, and the task is resumable (not HITL). After `recover_session` → `_resolve` must pick `reconcile` and the second tool runs (deterministic at-least-once — the §9 free win). Assert the missing TOOL_RESULT gets written and the run reaches `act`.

- [ ] **Step 2–4: Run, implement (no new prod code expected), verify**

Run: `cd loomex-core && python -m pytest -q` and host suite.
Expected: PASS (all). **Phase C-1 gate.**

- [ ] **Step 5: Commit + spec update**

Update `docs/spec/05-authz-and-hitl.md` and `07` for the C-1 deltas (PAUSED_HITL survives restart; restore() split; reconcile unifies crash recovery).

```bash
git add loomex-core/tests/unit/test_hitl_recovery.py docs/spec/05-authz-and-hitl.md docs/spec/07-hitl-suspend-resume.md
git commit -m "test(recovery): crash-mid-batch reconcile + phase C-1 gate (spec/07 §9)"
```

---

# PHASE C-2 / D — Park / eviction + timeout downgrade + approval cold path

> Highest risk + accepted semantic change (D): timeout no longer fails; crash-mid-`provider.invoke` is deterministic at-least-once replay. Confirm acceptance before starting.

### Task C2-1: `HitlPark` signal + `AuthorizationDecision.defer`

**Files:**
- Create: `loomex-core/src/loomex_core/core/loop/park.py` (or add to `driver.py`)
- Modify: `loomex-core/src/loomex_core/core/auth/authorizer.py:25-31`
- Test: `loomex-core/tests/unit/test_hitl_park.py` (create)

- [ ] **Step 1: Write the failing test**

```python
def test_hitl_park_is_base_exception_not_exception() -> None:
    from loomex_core.core.loop.park import HitlPark
    assert issubclass(HitlPark, BaseException)
    assert not issubclass(HitlPark, Exception)   # 必须穿过 gateway 的 except Exception
```

- [ ] **Step 2: Run → FAIL** (`cd loomex-core && python -m pytest tests/unit/test_hitl_park.py -v`)

- [ ] **Step 3: Implement**

Create `core/loop/park.py`:

```python
"""HitlPark：热→冷降级 / 显式挂起的专用信号（spec/07 §7）。

继承 BaseException（非 Exception）→ 穿过 CapabilityGateway 的 except Exception，不被当成
工具错误结果；一路上抛到 loop，落 task SUSPENDED（非 FAILED）。与真正的 interrupt 可区分。
"""

from __future__ import annotations


class HitlPark(BaseException):
    """携带挂起所需的最小信息。"""

    def __init__(self, request_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: request={request_id} tool_call={tool_call_id}")
        self.request_id = request_id
        self.tool_call_id = tool_call_id
```

Add `defer` to `AuthorizationDecision`:

```python
@dataclass
class AuthorizationDecision:
    allowed: bool
    message: str = ""
    modified_arguments: dict[str, Any] | None = None
    defer: bool = False   # spec/07 §7：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke）
```

- [ ] **Step 4: Run → PASS. Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/park.py loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): HitlPark BaseException + AuthorizationDecision.defer (spec/07 §7)"
```

---

### Task C2-2: Gateway honors `defer`/park — never invokes, raises `HitlPark`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/capability_gateway.py:125-216`
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write the failing test**

A stub authorizer returning `AuthorizationDecision(allowed=False, defer=True)` → `gateway.invoke` must raise `HitlPark` and the provider's `invoke` must NOT have run.

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

After computing `decision = await authorizer.authorize(...)` (gateway line 127), add the defer check *before* the `if not decision.allowed:` block:

```python
        if decision.defer:
            # 守住安全不变式：绝不调 provider.invoke；上抛 park 信号 → loop 落 SUSPENDED。
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(tool_call_id=tool_call_id)
```

Also pass `tool_call_id` into `authorize()` so HumanConfirmationAuthorizer can key by it (spec/07 §6 plumbing). Extend the `Authorizer.authorize` signature with a keyword `tool_call_id: str = ""` and update the call site:

```python
        decision = await authorizer.authorize(
            cap, state.agent, state.task, ctx, arguments, tool_call_id=tool_call_id,
        )
```

Update `Authorizer.authorize` abstract signature + all impls (`AllowAllAuthorizer`, `AllowListAuthorizer`, `HumanConfirmationAuthorizer`) to accept `tool_call_id: str = ""`.

> The `HitlPark` raised inside `invoke` propagates out of the `try/except Exception` (line 195-214) because it's `BaseException`. Confirm no broader `except BaseException` wraps it inside gateway (there isn't).

- [ ] **Step 4: Run → PASS** (`test_hitl_park.py` + `test_authorizer.py` for signature regression)

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/capability_gateway.py loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(gateway): defer -> raise HitlPark, never invoke; plumb tool_call_id to authorize (spec/07 §6/§7)"
```

---

### Task C2-3: ActStep + `_run_task` unwind `HitlPark` → task `SUSPENDED`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/act.py:212-222`
- Modify: `loomex-core/src/loomex_core/core/orchestrator/task_manager.py:208-258` (`_run_task`)
- Modify: `loomex-core/src/loomex_core/core/runtime.py` (`_run_loop` BaseException handling)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write the failing test**

Driving an ActStep whose gateway raises `HitlPark` mid-batch must set `state.task.status = "SUSPENDED"` and propagate cleanly (not mark FAILED). And `_run_task` seeing the task SUSPENDED-on-park must move it out of the running set (parked, not requeued).

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

In `act.py`, wrap the dangling-batch invoke so `HitlPark` sets SUSPENDED and unwinds:

```python
            for tc in tool_calls:
                if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                    ctx.cancel_token.raise_if_cancelled()
                try:
                    result = await _invoke_tool(tc, state, ctx)
                except HitlPark:
                    # 热→冷降级 / 显式挂起：被 park 的工具未执行；task 落 SUSPENDED，unwind。
                    state.task.status = "SUSPENDED"
                    raise
                tool_results.append({...})
```

Add `from loomex_core.core.loop.park import HitlPark` at top of `act.py`.

In `runtime.py::_run_loop`, ensure `HitlPark` is caught and turned into a clean SUSPENDED return rather than a FAILED run (it must land in the "SUSPENDED 正常返回" branch, spec/07 §7). After the `async for outcome in driver.run(...)` try-block, add a dedicated except that records park and returns the state with task SUSPENDED (do not emit RUN_FINISHED=FAILED):

```python
        except HitlPark:
            # park：干净挂起，不算失败；task 已置 SUSPENDED。
            logger.info("Run parked on HITL for task %s", task.id)
            # 发 RunFinished(SUSPENDED-ish) 或 RUN_PAUSED；保持 task SUSPENDED，交回 _run_task。
            ...
            return state
```

In `task_manager.py::_run_task`, the existing `except BaseException` in the inner try (line 224) discards staged subtasks and re-raises — `HitlPark` would currently propagate to the outer `except Exception` (line 253) which does NOT catch BaseException, so the task would error out of `_run_task` uncaught. Fix: after `await self._runner(...)` returns (park is swallowed in `_run_loop` and returns normally with task SUSPENDED), the existing `if task and task.status == "SUSPENDED":` branch (line 232) already moves it out of running set + drains **without requeue** — exactly park semantics. So the key is: `_run_loop` must *return normally* (not raise) on park, leaving `task.status == "SUSPENDED"`. Then no `_run_task` change is needed beyond confirming the SUSPENDED branch doesn't requeue (it doesn't — requeue happens only via `_try_resume_parent`, which won't fire since the parked task has no children driving it). The cold resume is driven later by the answer → `recover_session`.

> Decision: prefer "park swallowed in `_run_loop`, returns SUSPENDED" over "park raised through `_run_task`". This reuses the delegation SUSPENDED path (task_manager.py:232) verbatim. Write the test to assert `_run_loop`/run_task leaves the task SUSPENDED and out of the running set.

- [ ] **Step 4: Run → PASS** (`test_hitl_park.py` + `test_task_scheduling.py` + `test_delegation.py` for SUSPENDED-path regression)

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/steps/act.py loomex-core/src/loomex_core/core/runtime.py loomex-core/src/loomex_core/core/orchestrator/task_manager.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(loop): HitlPark unwinds to task SUSPENDED via delegation path (spec/07 §7)"
```

---

### Task C2-4: Timeout = hot→cold eviction (not failure) + single-authority race

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py` (`wait`, eviction, lock)
- Modify: `loomex-core/src/loomex_core/core/control/reducers.py` (drop HITL_TIMEOUT from the resolve tuple — see Task B4 note)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write the failing tests**

```python
async def test_timeout_evicts_to_cold_keeps_pending() -> None:
    mgr = HitlManager(timeout_sec=0)
    rid = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tc1")
    parked = await mgr.wait(rid)      # 超时 → 驱逐，不再是 timeout 终态
    assert parked is None or mgr.get(rid).status == "pending"   # 仍 pending（hot→cold）
    assert rid not in mgr._futures or mgr._futures[rid].done()  # future 驱逐
    assert mgr.list_pending()         # pending 保留


async def test_eviction_vs_answer_single_authority() -> None:
    # answer 先到 → 热续跑，取消驱逐；驱逐先到 → answer 走冷。二选一、不双投。
    ...
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

Rework `wait()`: on `TimeoutError`, instead of setting `status="timeout"` + `HitlTimeout`, **evict** (pop the future, keep request `pending`) and raise `HitlPark` so the awaiting coroutine unwinds:

```python
    async def wait(self, request_id: str) -> HitlRequest:
        future = self._futures.get(request_id)
        if future is None:
            raise KeyError(f"No HITL request found: {request_id}")
        try:
            async with asyncio.timeout(self._timeout_sec):
                return await future
        except TimeoutError:
            async with self._lock:
                req = self._requests[request_id]
                if req.status != "pending":
                    return req                 # answer 先到（race）：走热已解决
                # 驱逐：移除 future，保留 pending；task 由 park 信号落 SUSPENDED。
                self._futures.pop(request_id, None)
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(request_id=request_id, tool_call_id=req.tool_call_id)
```

Add `self._lock = asyncio.Lock()` in `__init__`. Make `_resolve` take the lock around the future-set / status-transition so eviction vs answer is atomic (single-authority transfer, spec/07 §8):

```python
    async def _resolve(self, req, status, event_type):
        async with self._lock:
            if req.status != "pending":
                return req
            req.status = status
            req.resolved_at = now_utc()
            future = self._futures.get(req.id)
            hot = future is not None and not future.done()
            if hot:
                future.set_result(req)
        await self._emit(event_type, req, payload={"approval_id": req.id})
        return req
```

> `_is_hot` must be read under the same lock when used to decide cold resume — acceptable to snapshot it just before `answer()` since `_resolve` re-checks atomically. For the host cold-trigger, rely on `_resolve`'s atomic transition: if the future was already evicted, `hot=False`, the request resolves, and the host's `was_hot` (snapshotted) may race. To be safe, derive `was_hot` from whether `_resolve` actually set a live future — return it from the resolve wrappers instead of pre-snapshotting. Adjust `resolve_answer` to return the hot-ness determined inside `_resolve` (thread a return value or set `req`-attached transient flag).

Remove `HitlTimeout` from the reducer resolve tuple (Task B4) and from `projection_updater` HITL-resolve tuple (Task C1-4): a timeout no longer resolves; the request stays pending. Keep `HITL_TIMEOUT` enum for backward-compat but stop emitting it (the eviction path emits nothing — spec/07 §3 "不发终态事件").

Update `test_hitl.py::test_wait_times_out_and_emits` and `test_resolve_is_idempotent_after_timeout` to the new semantics (timeout → HitlPark + still pending), or replace them.

- [ ] **Step 4: Run → PASS** (`test_hitl.py`, `test_hitl_park.py`)

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py loomex-core/src/loomex_core/core/control/reducers.py src/loomex_host/persistence/postgres/projection_updater.py loomex-core/tests/unit/test_hitl.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): timeout = hot->cold eviction (not failure) + single-authority race (spec/07 §3/§8)"
```

---

### Task C2-5: approval-kind cold path via reconcile + defer

**Files:**
- Modify: `loomex-core/src/loomex_core/core/auth/authorizer.py` (`HumanConfirmationAuthorizer.authorize`)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

The hot approval path is unchanged (request+wait inside `authorize`, returns decision, gateway invokes in place). The cold path: on reconcile re-invoke, `authorize` finds a resolved HITL for the `tool_call_id` and returns the decision *without* re-requesting; if still pending after a restart-rebuild, it re-waits (re-park) or returns `defer` to suspend.

- [ ] **Step 1: Write the failing test**

```python
async def test_authorize_cold_uses_resolved_decision_no_new_hitl() -> None:
    mgr = HitlManager()
    # 预置 approved（带改参）的请求，键 tool_call_id
    rid = await mgr.request(kind="approval", session_id="s1", task_id="t1", tool_call_id="tcZ")
    await mgr.approve(rid, modified_arguments={"command": "ls -la"})
    authorizer = HumanConfirmationAuthorizer(hitl_manager=mgr)
    d = await authorizer.authorize(_cap(), _agent(), SimpleNamespace(id="t1"),
                                   ProviderContext(session_id="s1", tenant_id="default"),
                                   {"command": "ls"}, tool_call_id="tcZ")
    assert d.allowed and d.modified_arguments == {"command": "ls -la"}
    assert len(mgr.list_pending()) == 0      # 未新建
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

Rewrite `HumanConfirmationAuthorizer.authorize`:

```python
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        existing = self.hitl_manager.find_for_tool_call(tool_call_id)
        if existing is not None and existing.status != "pending":
            approval = existing                          # 决定缓存命中（cold reconcile）
        else:
            approval_id = await self.hitl_manager.request(
                kind="approval", session_id=agent.session_id,
                task_id=task.id if task else "", agent_id=agent.id,
                capability_id=capability.id, arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?", context=capability.description,
                tool_call_id=tool_call_id,
            )
            try:
                approval = await self.hitl_manager.wait(approval_id)
            except Exception:
                raise            # HitlPark propagates (BaseException) → gateway suspends
        if approval.accepted:
            return AuthorizationDecision(allowed=True, message=approval.message,
                                         modified_arguments=approval.modified_arguments)
        return AuthorizationDecision(allowed=False, message=approval.message)
```

> `wait()` now raises `HitlPark` on eviction (Task C2-4); since it's `BaseException`, the bare `except Exception` won't catch it — it propagates through `authorize` → gateway (which never invoked, decision came before execution) → unwinds to SUSPENDED. exactly-once invariant holds (spec/07 §6 exactly-once).

- [ ] **Step 4: Run → PASS** (`test_hitl.py`, `test_hitl_park.py`, `test_authorizer.py`)

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): approval-kind cold path via resolved-decision cache (spec/07 §5/§6)"
```

---

### Task C2-6: Phase C-2 gate — full regression + spec finalize

- [ ] **Step 1: Run full suites**

Run: `cd loomex-core && python -m pytest -q` and the host suite.
Expected: PASS (all). **Phase C-2 gate.**

- [ ] **Step 2: Finalize specs**

Rewrite `docs/spec/05-authz-and-hitl.md` HITL lifecycle per spec/07 §13 (delete timeout-failure semantics; document hot/cold). Flip `docs/spec/07` banner to "已实现".

- [ ] **Step 3: Commit**

```bash
git add docs/spec/05-authz-and-hitl.md docs/spec/07-hitl-suspend-resume.md
git commit -m "docs(spec): finalize HITL hot/cold model in 05/07 (spec/07 phase C-2)"
```

---

## Self-Review

**1. Spec coverage:**
- §2 hot/cold + `_futures` discriminant → C2-4 (`_is_hot`), B5 (`was_hot`). ✓
- §3 state machine (cancelled; timeout=downgrade) → B2 (cancel), C2-4 (timeout eviction). ✓
- §4 input-kind hot/cold → B6 (short-circuit), B9 (cold e2e). ✓
- §5 approval-kind hot/cold → C2-5. ✓
- §6 reconcile (dangling, decision cache, exactly-once, plumbing) → B7, B8, B3, C2-2 (`tool_call_id` to authorize). ✓
- §7 park signal pipeline → C2-1, C2-2, C2-3. ✓
- §8 eviction-vs-answer race → C2-4. ✓
- §9 persistence via events + reducer + rebuild → B4, B5, C1-1. ✓
- §9.1 projection PAUSED_HITL → C1-4. ✓
- §9 restore() split + recover skip → C1-2, C1-3. ✓
- §10 host/API → C1-3 (cold trigger), C1-4. ✓
- §12 HitlCancelled + spec/01 → B1. ✓

**2. Placeholder scan:** B9, C1-1, C1-3..C1-5, C2-3..C2-4 contain `...` in *test scaffolding / branch bodies* where the exact harness wiring depends on existing fixtures the executor must read (`test_snapshot_recovery.py` harness, host persistence fixtures, `_run_loop` tail). These are flagged as "mirror existing X" with the concrete assertions given — acceptable for recovery-path tests that must reuse project fixtures, but the executor should read the named reference file before writing them. Production-code steps contain complete code.

**3. Type consistency:** `tool_call_id` (str) consistent across HitlRequest, request(), find_for_tool_call, authorize(), HitlRequestView, reducer payload. `pending_hitl: dict[str, HitlRequestView]` consistent in types.py, reducers (serialize/deserialize/_apply), rebuild_pending, recover_session. `resolve_answer/approve/reject` return `(HitlRequest, bool)` consistent between B5 and host C1-3. `HitlPark(BaseException)` consistent C2-1..C2-5.

**Open risk flagged for executor:** C2-4's `was_hot` correctness under the eviction-vs-answer race needs the hot-ness determined *inside* the locked `_resolve`, not pre-snapshotted (noted in C2-4 step 3). Verify with `test_eviction_vs_answer_single_authority` before the C-2 gate.
