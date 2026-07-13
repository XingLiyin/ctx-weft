# HITL Hot/Cold — Phase C (Recovery + Park/Eviction) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the hot/cold HITL model: make HITL survive process restart (rebuild `HitlManager` + parked tasks on recover, persist session `PAUSED_HITL`), then turn timeout into a hot→cold *eviction* (not a failure) via a `HitlPark` signal that cleanly unwinds the active coroutine to `SUSPENDED`, plus the approval-kind cold path.

**Architecture:** Phase B already persists HITL as events, folds them into `RunStateView.pending_hitl`, and added the `reconcile` step + idempotent-by-`tool_call_id` `request()`. Phase C wires those into the load-bearing recovery paths (`restore()`, `recover_session`, `recover`, host projection) — **no new tables** — then adds the park signal: a `HitlPark(BaseException)` that crosses `CapabilityGateway`'s `except Exception`, is caught explicitly in `_run_loop` to return normally with `task.status="SUSPENDED"` (reusing the existing delegation-suspend `RUN_FINISHED(SUSPENDED)` shape), with timeout-eviction and answer racing under a single lock.

**Tech Stack:** Python 3.12 (asyncio), event-sourcing reducer, pytest (asyncio auto-mode). Core: `loomex-core/`. Host: `src/loomex_host/` (FastAPI + SQLAlchemy/Postgres projections).

---

## Status / Prerequisites

**Phase B is DONE and committed** on branch `feature/hitl-hot-cold` (9 commits, full core suite green = 147 passed). Phase B delivered: `HitlCancelled` event; `HitlRequest.tool_call_id` + `cancelled`/`cancel()`; idempotent `request()` by `tool_call_id` + `find_for_tool_call`; reducer `pending_hitl` fold (+ snapshot round-trip); `HitlManager.rebuild_pending` + `resolve_answer/approve/reject` (returning `(req, was_hot)` via a pre-snapshot `_is_hot`); `request_human_input` cold short-circuit; `ReconcileStep` + driver registration; `_resolve` memory-based dangling detection (`_task_has_dangling_tool_call`).

**Run the full suite green before EACH phase gate:** `cd loomex-core && python -m pytest -p no:warnings -q`.

**Environment:** Windows; Bash tool = Git Bash (POSIX). Tests: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest <args>`. Branch `feature/hitl-hot-cold` is checked out. Unrelated pre-existing uncommitted spec/06 work exists in the tree — always `git add` only this task's files, never `git add -A`. End commit messages with a blank line then `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

### Phase C-1 (recovery — HIGH RISK: touches all session recovery)
- `loomex-core/src/loomex_core/core/orchestrator/task_manager.py` — **modify** `restore()` (lines 94-133): add `parked_task_ids` param; SUSPENDED-on-HITL tasks stay parked (not re-queued).
- `loomex-core/src/loomex_core/core/runtime.py` — **modify** `recover_session()` (lines 743-812): rebuild `self.hitl_manager` from `view.pending_hitl`; pass `parked_task_ids` to `restore()`.
- `src/loomex_host/persistence/postgres/state_store.py` — **modify**: add `get_session_status()`.
- `src/loomex_host/persistence/postgres/projection_updater.py` — **modify** `_handle()`: `SessionPausedHitl` → `PAUSED_HITL`; HITL resolve events → `RUNNING` (conditional).
- `src/loomex_host/api/startup.py` — **modify** `_on_session_interrupted` (line 204): skip `PAUSED_HITL` sessions.
- `src/loomex_host/api/sessions.py` — **modify** `_submit_hitl_response` (lines 101-134): use `resolve_*` wrappers; on cold (`not was_hot`) trigger `runtime.recover_session`.
- Tests: `loomex-core/tests/unit/test_hitl_recovery.py` (extend), `loomex-core/tests/unit/test_hitl_projection.py` (host; create or place under host tests).

### Phase C-2/D (park / eviction / approval cold — HIGHEST RISK + accepted semantic change)
- `loomex-core/src/loomex_core/core/loop/park.py` — **create**: `HitlPark(BaseException)`.
- `loomex-core/src/loomex_core/core/auth/authorizer.py` — **modify**: `AuthorizationDecision.defer`; all `authorize()` signatures gain `tool_call_id`; `HumanConfirmationAuthorizer` short-circuits via resolved HITL.
- `loomex-core/src/loomex_core/core/loop/capability_gateway.py` — **modify** `invoke()` (lines 125-216): pass `tool_call_id` into `authorize()`; `defer` → raise `HitlPark` (never invoke provider).
- `loomex-core/src/loomex_core/core/loop/steps/act.py` — **modify** the tool-batch loop (lines 212-222): `except HitlPark` → set `task.status="SUSPENDED"`, re-raise.
- `loomex-core/src/loomex_core/core/runtime.py` — **modify** `_run_loop` (lines 977-993): `except HitlPark` → return normally with `SUSPENDED`.
- `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py` — **modify**: `_lock`; `wait()` timeout → evict + raise `HitlPark`; `_resolve` returns `(req, was_hot)` under lock; `resolve_*` thread that value (replacing B5's pre-snapshot `_is_hot`).
- `loomex-core/src/loomex_core/core/control/reducers.py` — **modify**: drop `HITL_TIMEOUT` from the `pending_hitl` removal tuple.
- `src/loomex_host/persistence/postgres/projection_updater.py` — **modify**: drop `HITL_TIMEOUT` from the resolve tuple (added in C1-3).
- Tests: `loomex-core/tests/unit/test_hitl_park.py` (create); update `loomex-core/tests/unit/test_hitl.py` (timeout tests) + `loomex-core/tests/unit/test_authorizer.py` (authorizer signatures).

---

# PHASE C-1 — Recovery (HIGH RISK)

> Tasks are ordered so dependencies are satisfied: `restore()` split (C1-1) lands before `recover_session` wiring (C1-2).

### Task C1-1: `restore()` distinguishes SUSPENDED-on-children vs SUSPENDED-on-HITL

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/task_manager.py:94-133`
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

The trap (spec/07 §9.1): a HITL-parked task is `SUSPENDED` with **no children** → `all(cid in terminal_ids for cid in set())` is vacuously True → it gets re-queued `PENDING` and runs before the human answers. Fix: a task whose id is in `parked_task_ids` stays parked.

- [ ] **Step 1: Write the failing tests**

Append to `loomex-core/tests/unit/test_hitl_recovery.py`:

```python
def test_restore_keeps_hitl_parked_task_suspended() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parked = Task(id="t1", session_id="s1", kind="reasoning", status="SUSPENDED")
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
    assert tm.get_task("p").status == "PENDING"


def test_restore_parked_ids_default_none_is_old_behavior() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", kind="reasoning", status="SUSPENDED")
    tm.restore([parent], terminal_ids=set())   # no parked_task_ids → old behavior: requeue
    assert tm.get_task("p").status == "PENDING"
```

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_recovery.py -k restore -v`
Expected: FAIL — `restore()` got an unexpected keyword argument `parked_task_ids`.

- [ ] **Step 3: Implement**

In `task_manager.py`, change the `restore` signature and the `SUSPENDED` branch. The current method body's loop is:

```python
    def restore(self, all_tasks: list[Task], terminal_ids: set[str]) -> list[Task]:
        """Rebuild task registry and re-queue resumable tasks after a crash.

        Returns daemon tasks that need to be re-spawned by the caller.
        """
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}

        for t in all_tasks:
            self._tasks[t.id] = t
            if t.parent_task_id:
                self._parent_map[t.id] = t.parent_task_id
                self._children_of.setdefault(t.parent_task_id, set()).add(t.id)

        for tid in terminal_ids:
            self._queue._completed.add(tid)

        daemon_resumable: list[Task] = []
        for t in all_tasks:
            if t.status in _TERMINAL:
                continue
            if getattr(t.settings, "is_daemon", False):
                daemon_resumable.append(t)
                continue
            if t.status == "SUSPENDED":
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
            else:
                t.status = "PENDING"
                t.retry_count = 0
                blocked = {dep for dep in (t.dag_deps or []) if dep not in terminal_ids}
                self._queue.push(QueueEntry(
                    task_id=t.id, session_id=self._session_id,
                    priority=t.priority, blocked_by=blocked,
                ))

        return daemon_resumable
```

Replace the signature/docstring and the `if t.status == "SUSPENDED":` block:

```python
    def restore(
        self,
        all_tasks: list[Task],
        terminal_ids: set[str],
        parked_task_ids: set[str] | None = None,
    ) -> list[Task]:
        """Rebuild task registry and re-queue resumable tasks after a crash.

        ``parked_task_ids``: tasks with an unresolved pending HITL (spec/07 §9.1) —
        SUSPENDED but must STAY parked (not re-queued) until ``/answer`` resumes them.
        Empty/None → legacy behavior. Returns daemon tasks for the caller to re-spawn.
        """
        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        parked = parked_task_ids or set()
```

…and in the loop:

```python
            if t.status == "SUSPENDED":
                if t.id in parked:
                    continue                       # HITL-park：保持挂起，不入队（spec/07 §9.1）
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
```

(Leave the `else` branch unchanged.)

- [ ] **Step 4: Run, verify PASS + scheduling/recovery regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_recovery.py tests/unit/test_task_scheduling.py tests/unit/test_snapshot_recovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/task_manager.py loomex-core/tests/unit/test_hitl_recovery.py
git commit -m "fix(recovery): restore() distinguishes SUSPENDED-on-children vs on-HITL (spec/07 §9.1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C1-2: `recover_session` rebuilds `HitlManager` + passes `parked_task_ids`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/runtime.py:743-812`
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

- [ ] **Step 1: Write the failing integration test**

Append to `test_hitl_recovery.py`. This builds a real runtime, seeds its event store with a HITL-parked task, calls `recover_session`, and asserts the shared `HitlManager` holds the pending request and the parked task did not run.

```python
async def test_recover_session_rebuilds_pending_hitl_and_parks() -> None:
    import asyncio
    from loomex_core.core import LoomeXRuntime
    from loomex_core.core.events.types import Event, EventType
    from loomex_core.providers.llm.mock import MockLLMAdapter, MockResponse
    from loomex_core.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template
    from datetime import datetime, timezone

    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="should not run")])
    runtime = LoomeXRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    ts = datetime(2026, 6, 12, tzinfo=timezone.utc)
    def ev(seq, type_, **payload):
        task_id = payload.pop("task_id", None)
        return Event(id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="ses_1",
                     type=type_, timestamp=ts, task_id=task_id, payload=payload)

    seed = [
        ev(1, EventType.SESSION_CREATED, user_prompt="do it", template_id="tpl_echo",
           root_agent_id="agt_root"),
        ev(2, EventType.RUN_STARTED),
        ev(3, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "PENDING", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root"}),
        ev(4, EventType.TASK_STARTED, task_id="tsk_1", assigned_agent_id="agt_root"),
        ev(5, EventType.HITL_REQUIRED, task_id="tsk_1", approval_id="hit_1", kind="input",
           capability_id="control:request_human_input", tool_call_id="tcA", question="Which DB?"),
        ev(6, EventType.TASK_SUSPENDED, task_id="tsk_1"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover_session("ses_1")
    await asyncio.sleep(0)                       # let the (empty) drain settle

    pend = runtime.hitl_manager.list_pending(session_id="ses_1")
    assert len(pend) == 1 and pend[0].tool_call_id == "tcA"
    assert llm.last_request is None             # parked task did not run
```

> The seeded `TaskSuspended` makes the task `SUSPENDED`; combined with the rebuilt `pending_hitl` for `tsk_1`, `restore(parked_task_ids={"tsk_1"})` (C1-1) keeps it parked, so the mock LLM is never called.

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_recovery.py::test_recover_session_rebuilds_pending_hitl_and_parks -v`
Expected: FAIL — `runtime.hitl_manager.list_pending(...)` is empty (and/or the task runs).

- [ ] **Step 3: Implement**

In `runtime.py::recover_session`, the current code (around lines 760-782) is:

```python
        from loomex_core.core.control.converters import session_from_projection, task_from_projection
        session = session_from_projection(sess_proj)
        all_tasks = [task_from_projection(tp) for tp in view.tasks.values()]

        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        terminal_ids = {t.id for t in all_tasks if t.status in _TERMINAL}
        resumable = [t for t in all_tasks if t.status not in _TERMINAL]

        if not resumable:
            raise RuntimeError(f"Session {session_id!r} has no resumable tasks")
        ...
        task_manager = TaskManager(session_id=session.id, event_bus=self._event_bus)
        task_manager.set_session(session)
        daemon_resumable = task_manager.restore(all_tasks, terminal_ids)
```

Add the HitlManager rebuild right after `all_tasks` is computed, and compute + pass `parked_task_ids`:

```python
        from loomex_core.core.control.converters import session_from_projection, task_from_projection
        session = session_from_projection(sess_proj)
        all_tasks = [task_from_projection(tp) for tp in view.tasks.values()]

        # 重建内存 HitlManager（_futures 空 → 后续应答自动走冷 resume；spec/07 §9）
        if view.pending_hitl:
            self.hitl_manager.rebuild_pending(view.pending_hitl)
        # 有未解决 pending HITL 的 task：restore 时保持 parked、不重排（spec/07 §9.1）
        parked_task_ids = {h.task_id for h in view.pending_hitl.values() if h.task_id}

        _TERMINAL = {"FINISHED", "FAILED", "CANCELED"}
        terminal_ids = {t.id for t in all_tasks if t.status in _TERMINAL}
        resumable = [t for t in all_tasks if t.status not in _TERMINAL]

        if not resumable:
            raise RuntimeError(f"Session {session_id!r} has no resumable tasks")
```

…and change the `restore` call:

```python
        daemon_resumable = task_manager.restore(all_tasks, terminal_ids, parked_task_ids=parked_task_ids)
```

- [ ] **Step 4: Run, verify PASS + recovery regression + runtime import**

Run:
```
cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_recovery.py tests/unit/test_snapshot_recovery.py -v && python -c "import loomex_core.core.runtime"
```
Expected: PASS; import exits 0.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_hitl_recovery.py
git commit -m "feat(hitl): recover_session rebuilds HitlManager + parks HITL tasks (spec/07 §9)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C1-3: `projection_updater` maintains `PAUSED_HITL`↔`RUNNING` + `get_session_status`

**Files:**
- Modify: `src/loomex_host/persistence/postgres/projection_updater.py:47-105,135-142`
- Modify: `src/loomex_host/persistence/postgres/state_store.py` (add `get_session_status`)
- Test: `src/loomex_host/tests/` (create `test_hitl_projection.py`) — see Step 1 for the location probe.

- [ ] **Step 1: Locate the host test dir + DB fixture; write the failing test**

First find how host persistence tests build a `ProjectionUpdater` with a sqlite/pg session factory:
```
cd "c:/Users/Xing/Documents/codes/LoomeX-00" && ls src/loomex_host/tests 2>/dev/null; grep -rl "ProjectionUpdater\|async_sessionmaker\|init_db" src/loomex_host/tests 2>/dev/null | head
```
If a host test harness exists (a fixture yielding an `async_sessionmaker` + a way to insert a session row), write `test_hitl_projection.py` mirroring it:

```python
async def test_session_paused_hitl_then_resume(projection_and_factory):
    proj, factory = projection_and_factory   # adapt to the existing fixture's shape
    # seed a RUNNING session row (via SessionCreated or direct insert, per the harness)
    await proj.on_event(_session_created_event("ses_1"))
    await proj.on_event(_event(EventType.SESSION_PAUSED_HITL, session_id="ses_1"))
    assert await _status(factory, "ses_1") == "PAUSED_HITL"
    await proj.on_event(_event(EventType.HITL_ANSWERED, session_id="ses_1", approval_id="h1"))
    assert await _status(factory, "ses_1") == "RUNNING"
```

If NO host test harness exists for ProjectionUpdater, STOP and report NEEDS_CONTEXT (the controller will decide whether to add a sqlite fixture or verify via the core-side projection contract). Do NOT invent a Postgres connection.

- [ ] **Step 2: Run, verify FAIL**

Run the host test (path per the harness). Expected: FAIL — no `SessionPausedHitl` handler.

- [ ] **Step 3: Implement the projection handlers**

In `projection_updater.py::_handle`, after the existing `SESSION_STATUS_CHANGED` / `SESSION_FINISHED` branches, add:

```python
        elif t == EventType.SESSION_PAUSED_HITL:
            await self._update_session(event.session_id, status="PAUSED_HITL")

        elif t in (
            EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
            EventType.HITL_REJECTED, EventType.HITL_TIMEOUT, EventType.HITL_CANCELLED,
        ):
            # HITL 解决 → session 回 RUNNING（仅当仍处暂停态，避免覆盖已到的终态）。
            await self._update_session_if_status(
                event.session_id, from_status="PAUSED_HITL", to_status="RUNNING",
            )
```

> NOTE for Phase C-2 (Task C2-4): once timeout = eviction (no longer resolves), REMOVE `EventType.HITL_TIMEOUT` from this tuple (a timeout must keep the session `PAUSED_HITL`). Flagged again there.

Add the conditional helper next to `_update_session`:

```python
    async def _update_session_if_status(self, session_id: str, from_status: str, to_status: str) -> None:
        async with self._factory() as db:
            async with db.begin():
                row = await db.get(SessionModel, session_id)
                if row is not None and row.status == from_status:
                    row.status = to_status
```

`PAUSED_HITL` is already in `_VALID_SESSION_STATUSES` (projection_updater.py:26) — no change needed there.

- [ ] **Step 4: Add `get_session_status` to the state store**

In `state_store.py`, in the "Mutation (limited)" / reads area, add:

```python
    async def get_session_status(self, session_id: str) -> str | None:
        """读回某 session 的持久状态（recover 据此跳过 PAUSED_HITL；spec/07 §9）。"""
        async with self._factory() as db:
            row = await db.get(SessionModel, session_id)
            return row.status if row is not None else None
```

- [ ] **Step 5: Run, verify PASS**

Run the host projection test + a host import smoke check:
```
cd "c:/Users/Xing/Documents/codes/LoomeX-00" && python -c "import loomex_host.persistence.postgres.projection_updater, loomex_host.persistence.postgres.state_store"
```
Expected: test PASS; import exits 0.

- [ ] **Step 6: Commit**

```bash
git add src/loomex_host/persistence/postgres/projection_updater.py src/loomex_host/persistence/postgres/state_store.py src/loomex_host/tests/test_hitl_projection.py
git commit -m "feat(host): projection maintains session PAUSED_HITL<->RUNNING + get_session_status (spec/07 §9.1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C1-4: `recover()` skips `PAUSED_HITL`; cold resolve triggers resume

**Files:**
- Modify: `src/loomex_host/api/startup.py:201-214` (`_on_session_interrupted`)
- Modify: `src/loomex_host/api/sessions.py:101-134` (`_submit_hitl_response`)
- Test: covered by C1-2 (core) + manual host reasoning; add a focused unit test for the cold-trigger branch if a host harness exists.

- [ ] **Step 1: Implement the recover skip**

`runtime.recover()` calls the host `_on_session_interrupted` callback for every session the event store reports active (a `PAUSED_HITL` session has no terminal event, so it's reported). The skip belongs in the callback, reading the persisted status maintained by C1-3. In `startup.py::_setup_recovery`, change `_on_session_interrupted`:

```python
    async def _on_session_interrupted(session_id: str) -> None:
        # PAUSED_HITL：跨重启保持可恢复，不误标 INTERRUPTED（spec/07 §9）。
        if state_store is not None:
            status = await state_store.get_session_status(session_id)
            if status == "PAUSED_HITL":
                return
        entry = _sessions.get(session_id)
        if entry is not None:
            entry.status = "INTERRUPTED"
            await entry._append_json(json.dumps({"type": "session_update", "status": "INTERRUPTED"}))
        if state_store is not None:
            await state_store.update_session_status(session_id, "INTERRUPTED")
```

- [ ] **Step 2: Implement the cold-resolve resume trigger**

In `api/sessions.py::_submit_hitl_response`, the current resolve block is:

```python
    req = pending[0]
    action, message = route_hitl_reply(req.kind, content)
    if action == "reject":
        await hitl.reject(req.id, message=message)
    elif action == "approve":
        await hitl.approve(req.id, message=message)
    else:
        await hitl.answer(req.id, message)
    return entry.to_dict()
```

Replace with the hot/cold-reporting wrappers + cold resume trigger:

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
        # 冷：活协程已驱逐 / 进程重启后重建 → 触发该 session resume（reconcile 续跑，spec/07 §6/§9）
        runtime = deps.get_runtime()
        if runtime is not None:
            await runtime.recover_session(entry.session_id)
    return entry.to_dict()
```

> In C-1 (no eviction yet), in-process answers are always hot (`was_hot=True`), so this branch only fires after a restart. C-2's eviction makes it fire in-process too.

- [ ] **Step 3: Run regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00" && python -c "import loomex_host.api.sessions, loomex_host.api.startup"` and any host endpoint tests that exist (`grep -rl "_submit_hitl_response\|answer_input\|send_message" src/loomex_host/tests` then run them).
Expected: imports exit 0; existing host tests PASS.

- [ ] **Step 4: Commit**

```bash
git add src/loomex_host/api/startup.py src/loomex_host/api/sessions.py
git commit -m "feat(hitl): recover skips PAUSED_HITL; cold resolve triggers recover_session (spec/07 §9)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C1-5: Phase C-1 gate — crash-mid-batch reconcile + full regression

**Files:**
- Test: `loomex-core/tests/unit/test_hitl_recovery.py`

- [ ] **Step 1: Add the "crash mid tool batch" recovery test (the free §9 win)**

Append to `test_hitl_recovery.py`. Seed a NON-HITL task whose last assistant turn has 2 tool_calls but only 1 `TOOL_RESULT` in memory; after `recover_session`, `_resolve` must pick `reconcile` (B8) and the second tool runs. Assert via the detection helper that the recovered task routes to reconcile:

```python
async def test_crash_mid_batch_routes_to_reconcile() -> None:
    from datetime import datetime, timezone, timedelta
    from loomex_core.core.runtime import _task_has_dangling_tool_call
    from loomex_core.protocols import MemoryEventType, MemoryScope, ProviderContext
    from loomex_core.protocols.memory import MemoryEvent
    from loomex_core.providers.memory_blackboard import InMemoryMemoryProvider

    base = datetime(2026, 6, 12, tzinfo=timezone.utc)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=MemoryEventType.LLM_RESPONSE, scope=sc, content="",
        timestamp=base + timedelta(seconds=1), role="assistant",
        metadata={"tool_calls": [{"id": "x1", "name": "web", "input": {}},
                                 {"id": "x2", "name": "web", "input": {}}]}), pctx)
    await mem.ingest(MemoryEvent(type=MemoryEventType.TOOL_RESULT, scope=sc, content="r1",
        timestamp=base + timedelta(seconds=2), role="tool", metadata={"tool_call_id": "x1"}), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is True   # x2 dangling → reconcile
```

- [ ] **Step 2: Run + full core gate**

Run:
```
cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest -p no:warnings -q
```
Expected: PASS (all). **Phase C-1 gate.** If host tests live elsewhere, run those too.

- [ ] **Step 3: Update specs**

In `docs/spec/07-hitl-suspend-resume.md`, update the status banner to note Phase C-1 (recovery: HitlManager rebuild, restore() split, PAUSED_HITL persistence, recover skip) is implemented. In `docs/spec/05-authz-and-hitl.md`, add the C-1 deltas (PAUSED_HITL survives restart; reconcile unifies crash recovery). Concise edits only.

- [ ] **Step 4: Commit**

```bash
git add loomex-core/tests/unit/test_hitl_recovery.py docs/spec/07-hitl-suspend-resume.md docs/spec/05-authz-and-hitl.md
git commit -m "test(recovery): crash-mid-batch reconcile + Phase C-1 gate (spec/07 §9)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

# PHASE C-2 / D — Park / eviction / approval cold (HIGHEST RISK + accepted semantic change)

> Accepted semantic change (D): timeout no longer fails; a crash mid-`provider.invoke` becomes deterministic at-least-once replay (reconcile re-runs the un-resulted tool). Only `TOOL_RESULT`-written tools are guaranteed not to re-run.

### Task C2-1: `HitlPark` signal + `AuthorizationDecision.defer`

**Files:**
- Create: `loomex-core/src/loomex_core/core/loop/park.py`
- Modify: `loomex-core/src/loomex_core/core/auth/authorizer.py:25-31`
- Test: `loomex-core/tests/unit/test_hitl_park.py` (create)

- [ ] **Step 1: Write the failing test**

Create `loomex-core/tests/unit/test_hitl_park.py`:

```python
"""HITL park 信号 + 热→冷驱逐 + approval 冷路径（spec/07 §5/§7/§8）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


def test_hitl_park_is_base_exception_not_exception() -> None:
    from loomex_core.core.loop.park import HitlPark
    assert issubclass(HitlPark, BaseException)
    assert not issubclass(HitlPark, Exception)


def test_hitl_park_carries_ids() -> None:
    from loomex_core.core.loop.park import HitlPark
    p = HitlPark(request_id="hit_1", tool_call_id="tc1")
    assert p.request_id == "hit_1" and p.tool_call_id == "tc1"


def test_authorization_decision_has_defer_default_false() -> None:
    from loomex_core.core.auth.authorizer import AuthorizationDecision
    assert AuthorizationDecision(allowed=True).defer is False
```

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py -v`
Expected: FAIL — module/attr missing.

- [ ] **Step 3: Implement**

Create `core/loop/park.py`:

```python
"""HitlPark：热→冷降级 / 显式挂起的专用信号（spec/07 §7）。

继承 BaseException（非 Exception）→ 穿过 CapabilityGateway 的 except Exception，不被当成
工具错误结果；一路上抛到 loop，由 _run_loop 显式捕获、落 task SUSPENDED（非 FAILED），
复用委派挂起返回路径。与真正的 interrupt（CancelledError）可区分。
"""

from __future__ import annotations


class HitlPark(BaseException):
    """携带挂起所需的最小信息。"""

    def __init__(self, request_id: str = "", tool_call_id: str = "") -> None:
        super().__init__(f"HITL park: request={request_id} tool_call={tool_call_id}")
        self.request_id = request_id
        self.tool_call_id = tool_call_id
```

In `authorizer.py`, add `defer` to `AuthorizationDecision`:

```python
@dataclass
class AuthorizationDecision:
    """一次授权的结构化结果。"""

    allowed: bool
    message: str = ""                              # 反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）
    modified_arguments: dict[str, Any] | None = None  # allow 时的有效参数（None = 用原参）
    defer: bool = False                            # spec/07 §7：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke）
```

- [ ] **Step 4: Run, verify PASS**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/park.py loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): HitlPark BaseException + AuthorizationDecision.defer (spec/07 §7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C2-2: Gateway honors `defer` → raises `HitlPark`; plumbs `tool_call_id` to `authorize()`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/capability_gateway.py:125-140`
- Modify: `loomex-core/src/loomex_core/core/auth/authorizer.py` (ALL `authorize` signatures)
- Modify: `loomex-core/tests/unit/test_authorizer.py:185,190` (the two inline authorizers)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write the failing test**

Append to `test_hitl_park.py` (mirror `test_authorizer.py`'s `_state_ctx`/`_gateway` helpers — import them or inline a minimal version):

```python
async def test_gateway_defer_raises_park_and_skips_provider() -> None:
    from types import SimpleNamespace
    from collections.abc import AsyncIterator
    from loomex_core.core.auth import AuthorizationDecision, Authorizer
    from loomex_core.core.events.bus import InProcessEventBus
    from loomex_core.core.loop.capability_gateway import CapabilityGateway
    from loomex_core.core.loop.driver import LoopContext, LoopState
    from loomex_core.core.loop.park import HitlPark
    from loomex_core.core.orchestrator.capability_cache import CapabilityCache
    from loomex_core.protocols import MemoryScope, ProviderContext
    from loomex_core.protocols.capability import (
        CapabilityEvent, CapabilityProviderInfo, ToolCapability, ToolCapabilityProvider)
    from loomex_core.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    cap = ToolCapability(id="test:echo", name="echo", description="e")

    class _Prov(ToolCapabilityProvider):
        name = "test"
        def __init__(self): self.invoked = False
        async def list(self, ctx): return [cap]
        async def retrieve(self, ctx): return [cap]
        async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)
        def invoke(self, cid, args, ctx) -> AsyncIterator[CapabilityEvent]: return self._run()
        async def _run(self):
            self.invoked = True
            yield CapabilityEvent(kind="result", payload={"content": "x"})
        async def cancel(self, iid, ctx): return None

    class _DeferAuth(Authorizer):
        async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id=""):
            return AuthorizationDecision(allowed=False, defer=True)

    prov = _Prov()
    cache = CapabilityCache(); cache.put("agt_1", [cap])
    gw = CapabilityGateway(capability_cache=cache, capability_providers=[prov],
                           memory=InMemoryMemoryProvider(), event_bus=InProcessEventBus(),
                           provider_authorizers={"test:echo": _DeferAuth()})
    agent = SimpleNamespace(id="agt_1", template_id="tmpl_a", session_id="s1")
    session = SimpleNamespace(id="s1", tenant_id="default")
    task = SimpleNamespace(id="tsk_1")
    scope = MemoryScope(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(run_id="r1", session=session, task=task, agent=agent, scope=scope)
    ctx = LoopContext(assembler=None, llm=None, memory=InMemoryMemoryProvider(),
                      event_bus=InProcessEventBus(),
                      provider_ctx=ProviderContext(session_id="s1", tenant_id="default",
                                                   task_id="tsk_1", agent_id="agt_1"))
    with pytest.raises(HitlPark):
        await gw.invoke("echo", {"text": "hi"}, state, ctx, tool_call_id="tcZ")
    assert prov.invoked is False
```

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py::test_gateway_defer_raises_park_and_skips_provider -v`
Expected: FAIL — no defer handling (returns an error result instead of raising).

- [ ] **Step 3: Thread `tool_call_id` through ALL authorize signatures**

In `authorizer.py`, update the abstract method and all three impls to accept a keyword-only `tool_call_id: str = ""`:

Abstract (`Authorizer.authorize`):
```python
    @abstractmethod
    async def authorize(
        self,
        capability: Capability,
        agent: Agent,
        task: Task | None,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "",
    ) -> AuthorizationDecision: ...
```

`AllowAllAuthorizer.authorize`:
```python
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True)
```

`AllowListAuthorizer.authorize` — add `*, tool_call_id=""` to the signature (body unchanged):
```python
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
```

`HumanConfirmationAuthorizer.authorize` — add `*, tool_call_id=""` (the cold short-circuit body is Task C2-5; for now just thread the param into `request(...)`):
```python
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        approval_id = await self.hitl_manager.request(
            kind="approval",
            session_id=agent.session_id,
            task_id=task.id if task else "",
            agent_id=agent.id,
            capability_id=capability.id,
            arguments=arguments or {},
            question=f"Allow tool '{capability.name}'?",
            context=capability.description,
            tool_call_id=tool_call_id,
        )
        approval = await self.hitl_manager.wait(approval_id)
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True, message=approval.message, modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (status=%s)", capability.id, approval.status)
        return AuthorizationDecision(allowed=False, message=approval.message)
```

In `test_authorizer.py`, update the two inline authorizers (lines 185, 190) to accept the new kwarg:
```python
class _ModifyAuthorizer(Authorizer):
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True, message="be careful", modified_arguments={"text": "override"})


class _DenyMsgAuthorizer(Authorizer):
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, message="not in this context")
```

- [ ] **Step 4: Gateway — pass `tool_call_id` + honor `defer`**

In `capability_gateway.py::invoke`, the current authorize call (line 127) is:
```python
        decision = await authorizer.authorize(cap, state.agent, state.task, ctx, arguments)
        if not decision.allowed:
```
Replace with:
```python
        decision = await authorizer.authorize(
            cap, state.agent, state.task, ctx, arguments, tool_call_id=tool_call_id,
        )
        if decision.defer:
            # 守住安全不变式：绝不调 provider.invoke；上抛 park 信号 → loop 落 SUSPENDED（spec/07 §7）。
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(tool_call_id=tool_call_id)
        if not decision.allowed:
```

> `HitlPark` is `BaseException` so it propagates straight out of `invoke` (the later `try/except Exception` around `provider.invoke` only catches `Exception`). Verify no broader `except BaseException` wraps it in the gateway (there is none).

- [ ] **Step 5: Run, verify PASS + authorizer regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py tests/unit/test_authorizer.py tests/unit/test_hitl.py -v`
Expected: PASS (all — the existing `test_authorizer.py` gateway tests still pass since `tool_call_id` defaults to `""`).

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/capability_gateway.py loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(gateway): defer -> raise HitlPark, never invoke; plumb tool_call_id to authorize (spec/07 §6/§7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C2-3: ActStep + `_run_loop` unwind `HitlPark` → task `SUSPENDED`

**Files:**
- Modify: `loomex-core/src/loomex_core/core/loop/steps/act.py:14-18,212-222`
- Modify: `loomex-core/src/loomex_core/core/runtime.py:977-993` (`_run_loop`)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write the failing test**

Append to `test_hitl_park.py`. Verify `_run_loop` catches `HitlPark` and returns normally with the task `SUSPENDED` (no FAILED). Drive a minimal `_run_loop` by raising `HitlPark` from a fake driver:

```python
async def test_run_loop_catches_park_returns_suspended() -> None:
    from types import SimpleNamespace
    from loomex_core.core.loop.park import HitlPark
    from loomex_core.core.events.bus import InProcessEventBus

    seen = []
    bus = InProcessEventBus()
    bus.subscribe(None, lambda ev: seen.append(ev.type))

    # Build a runtime instance just to call _run_loop; stub the driver to raise HitlPark.
    from loomex_core.core import LoomeXRuntime
    from loomex_core.providers.llm.mock import MockLLMAdapter, MockResponse
    from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template
    resolver = InMemoryTemplateResolver(); resolver.register(make_echo_template())
    rt = LoomeXRuntime(llm=MockLLMAdapter(responses=[MockResponse(text="x")]), template_resolver=resolver)
    rt._event_bus = bus

    class _ParkDriver:
        async def run(self, state, ctx):
            raise HitlPark(tool_call_id="tc1")
            yield  # make it an async generator

    task = SimpleNamespace(id="t1", status="ACTIVE", error=None, retry_count=0, max_retries=3)
    agent = SimpleNamespace(id="a1")
    # Minimal LoopState-ish: _run_loop reads state.sequence_counter, state.transcript, state.apply_patch
    state = SimpleNamespace(sequence_counter=0, transcript=[], session=SimpleNamespace(id="s1", tenant_id="default"),
                            task=task, agent=agent, apply_patch=lambda p: state)
    out = await rt._run_loop(state, ctx=SimpleNamespace(), driver=_ParkDriver(),
                             run_id="r1", initial_step="reason", task=task, agent=agent)
    assert task.status == "SUSPENDED"
    assert "TaskFailed" not in seen
```

> If `_run_loop`'s `finally` block touches attributes the `SimpleNamespace` state lacks (e.g. `make_event` needs more fields), adapt the stub minimally — but do NOT change production to accommodate the test. If the stub becomes unreasonable, instead assert the behavior through the real act-path test below and mark this one DONE_WITH_CONCERNS describing the harness limitation.

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py::test_run_loop_catches_park_returns_suspended -v`
Expected: FAIL — `HitlPark` propagates out of `_run_loop` (uncaught) instead of returning SUSPENDED.

- [ ] **Step 3: Implement `_run_loop` park handling**

In `runtime.py::_run_loop`, the current exception ladder is:
```python
        try:
            async for outcome in driver.run(state, loop_ctx):
                if outcome.state_patch:
                    state = state.apply_patch(outcome.state_patch)
        except asyncio.CancelledError:
            was_cancelled = True
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "CANCELED"
        except Exception as exc:
            run_error = exc
            ...
```
Insert a `HitlPark` handler BEFORE `except asyncio.CancelledError` (so it wins over `Exception`; order doesn't matter vs CancelledError since they're disjoint, but place it first for clarity):
```python
        try:
            async for outcome in driver.run(state, loop_ctx):
                if outcome.state_patch:
                    state = state.apply_patch(outcome.state_patch)
        except HitlPark:
            # 热→冷降级 / 显式挂起：干净挂起，不算失败。run_error 保持 None →
            # finally 发 RUN_FINISHED(final_status=SUSPENDED, will_retry=False)，
            # 与委派挂起同形；_run_task 据 task.status==SUSPENDED 走挂起分支（不 requeue）。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "SUSPENDED"
            logger.info("_run_loop: task %s parked on HITL", task.id)
        except asyncio.CancelledError:
            ...
```
Add the import at the top of `runtime.py` (near other loop imports): `from loomex_core.core.loop.park import HitlPark`.

- [ ] **Step 4: Implement ActStep park unwind**

In `act.py`, add the import (top, with the other `from loomex_core.core.loop...` imports):
```python
from loomex_core.core.loop.park import HitlPark
```
In the tool-batch loop (lines 212-222), wrap the invoke:
```python
            for tc in tool_calls:
                if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                    ctx.cancel_token.raise_if_cancelled()
                try:
                    result = await _invoke_tool(tc, state, ctx)
                except HitlPark:
                    # 被 park 的工具未执行；task 落 SUSPENDED 并 unwind（spec/07 §7）。
                    state.task.status = "SUSPENDED"
                    raise
                tool_results.append({
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "result": result.content,
                    "is_error": result.is_error,
                })
```

- [ ] **Step 5: Run, verify PASS + act/scheduling regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py tests/unit/test_task_scheduling.py tests/unit/test_delegation.py tests/integration/test_minimal_loop.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/loop/steps/act.py loomex-core/src/loomex_core/core/runtime.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(loop): HitlPark unwinds to task SUSPENDED via delegation path (spec/07 §7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C2-4: Timeout = hot→cold eviction (not failure) + single-authority race

**Files:**
- Modify: `loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py` (`__init__`, `wait`, `_resolve`, `resolve_*`)
- Modify: `loomex-core/src/loomex_core/core/control/reducers.py` (drop `HITL_TIMEOUT` from removal tuple)
- Modify: `src/loomex_host/persistence/postgres/projection_updater.py` (drop `HITL_TIMEOUT` from resolve tuple)
- Modify: `loomex-core/tests/unit/test_hitl.py` (rewrite timeout tests)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

- [ ] **Step 1: Write/replace the failing tests**

In `test_hitl_park.py`, add eviction + race tests:

```python
async def test_timeout_evicts_to_cold_keeps_pending() -> None:
    from loomex_core.core.orchestrator.hitl_manager import HitlManager
    from loomex_core.core.loop.park import HitlPark
    mgr = HitlManager(timeout_sec=0)
    rid = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tc1")
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    assert mgr.get(rid).status == "pending"        # 仍 pending（hot→cold，不是 timeout 终态）
    assert mgr.list_pending()                       # pending 保留
    # future 已驱逐 → 后续 answer 走冷
    resolved, was_hot = await mgr.resolve_answer(rid, "late answer")
    assert resolved.status == "accepted" and was_hot is False


async def test_answer_before_timeout_is_hot_and_wins() -> None:
    import asyncio
    from loomex_core.core.orchestrator.hitl_manager import HitlManager
    mgr = HitlManager(timeout_sec=None)             # never times out
    rid = await mgr.request(kind="input", session_id="s1", task_id="t1", tool_call_id="tc1")
    waiter = asyncio.create_task(mgr.wait(rid))
    await asyncio.sleep(0)
    resolved, was_hot = await mgr.resolve_answer(rid, "answered")
    assert was_hot is True
    assert (await waiter).status == "accepted"
```

In `test_hitl.py`, the existing timeout tests now describe OLD semantics and must change. Replace `test_wait_times_out_and_emits` and `test_resolve_is_idempotent_after_timeout` with new-semantics versions:

```python
async def test_wait_timeout_raises_park_and_keeps_pending() -> None:
    from loomex_core.core.loop.park import HitlPark
    mgr = HitlManager(timeout_sec=0)
    rid = await _request(mgr)
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    assert mgr.get(rid).status == "pending"


async def test_answer_after_timeout_resolves_cold() -> None:
    mgr = HitlManager(timeout_sec=0)
    rid = await _request(mgr, kind="input")
    from loomex_core.core.loop.park import HitlPark
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    resolved, was_hot = await mgr.resolve_answer(rid, "late")
    assert resolved.status == "accepted" and was_hot is False
```

(Delete the old two tests. The `HitlTimeout` event is no longer emitted.)

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py tests/unit/test_hitl.py -k "timeout or evict or hot_and_wins or cold" -v`
Expected: FAIL.

- [ ] **Step 3: Implement HitlManager eviction + lock + (req, was_hot)**

In `hitl_manager.py`:

(a) `__init__` — add a lock:
```python
        self._requests: dict[str, HitlRequest] = {}
        self._futures: dict[str, asyncio.Future[HitlRequest]] = {}
        self._lock = asyncio.Lock()
```

(b) Rework `wait()` — on timeout, evict the future, keep `pending`, raise `HitlPark`:
```python
    async def wait(self, request_id: str) -> HitlRequest:
        """阻塞至应答。timeout_sec=None（默认）则永不超时。

        显式正整数 timeout_sec 超时 → 热→冷驱逐：移除 future、保留 pending、抛 HitlPark
        （spec/07 §3/§7）。answer 先到（race）则正常返回已解决请求。未知 id 抛 KeyError。
        """
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
                    return req                       # answer 先到：走热已解决
                self._futures.pop(request_id, None)  # 驱逐 future，保留 pending
            from loomex_core.core.loop.park import HitlPark
            raise HitlPark(request_id=request_id, tool_call_id=req.tool_call_id)
```

(c) Make `_resolve` return `(req, was_hot)` under the lock; update `approve/answer/reject/cancel` + `resolve_*` accordingly. Replace the B5 `_is_hot`-pre-snapshot approach. The new `_resolve`:
```python
    async def _resolve(
        self,
        req: HitlRequest,
        status: HitlStatus,
        event_type: EventType,
    ) -> tuple[HitlRequest, bool]:
        async with self._lock:
            if req.status != "pending":
                return req, False                    # 已解决（含驱逐后）→ 幂等
            req.status = status
            req.resolved_at = now_utc()
            future = self._futures.get(req.id)
            was_hot = future is not None and not future.done()
            if was_hot:
                future.set_result(req)
        await self._emit(event_type, req, payload={"approval_id": req.id})
        return req, was_hot
```
Update the public resolvers to unpack and keep their existing return type (just `HitlRequest`):
```python
    async def approve(self, request_id, *, message="", modified_arguments=None) -> HitlRequest:
        req = self._require(request_id)
        req.message = message
        req.modified_arguments = modified_arguments
        evt = EventType.HITL_MODIFIED if modified_arguments is not None else EventType.HITL_APPROVED
        result, _ = await self._resolve(req, "accepted", evt)
        return result

    async def answer(self, request_id, text) -> HitlRequest:
        req = self._require(request_id)
        req.message = text
        result, _ = await self._resolve(req, "accepted", EventType.HITL_ANSWERED)
        return result

    async def reject(self, request_id, *, message="") -> HitlRequest:
        req = self._require(request_id)
        req.message = message
        result, _ = await self._resolve(req, "rejected", EventType.HITL_REJECTED)
        return result

    async def cancel(self, request_id, *, message="") -> HitlRequest:
        req = self._require(request_id)
        req.message = message
        result, _ = await self._resolve(req, "cancelled", EventType.HITL_CANCELLED)
        return result
```
Rewrite the `resolve_*` wrappers (replacing B5's pre-snapshot `_is_hot`) to thread `was_hot` from `_resolve`:
```python
    async def resolve_answer(self, request_id, text) -> tuple[HitlRequest, bool]:
        req = self._require(request_id); req.message = text
        return await self._resolve(req, "accepted", EventType.HITL_ANSWERED)

    async def resolve_approve(self, request_id, *, message="", modified_arguments=None) -> tuple[HitlRequest, bool]:
        req = self._require(request_id); req.message = message; req.modified_arguments = modified_arguments
        evt = EventType.HITL_MODIFIED if modified_arguments is not None else EventType.HITL_APPROVED
        return await self._resolve(req, "accepted", evt)

    async def resolve_reject(self, request_id, *, message="") -> tuple[HitlRequest, bool]:
        req = self._require(request_id); req.message = message
        return await self._resolve(req, "rejected", EventType.HITL_REJECTED)
```
DELETE the now-unused `_is_hot` method.

> Note: `approve`/`answer`/`resolve_*` now both set `req.message` before `_resolve`. Make sure not to double-resolve. Each public method sets fields then calls `_resolve` exactly once.

(d) Drop the timeout terminal: remove the old `wait()` timeout branch that set `status="timeout"` + emitted `HITL_TIMEOUT` (replaced above). `HITL_TIMEOUT` enum stays for back-compat but is no longer emitted.

- [ ] **Step 4: Stop folding HITL_TIMEOUT as a resolution**

In `reducers.py` `_apply`, remove `EventType.HITL_TIMEOUT` from the `pending_hitl` removal tuple (added in B4) — a timeout must NOT clear pending now:
```python
    elif t in (
        EventType.HITL_APPROVED, EventType.HITL_MODIFIED, EventType.HITL_ANSWERED,
        EventType.HITL_REJECTED, EventType.HITL_CANCELLED,
    ):
        view.pending_hitl.pop(p.get("approval_id", ""), None)
```
In `projection_updater.py`, remove `EventType.HITL_TIMEOUT` from the resolve tuple (added in C1-3) for the same reason.

- [ ] **Step 5: Run, verify PASS + reducer/hitl regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl.py tests/unit/test_hitl_park.py tests/unit/test_hitl_reconcile.py tests/unit/test_hitl_recovery.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add loomex-core/src/loomex_core/core/orchestrator/hitl_manager.py loomex-core/src/loomex_core/core/control/reducers.py src/loomex_host/persistence/postgres/projection_updater.py loomex-core/tests/unit/test_hitl.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): timeout = hot->cold eviction (not failure) + single-authority race (spec/07 §3/§8)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C2-5: approval-kind cold path via resolved-decision cache

**Files:**
- Modify: `loomex-core/src/loomex_core/core/auth/authorizer.py` (`HumanConfirmationAuthorizer.authorize`)
- Test: `loomex-core/tests/unit/test_hitl_park.py`

On reconcile re-invoke, `authorize` must reuse the already-resolved HITL (keyed by `tool_call_id`) instead of opening/awaiting a new one. If still pending (restart-rebuilt), `request()` idempotency re-arms a future and `wait()` re-parks (or raises `HitlPark` on eviction).

- [ ] **Step 1: Write the failing test**

Append to `test_hitl_park.py`:

```python
async def test_authorize_cold_uses_resolved_decision_no_new_hitl() -> None:
    from types import SimpleNamespace
    from loomex_core.core.auth import HumanConfirmationAuthorizer
    from loomex_core.core.orchestrator.hitl_manager import HitlManager
    from loomex_core.protocols import ProviderContext
    from loomex_core.protocols.capability import ToolCapability

    mgr = HitlManager()
    rid = await mgr.request(kind="approval", session_id="s1", task_id="t1", tool_call_id="tcZ")
    await mgr.approve(rid, modified_arguments={"command": "ls -la"})

    authz = HumanConfirmationAuthorizer(hitl_manager=mgr)
    cap = ToolCapability(id="fs:bash_exec", name="bash_exec", description="run")
    agent = SimpleNamespace(id="a1", template_id="t", session_id="s1")
    d = await authz.authorize(cap, agent, SimpleNamespace(id="t1"),
                              ProviderContext(session_id="s1", tenant_id="default"),
                              {"command": "ls"}, tool_call_id="tcZ")
    assert d.allowed and d.modified_arguments == {"command": "ls -la"}
    assert len(mgr.list_pending()) == 0     # 未新建
```

- [ ] **Step 2: Run, verify FAIL**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py::test_authorize_cold_uses_resolved_decision_no_new_hitl -v`
Expected: FAIL — `authorize` opens a new request and `wait()`s (or blocks).

- [ ] **Step 3: Implement the cold short-circuit**

Rewrite `HumanConfirmationAuthorizer.authorize` (building on C2-2's `tool_call_id` param):
```python
    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        existing = self.hitl_manager.find_for_tool_call(tool_call_id)
        if existing is not None and existing.status != "pending":
            approval = existing                       # 决定缓存命中（cold reconcile，spec/07 §6）
        else:
            approval_id = await self.hitl_manager.request(
                kind="approval", session_id=agent.session_id,
                task_id=task.id if task else "", agent_id=agent.id,
                capability_id=capability.id, arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?", context=capability.description,
                tool_call_id=tool_call_id,
            )
            approval = await self.hitl_manager.wait(approval_id)   # may raise HitlPark on eviction
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True, message=approval.message, modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (status=%s)", capability.id, approval.status)
        return AuthorizationDecision(allowed=False, message=approval.message)
```

> `wait()` raising `HitlPark` (BaseException) propagates through `authorize` → gateway (which has NOT invoked the provider — authorize runs before execution) → `_run_loop` → SUSPENDED. exactly-once invariant holds (the gated tool never ran).

- [ ] **Step 4: Run, verify PASS + authorizer/hitl regression**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest tests/unit/test_hitl_park.py tests/unit/test_authorizer.py tests/unit/test_hitl.py -v`
Expected: PASS (the existing `test_authorize_approve_with_modify_flows_through` / `test_authorizer_approve_passes` etc. still pass — their `tool_call_id` is empty so `find_for_tool_call` returns None → normal request+wait).

- [ ] **Step 5: Commit**

```bash
git add loomex-core/src/loomex_core/core/auth/authorizer.py loomex-core/tests/unit/test_hitl_park.py
git commit -m "feat(hitl): approval-kind cold path via resolved-decision cache (spec/07 §5/§6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task C2-6: Phase C-2 gate — full regression + spec finalize

- [ ] **Step 1: Full suites**

Run: `cd "c:/Users/Xing/Documents/codes/LoomeX-00/loomex-core" && python -m pytest -p no:warnings -q` and the host suite.
Expected: PASS (all). **Phase C-2 gate.**

- [ ] **Step 2: Finalize specs**

Rewrite the HITL lifecycle section of `docs/spec/05-authz-and-hitl.md` per spec/07 §13: delete timeout-failure semantics / `HitlTimeout` terminal; document the hot/cold model, park signal, eviction, single-authority race, approval cold path. Flip `docs/spec/07-hitl-suspend-resume.md` banner to fully implemented.

- [ ] **Step 3: Commit**

```bash
git add docs/spec/05-authz-and-hitl.md docs/spec/07-hitl-suspend-resume.md
git commit -m "docs(spec): finalize HITL hot/cold model in 05/07 (spec/07 phase C-2)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage (remaining items after Phase B):**
- §3 timeout = downgrade, `cancelled` terminal → C2-4 (eviction), B2 (cancel, done). ✓
- §5 approval hot/cold → C2-5. ✓
- §7 park signal pipeline (crosses gateway except, lands SUSPENDED not FAILED) → C2-1, C2-2, C2-3. ✓
- §8 eviction-vs-answer single authority → C2-4 (lock in `wait`/`_resolve`). ✓
- §9 recover rebuild + restore parked + recover skip → C1-1, C1-2, C1-4. ✓
- §9.1 projection PAUSED_HITL↔RUNNING + persist → C1-3. ✓
- §9 crash-mid-batch reconcile接驳 → C1-5. ✓
- §10 host cold-resolve trigger → C1-4. ✓
- §13 spec deltas → C1-5, C2-6 doc tasks. ✓

**2. Placeholder scan:** Two tests carry intentional adapt-to-harness notes: C1-3 (host projection fixture — explicit STOP/NEEDS_CONTEXT instruction if no host harness exists, rather than inventing a DB) and C2-3 (`_run_loop` `SimpleNamespace` stub — explicit fallback to assert via the real act-path + DONE_WITH_CONCERNS if the stub gets unreasonable). These are honest harness-dependency callouts, not lazy placeholders; every production-code step has complete code. No `TBD`/`implement later`/"add error handling" remain.

**3. Type consistency:**
- `restore(all_tasks, terminal_ids, parked_task_ids=None)` — C1-1 def matches C1-2 caller. ✓
- `recover_session` uses `self.hitl_manager.rebuild_pending(view.pending_hitl)` and `view.pending_hitl[*].task_id` — matches `HitlRequestView` (Phase B) + `rebuild_pending` (Phase B). ✓
- `resolve_answer/approve/reject` return `(HitlRequest, bool)` — C1-4 host unpacks `(_resolved, was_hot)`; C2-4 redefines them to thread `was_hot` from `_resolve` (replacing B5's `_is_hot`); `_resolve` now returns `(req, was_hot)` and all four public resolvers (`approve/answer/reject/cancel`) unpack `result, _`. Consistent. ✓
- `authorize(..., *, tool_call_id="")` — abstract + 4 impls (AllowAll/AllowList/HumanConfirmation in authorizer.py) + 2 inline test authorizers + gateway call site, all updated in C2-2; C2-5 builds on the same signature. ✓
- `HitlPark(request_id="", tool_call_id="")` — raised in gateway (C2-2, `tool_call_id` only), `wait` (C2-4, both), caught in act.py + `_run_loop` (C2-3). ✓
- `get_session_status` — added in C1-3, consumed in C1-4 host callback. ✓

**Open risk re-flagged for executor:** C2-4 single-authority correctness depends on `was_hot` being decided INSIDE the locked `_resolve` (done — `_resolve` returns it; `resolve_*` no longer pre-snapshot). Verify with `test_answer_before_timeout_is_hot_and_wins` + `test_timeout_evicts_to_cold_keeps_pending` before the C-2 gate. Also: C1-3 needs a host DB test fixture to exist; if it doesn't, that's a genuine blocker to surface, not to paper over.
