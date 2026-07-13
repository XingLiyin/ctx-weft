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
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.reducers import rebuild_view
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryScope, ProviderContext,
)
from ctx_weft.protocols.capability import qualify
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _make_runtime() -> tuple[CtxWeftRuntime, InMemoryMemoryProvider, list]:
    """Build a runtime with the echo template + in-memory memory, spying on emits.

    The mock LLM returns a fixed recap for the relaunched background observe so the
    re-run completes deterministically (plain-text fallback across react rounds).
    """
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    # Enough plain-text responses to outlast the observe ReAct loop
    # (max_turns_per_observe defaults to 5) without exhausting the mock.
    llm = MockLLMAdapter(responses=[MockResponse(text="recovered recap") for _ in range(8)])
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
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
            template_id="tpl_echo", root_agent_id=aid),
        _ev(2, EventType.RUN_STARTED),
        _ev(3, EventType.TASK_CREATED, task={
            "id": tid, "status": "ACTIVE", "title": "T", "kind": "reasoning",
            "assigned_agent_id": aid, "creator_agent_id": aid}),
        _ev(4, EventType.TASK_STARTED, task_id=tid, assigned_agent_id=aid),
        _ev(5, EventType.TASK_FINISHED, task_id=tid,
            outcome="success", summary="done", outputs={"result": "ok"}),
        _ev(6, EventType.TASK_FINALIZED, task_id=tid, outputs={"result": "ok"}),
        # Crash mid finish-boundary observe: STARTED persisted, DONE never was.
        _ev(7, EventType.TASK_RECAP_STARTED, task_id=tid, boundary="finish", agent_id=aid),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    # Pre-crash state: session projection is still RUNNING (no SESSION_FINISHED).
    view_before = await rebuild_view(runtime.event_store, sid)
    assert view_before.sessions[sid].status == "RUNNING"

    # Seed the placeholder finish pair in memory so _relaunch's close-synth path can
    # locate the finish_task tool_call and supersede the placeholder Process Report.
    scope = MemoryScope(session_id=sid, task_id=tid, agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id=tid, agent_id=aid)
    fin_tcid = "tc_finish"
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content="Process Report: (placeholder)", timestamp=_TS, role="assistant",
        metadata={"origin_task_id": tid, "tool_calls": [
            {"id": fin_tcid, "name": qualify("control:finish_task"), "input": {}}]},
    ), pctx)
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
        content="(placeholder finish result)", timestamp=_TS, role="tool",
        metadata={"origin_task_id": tid, "tool_call_id": fin_tcid},
    ), pctx)

    # ── Recover ────────────────────────────────────────────────────────────────
    await runtime.recover_session(sid)

    # finalize_idle_session awaits the relaunched recap before emitting SESSION_FINISHED,
    # so it should already be persisted; drain any stragglers defensively.
    tm = runtime._task_managers.get(sid)
    if tm is not None and tm._background_asyncio_tasks:
        await asyncio.gather(*list(tm._background_asyncio_tasks), return_exceptions=True)

    # SESSION_FINISHED(SUCCEEDED) was emitted.
    finished = [
        e for e in seen
        if getattr(e, "type", None) == EventType.SESSION_FINISHED
        and (e.payload or {}).get("final_status") == "SUCCEEDED"
    ]
    assert finished, (
        "expected SESSION_FINISHED(SUCCEEDED) after recovery; got "
        f"{[(getattr(e, 'type', None), (e.payload or {}).get('final_status')) for e in seen]}"
    )

    # The projection is now terminal — a repeated recovery would no longer hang.
    view = await rebuild_view(runtime.event_store, sid)
    assert view.sessions[sid].status == "SUCCEEDED"

    # The pending recap was closed out (TASK_RECAP_DONE emitted by the re-run's finally).
    assert any(
        getattr(e, "type", None) == EventType.TASK_RECAP_DONE
        and (e.payload or {}).get("task_id") == tid
        for e in seen
    ), "expected TASK_RECAP_DONE from the relaunched recap"


async def test_no_tasks_at_all_still_raises() -> None:
    """A session with no tasks at all is a genuinely empty/broken projection and must
    still raise RuntimeError (the empty-session guard)."""
    runtime, _mem, _seen = _make_runtime()
    sid, aid = "ses_recap", "agt_root"

    await runtime.event_store.append(_ev(
        1, EventType.SESSION_CREATED, user_prompt="do it",
        template_id="tpl_echo", root_agent_id=aid))

    with pytest.raises(RuntimeError, match="no resumable tasks"):
        await runtime.recover_session(sid)
