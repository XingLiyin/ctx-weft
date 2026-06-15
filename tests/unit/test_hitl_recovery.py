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
    assert was_hot is False


def test_restore_keeps_hitl_parked_task_suspended() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parked = Task(id="t1", session_id="s1", status="SUSPENDED")
    tm.restore([parked], terminal_ids=set(), parked_task_ids={"t1"})
    assert tm.get_task("t1").status == "SUSPENDED"
    assert not tm._queue.has_pending()


def test_restore_requeues_suspended_on_children_when_all_terminal() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", status="SUSPENDED")
    child = Task(id="c", session_id="s1", status="FINISHED", parent_task_id="p")
    tm.restore([parent, child], terminal_ids={"c"}, parked_task_ids=set())
    assert tm.get_task("p").status == "PENDING"


def test_restore_parked_ids_default_none_is_old_behavior() -> None:
    from loomex_core.core.orchestrator.task_manager import TaskManager
    from loomex_core.core.state.models import Task
    tm = TaskManager(session_id="s1")
    parent = Task(id="p", session_id="s1", status="SUSPENDED")
    tm.restore([parent], terminal_ids=set())   # no parked_task_ids → old behavior: requeue
    assert tm.get_task("p").status == "PENDING"


async def test_recover_session_rebuilds_pending_hitl_and_parks() -> None:
    import asyncio
    from datetime import datetime, timezone
    from loomex_core.core import LoomeXRuntime
    from loomex_core.core.events.types import Event, EventType
    from loomex_core.providers.llm.mock import MockLLMAdapter, MockResponse
    from loomex_core.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

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
           capability_id="control:ask_user", tool_call_id="tcA", question="Which DB?"),
        ev(6, EventType.TASK_SUSPENDED, task_id="tsk_1"),
    ]
    for e in seed:
        await runtime.event_store.append(e)

    await runtime.recover_session("ses_1")
    await asyncio.sleep(0)

    pend = runtime.hitl_manager.list_pending(session_id="ses_1")
    assert len(pend) == 1 and pend[0].tool_call_id == "tcA"
    assert llm.last_request is None             # parked task did not run


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
