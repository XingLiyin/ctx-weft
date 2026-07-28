"""Cold-restart resume must ingest the user's reply into the resumed task's real agent
scope — the "first prompt after restart is lost / blank reply" bug.

On a cold app restart the pending wait_for_user HITL is rebuilt from the event store.
The HITL_REQUIRED event never persisted agent_id, so the rebuilt HitlRequest has
agent_id="". _inject_user_reply then ingested the reply into a memory scope with
agent_id="".

That reply IS findable by recall_recent (task-layer recall keys on task_id), but the
actor's history comes from AgentRecallSource → recall_recent_by_agent, which filters by
scope.agent_id. With agent_id="" the reply is invisible to assembly, so the actor sees
history ending in the prior assistant turn, appends the "continue task" resume cue, and
produces a blank reply. Only the FIRST post-restart reply is affected; later parks create
live HITLs that still carry agent_id.

Two layers:
1. _inject_user_reply falls back to the resumed task's agent scope when req.agent_id is
   missing (robust even if the projection ever loses it).
2. The HITL_REQUIRED projection round-trips agent_id (fix at source).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.control.reducers import fold_pending_hitl
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.state.models import HitlRequest, Session, Task
from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

_TS = datetime(2026, 7, 13, tzinfo=UTC)


# ── Layer 1: _inject_user_reply scope fallback ─────────────────────────────────


async def test_inject_user_reply_reply_visible_to_agent_recall(monkeypatch):
    """When the rebuilt HitlRequest lost agent_id, the reply must still be recallable via
    recall_recent_by_agent (the actor's history path), which filters by scope.agent_id."""
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
    from ctx_weft.core import CtxWeftRuntime
    import ctx_weft.core.loop.steps.background_observe as bo

    monkeypatch.setattr(bo, "_task_pending", {})

    runtime = make_runtime(agent_provider=InlineAgentTemplateProvider())
    mem = InMemoryMemoryProvider()
    runtime.providers.register_memory(mem)

    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    task = Task(id="t1", session_id="s1", status="SUSPENDED",
                assigned_agent_id="ag_root", creator_agent_id="ag_root")
    task_manager = SimpleNamespace(get_task=lambda tid: task if tid == "t1" else None)

    # Rebuilt-from-projection HITL: agent_id was NOT persisted → empty.
    req = HitlRequest(id="h1", form="wait", session_id="s1", task_id="t1", agent_id="",
                      status="accepted", message="用户回复", context="plain_text")

    await runtime._inject_user_reply(req, session, task_manager)

    # The actor recalls task body by agent (recall_recent_by_agent filters on scope.agent_id).
    agent_scope = MemoryAddress(session_id="s1", task_id=None, agent_id="ag_root")
    ctx = ProviderContext(session_id="s1", tenant_id="default")
    recs = await mem.recall_recent_by_agent(agent_scope, [MemoryEventType.USER_PROMPT], 10, ctx)
    assert [r.content for r in recs] == ["用户回复"], (
        "reply invisible to recall_recent_by_agent — it was ingested into the empty agent "
        "scope, so the actor never sees it (→ continue-cue → blank reply)"
    )


# ── Layer 2: HITL_REQUIRED projection preserves agent_id ───────────────────────


async def test_hitl_required_projection_preserves_agent_id():
    """fold_pending_hitl must round-trip agent_id from the HITL_REQUIRED event so cold
    resume rebuilds a HitlRequest that still knows its agent scope."""
    ev = Event(
        id="evt_1", run_id="r1", sequence=1, session_id="s1", task_id="t1",
        agent_id="ag_root", type=EventType.HITL_REQUIRED, timestamp=_TS,
        payload={"hitl_id": "h1", "form": "wait", "agent_id": "ag_root",
                 "context": "plain_text"},
    )
    pending = fold_pending_hitl([ev])
    assert "h1" in pending
    assert pending["h1"].agent_id == "ag_root", (
        f"agent_id lost through HITL projection: {pending['h1'].agent_id!r}"
    )
