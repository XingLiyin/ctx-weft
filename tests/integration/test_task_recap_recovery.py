"""Crash-recovery of a session stuck in the finish-boundary background recap.

The reported bug: a session whose root task finished (``TASK_FINISHED`` persisted)
but whose finish-boundary background observe had NOT yet completed gets stuck —
``_fire_session_done`` was blocked awaiting that observe when the crash happened, so
``SESSION_FINISHED`` / terminal status never persisted (the session projection stays
RUNNING). On ``/resume`` the old ``_recover_session_locked`` found no resumable tasks
(the only task is terminal) and raised ``RuntimeError("has no resumable tasks")`` →
the resume click hung.

Test A reproduces that stuck state and asserts recover_session re-runs the pending
recap and finalizes the session to SUCCEEDED. Test B guards the genuinely-empty
projection: a session with no tasks at all must still raise RuntimeError.

Task 16 note: ``SessionFinished`` itself (and the SessionRegistry state machine that
used to emit it) is retired — the SM was demoted to a plain agent registry in Task 15.
The tests below now pin ``TaskQueueDrained`` instead: it's the real, still-emitted
TaskManager aggregate signal that ``SessionFinished`` used to be translated from, and
carries the same ``final_status``. They no longer assert on the event-store
projection's ``session_status`` reaching a terminal value, because nothing emits
``SessionFinished`` any more to drive that reducer branch — that's an accepted
consequence of session-state ownership moving to the host (docs/events-v2.md §2.1.1),
not a regression this file should guard against.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.reducers import rebuild_view
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.orchestrator.control_capability import BACKGROUND_PROCESS_REPORT_NAME
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryAddress, ProviderContext, ToolCall,
)
from ctx_weft.protocols.capability import qualify
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _make_runtime() -> tuple[CtxWeftRuntime, InMemoryMemoryProvider, list]:
    """Build a runtime with the echo template + in-memory memory, spying on emits.

    The mock LLM returns a fixed recap for the relaunched background observe so the
    re-run completes deterministically (plain-text fallback across react rounds).
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    # Enough plain-text responses to outlast the observe ReAct loop
    # (max_turns_per_observe defaults to 5) without exhausting the mock.
    llm = MockLLMAdapter(responses=[MockResponse(text="recovered recap") for _ in range(8)])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    mem = InMemoryMemoryProvider()
    runtime.providers.register_memory(mem)

    seen: list = []
    orig_emit = runtime._event_bus.emit

    async def _spy(ev):
        seen.append(ev)
        return await orig_emit(ev)

    runtime._event_bus.emit = _spy
    return runtime, mem, seen


def _ev(seq: int, type_, **payload) -> Event:
    # task_id populates both the Event field (used by the status reducers) and stays
    # available; the recap fold specifically reads it from the *payload* (mirroring how
    # background_observe emits TASK_RECAP_STARTED), so callers that need it there keep it.
    task_id = payload.get("task_id")
    if type_ not in (EventType.TASK_RECAP_STARTED, EventType.TASK_RECAP_DONE):
        task_id = payload.pop("task_id", None)
    return Event(
        id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="ses_recap",
        type=type_, timestamp=_TS, task_id=task_id, payload=payload,
    )


async def test_stuck_finish_session_recovers_and_finalizes() -> None:
    """root task FINISHED + TaskRecapStarted (no Done) + session projection RUNNING
    (no SESSION_FINISHED) → recover_session re-runs the recap and finalizes SUCCEEDED."""
    runtime, mem, seen = _make_runtime()
    sid, tid, aid = "ses_recap", "tsk_recap", "agt_root"

    seed = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="do it",
            template_id="agent:tpl_echo", root_agent_id=aid),
        _ev(2, EventType.RUN_STARTED),
        _ev(3, EventType.TASK_CREATED, task={
            "id": tid, "status": "ACTIVE", "title": "T", "kind": "reasoning",
            "assigned_agent_id": aid, "creator_agent_id": aid}),
        _ev(4, EventType.TASK_STARTED, task_id=tid, assigned_agent_id=aid),
        _ev(5, EventType.TASK_FINISHED, task_id=tid,
            outcome="success", summary="done", outputs={"result": "ok"}),
        # TaskFinalized 的真实发射侧只带 {task_id, outcome}（finalize.py:766）——
        # outputs 已由上面的 TASK_FINISHED 折进投影，这里不再伪造它。
        _ev(6, EventType.TASK_FINALIZED, task_id=tid, outcome="success"),
        # Crash mid finish-boundary observe: STARTED persisted, DONE never was.
        _ev(7, EventType.TASK_RECAP_STARTED, task_id=tid, boundary="finish", agent_id=aid),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    # Pre-crash state: session projection is still RUNNING (no SESSION_FINISHED).
    view_before = await rebuild_view(runtime.event_store, sid)
    assert view_before.sessions[sid].status == "RUNNING"
    assert view_before.tasks[tid].outputs == {"result": "ok"}

    # Seed the placeholder finish pair in memory so _relaunch's close-synth path can
    # locate the finish_task tool_call and supersede the placeholder Process Report.
    scope = MemoryAddress(session_id=sid, task_id=tid, agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id=tid, agent_id=aid)
    fin_tcid = "tc_finish"
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=scope,
        content="Process Report: (placeholder)", timestamp=_TS, role="assistant",
        metadata={"origin_task_id": tid, "tool_calls": [
            {"id": fin_tcid, "name": qualify("control:finish_task"), "input": {}}]},
    ), pctx)
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=scope,
        content="(placeholder finish result)", timestamp=_TS, role="tool",
        metadata={"origin_task_id": tid, "tool_call_id": fin_tcid},
    ), pctx)

    # ── Recover ────────────────────────────────────────────────────────────────
    await runtime.recover_session(sid)

    # finalize_idle_session awaits the relaunched recap before reporting the queue
    # drained, so it should already be persisted; drain any stragglers defensively.
    tm = runtime._task_managers.get(sid)
    if tm is not None and tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks), return_exceptions=True)

    # TaskQueueDrained(SUCCEEDED) was emitted — this is the TM aggregate signal that
    # used to feed SessionRegistry's now-retired SessionFinished translation (Task 16:
    # the session-state-machine SM was demoted to an agent registry and no longer
    # consumes/emits it). It's still the real, live "the session's work is done, and
    # it finished cleanly" signal, so it's what this test pins instead.
    drained = [
        e for e in seen
        if getattr(e, "type", None) == EventType.TASK_QUEUE_DRAINED
        and (e.payload or {}).get("final_status") == "SUCCEEDED"
    ]
    assert drained, (
        "expected TaskQueueDrained(SUCCEEDED) after recovery; got "
        f"{[(getattr(e, 'type', None), (e.payload or {}).get('final_status')) for e in seen]}"
    )

    # NOTE: the event-store *projection*'s session_status can no longer reach a
    # terminal value here — nothing emits SessionFinished any more (Task 15/16), and
    # reducers.py's session_status write only happens on that event. This is an
    # accepted consequence of the ownership move (per docs/events-v2.md §2.1.1, the
    # host now aggregates "is the session busy" itself rather than core broadcasting
    # a precomputed verdict); it is not something this test should assert on any more.

    # The pending recap was closed out (TASK_RECAP_DONE emitted by the re-run's finally).
    assert any(
        getattr(e, "type", None) == EventType.TASK_RECAP_DONE
        and (e.payload or {}).get("task_id") == tid
        for e in seen
    ), "expected TASK_RECAP_DONE from the relaunched recap"


async def test_stuck_failed_session_recovers_and_finalizes_failed() -> None:
    """Variant of Test A seeded with TASK_FAILED (+ FAILURE_THRESHOLD_HIT) instead of
    TASK_FINISHED: finalize_idle_session's FAILED branch (``session.failure_counter > 0``
    in ``_recover_session_locked``) must report TaskQueueDrained(FAILED), not SUCCEEDED.

    ``session.failure_counter`` is only incremented by the FAILURE_THRESHOLD_HIT reducer
    rule (``TASK_FAILED`` alone does not touch it — see
    ``TaskManager.on_task_finished``/``reducers._apply``), so both events are seeded to
    reach the same failure_counter > 0 state a real run would reach when a task's
    retries are exhausted and the session-level failure threshold trips.
    """
    runtime, mem, seen = _make_runtime()
    sid, tid, aid = "ses_recap", "tsk_recap", "agt_root"

    seed = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="do it",
            template_id="agent:tpl_echo", root_agent_id=aid),
        _ev(2, EventType.RUN_STARTED),
        _ev(3, EventType.TASK_CREATED, task={
            "id": tid, "status": "ACTIVE", "title": "T", "kind": "reasoning",
            "assigned_agent_id": aid, "creator_agent_id": aid}),
        _ev(4, EventType.TASK_STARTED, task_id=tid, assigned_agent_id=aid),
        _ev(5, EventType.TASK_FAILED, task_id=tid,
            error_code="TASK_FAILED_AT_RUN", error_message="boom", retry_count=3),
        _ev(6, EventType.FAILURE_THRESHOLD_HIT, failure_counter=1, threshold=1),
        # TaskFinalized 的真实发射侧只带 {task_id, outcome}（finalize.py:766）——
        # error 已由上面的 TASK_FAILED 折进投影，这里不再伪造它。
        _ev(7, EventType.TASK_FINALIZED, task_id=tid, outcome="fail"),
        # Crash mid finish-boundary observe: STARTED persisted, DONE never was.
        _ev(8, EventType.TASK_RECAP_STARTED, task_id=tid, boundary="finish", agent_id=aid),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    # Pre-crash state: task FAILED, session projection failure_counter > 0, still RUNNING.
    view_before = await rebuild_view(runtime.event_store, sid)
    assert view_before.tasks[tid].status == "FAILED"
    assert view_before.tasks[tid].error == "boom"
    assert view_before.sessions[sid].failure_counter > 0
    assert view_before.sessions[sid].status == "RUNNING"

    # Seed the placeholder finish pair in memory so _relaunch's close-synth path can
    # locate the finish_task tool_call (register_close_synth infers outcome="fail"
    # from task.status == "FAILED" — see runtime._relaunch_task_recap).
    scope = MemoryAddress(session_id=sid, task_id=tid, agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id=tid, agent_id=aid)
    fin_tcid = "tc_finish_fail"
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=scope,
        content="Process Report: (placeholder)", timestamp=_TS, role="assistant",
        metadata={"origin_task_id": tid, "tool_calls": [
            {"id": fin_tcid, "name": qualify("control:finish_task"), "input": {}}]},
    ), pctx)
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=scope,
        content="(placeholder finish result)", timestamp=_TS, role="tool",
        metadata={"origin_task_id": tid, "tool_call_id": fin_tcid},
    ), pctx)

    # ── Recover ────────────────────────────────────────────────────────────────
    await runtime.recover_session(sid)

    tm = runtime._task_managers.get(sid)
    if tm is not None and tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks), return_exceptions=True)

    # TaskQueueDrained(FAILED) was emitted — not SUCCEEDED (Test A's happy path). The
    # TM aggregate signal replaces the now-retired SessionFinished translation (Task 16
    # demoted SM; see the identically-worded note in test_stuck_finish_session_recovers_
    # and_finalizes just above for why the projection's session_status is no longer
    # asserted here).
    drained = [
        e for e in seen
        if getattr(e, "type", None) == EventType.TASK_QUEUE_DRAINED
        and (e.payload or {}).get("final_status") == "FAILED"
    ]
    assert drained, (
        "expected TaskQueueDrained(FAILED) after recovery of a failed session; got "
        f"{[(getattr(e, 'type', None), (e.payload or {}).get('final_status')) for e in seen]}"
    )

    # The pending finish-boundary recap was still closed out despite the failure.
    assert any(
        getattr(e, "type", None) == EventType.TASK_RECAP_DONE
        and (e.payload or {}).get("task_id") == tid
        for e in seen
    ), "expected TASK_RECAP_DONE from the relaunched recap"


class _RoutingLLM(MockLLMAdapter):
    """Routes each ``complete()`` call by request shape into one of two independent
    FIFO queues, instead of sharing a single index (the base ``MockLLMAdapter``).

    Recovery of a SUSPENDED task with a pending recap spins up two *concurrent*
    coroutines: the resumed task's own act loop, and the relaunched background-observe
    ReAct loop. A single shared response queue would race between them (whichever
    coroutine's LLM call lands first "steals" the next response, regardless of which
    queue it was meant for) — flaky by construction. Routing on the presence of the
    recap-report tool (only bound to background-observe requests) keeps each
    coroutine's responses deterministic regardless of scheduling order.
    """

    def __init__(self, *, responses: list[MockResponse], recap_responses: list[MockResponse], **kw):
        super().__init__(responses=responses, **kw)
        self._recap_responses = list(recap_responses)
        self._recap_idx = 0

    @staticmethod
    def _is_background_observe(request) -> bool:
        tools = getattr(request, "tools", None) or []
        return any(getattr(t, "name", "") == BACKGROUND_PROCESS_REPORT_NAME for t in tools)

    def complete(self, request, stream: bool = True):
        self.last_request = request
        if self._is_background_observe(request):
            if self._recap_idx >= len(self._recap_responses):
                raise RuntimeError(
                    f"_RoutingLLM: recap responses exhausted after {self._recap_idx} calls"
                )
            resp = self._recap_responses[self._recap_idx]
            self._recap_idx += 1
            return self._stream(resp, request)
        return super().complete(request, stream=stream)


async def test_suspended_task_with_pending_interrupt_recap_recovers() -> None:
    """Spec §7: a SUSPENDED (resumable) task with a pending interrupt-boundary recap.

    Simulates a crash where the root task was interrupted mid-act (e.g. an LLM outage,
    TaskSuspended persisted) while a background observe segment recap was also
    mid-flight (TaskRecapStarted boundary="interrupt", no Done). recover_session must
    do BOTH: re-queue/re-dispatch the SUSPENDED task (TaskManager.restore) AND
    re-launch the pending recap (_relaunch_task_recap) — and the re-fold guard in
    background_observe._run_background_observe must let it fold exactly once (no
    duplicate TaskRecapDone / no hang). The resumed task then runs to completion
    (its own normal-boundary close triggers a *second*, unrelated recap — expected,
    accepted best-effort concurrency per spec §5.1/§3.6), and the session reaches a
    terminal status.
    """
    runtime, mem, seen = _make_runtime()
    sid, tid, aid = "ses_recap", "tsk_recap", "agt_root"

    seed = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="do it",
            template_id="agent:tpl_echo", root_agent_id=aid),
        _ev(2, EventType.RUN_STARTED),
        _ev(3, EventType.TASK_CREATED, task={
            "id": tid, "status": "ACTIVE", "title": "T", "kind": "reasoning",
            "assigned_agent_id": aid, "creator_agent_id": aid}),
        _ev(4, EventType.TASK_STARTED, task_id=tid, assigned_agent_id=aid),
        # Crash mid interrupt-boundary observe while the task itself is suspended
        # (e.g. LLM outage during act, spec/07 outage path): STARTED persisted, DONE never was.
        _ev(5, EventType.TASK_SUSPENDED, task_id=tid, summary="outage"),
        _ev(6, EventType.TASK_RECAP_STARTED, task_id=tid, boundary="interrupt", agent_id=aid),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    # Route the routing-aware mock LLM in for this test (the module-level _make_runtime
    # builds a plain MockLLMAdapter; swap it for one that separates the two concurrent
    # LLM consumers deterministically — see _RoutingLLM docstring).
    runtime.llm = _RoutingLLM(
        responses=[MockResponse(text="resumed and done")],
        recap_responses=[
            MockResponse(tool_calls=[
                ToolCall(id=f"tc_recap_{i}", name=BACKGROUND_PROCESS_REPORT_NAME,
                          arguments={"act_recap": "recovered recap"}),
            ])
            for i in range(4)
        ],
    )

    view_before = await rebuild_view(runtime.event_store, sid)
    assert view_before.tasks[tid].status == "SUSPENDED"
    assert view_before.sessions[sid].status == "RUNNING"

    # Seed one un-folded raw LLM_RESPONSE so the interrupt-boundary recap's re-fold
    # guard sees active raw and takes the real fold path (rather than the "already
    # folded, nothing to do" no-op skip — see background_observe._run_background_observe).
    scope = MemoryAddress(session_id=sid, task_id=tid, agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id=tid, agent_id=aid)
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.LLM_RESPONSE, address=scope, content="(raw turn, unfolded)",
        timestamp=_TS, role="assistant", metadata={"tool_calls": []},
    ), pctx)

    # ── Recover ────────────────────────────────────────────────────────────────
    await runtime.recover_session(sid)

    # Drive to quiescence: repeatedly gather whatever background tasks the TM is
    # tracking (the relaunched recap, plus any recap the resumed task's own close
    # spawns) until the resumed task itself reaches a terminal status. Task 16 retired
    # SessionFinished (SessionRegistry's state machine was demoted to an agent registry
    # in Task 15), and with it every live path that could ever move the event-store
    # projection's session_status off RUNNING — polling on the task's own status is
    # the still-real completion signal for this loop.
    tm = runtime._task_managers.get(sid)
    for _ in range(100):
        if tm is not None and tm._background_asyncio_tasks:
            await asyncio.gather(*list(tm._background_asyncio_tasks), return_exceptions=True)
        view = await rebuild_view(runtime.event_store, sid)
        if view.tasks[tid].status in ("FINISHED", "FAILED", "CANCELED"):
            break
        await asyncio.sleep(0.02)

    # The SUSPENDED task was re-dispatched: a fresh TASK_STARTED was emitted for it
    # (recovery didn't just leave it parked) and it ran through to FINISHED.
    started_events = [
        e for e in seen
        if getattr(e, "type", None) == EventType.TASK_STARTED and e.task_id == tid
    ]
    assert started_events, "expected a re-dispatch TASK_STARTED for the resumed SUSPENDED task"
    view = await rebuild_view(runtime.event_store, sid)
    assert view.tasks[tid].status == "FINISHED", (
        f"expected resumed task to reach FINISHED, got {view.tasks[tid].status!r}"
    )

    # The interrupt-boundary recap pending at crash time was re-folded exactly once:
    # a TASK_RECAP_DONE closes it out — no duplicate fold, no hang.
    recap_done = [
        e for e in seen
        if getattr(e, "type", None) == EventType.TASK_RECAP_DONE
        and (e.payload or {}).get("task_id") == tid
    ]
    assert recap_done, "expected TASK_RECAP_DONE from the relaunched interrupt-boundary recap"

    # Recovery composed both threads to completion — TM reports the queue drained
    # with a terminal final_status (the aggregate signal that used to feed the now-
    # retired SessionFinished translation; see the note above the polling loop).
    drained = [
        e for e in seen
        if getattr(e, "type", None) == EventType.TASK_QUEUE_DRAINED
        and (e.payload or {}).get("final_status") in ("SUCCEEDED", "FAILED")
    ]
    assert drained, (
        f"expected a terminal TaskQueueDrained after recovery, got "
        f"{[(getattr(e, 'type', None), (e.payload or {}).get('final_status')) for e in seen]}"
    )


async def test_no_tasks_at_all_still_raises() -> None:
    """A session with no tasks at all is a genuinely empty/broken projection and must
    still raise RuntimeError (the empty-session guard)."""
    runtime, _mem, _seen = _make_runtime()
    sid, aid = "ses_recap", "agt_root"

    await runtime.event_store.append(_ev(
        1, EventType.SESSION_CREATED, user_prompt="do it",
        template_id="agent:tpl_echo", root_agent_id=aid))

    with pytest.raises(RuntimeError, match="no resumable tasks"):
        await runtime.recover_session(sid)
