"""HITL 持久化（事件折叠 pending_hitl）+ reconcile step（spec/07 §6/§9）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.control.reducers import reduce_events, serialize_view, deserialize_view
from ctx_weft.core.events.types import Event, EventType

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
            "hitl_id": "hit_1", "form": "question", "capability_id": "control:rhi",
            "tool_call_id": "tc1", "question": "Which DB?",
        }, 1),
    ]
    view = reduce_events(events, run_id="s1")
    assert "hit_1" in view.pending_hitl
    hr = view.pending_hitl["hit_1"]
    assert hr.form == "question" and hr.tool_call_id == "tc1" and hr.task_id == "t1"

    events.append(_ev(EventType.HITL_ANSWERED, {"hitl_id": "hit_1"}, 2))
    view2 = reduce_events(events, run_id="s1")
    assert "hit_1" not in view2.pending_hitl


def test_reducer_pending_survives_snapshot_roundtrip() -> None:
    events = [
        _ev(EventType.SESSION_CREATED, {"template_id": "tpl"}, 0),
        _ev(EventType.HITL_REQUIRED, {
            "hitl_id": "hit_2", "form": "approval", "capability_id": "fs:bash",
            "tool_call_id": "tc2", "question": "ok?",
        }, 1),
    ]
    view = reduce_events(events, run_id="s1")
    restored = deserialize_view(serialize_view(view))
    assert "hit_2" in restored.pending_hitl
    assert restored.pending_hitl["hit_2"].tool_call_id == "tc2"


def test_reducer_cancelled_removes_pending() -> None:
    events = [
        _ev(EventType.HITL_REQUIRED, {"hitl_id": "h3", "form": "question", "tool_call_id": "tc3"}, 0),
        _ev(EventType.HITL_CANCELLED, {"hitl_id": "h3"}, 1),
    ]
    view = reduce_events(events, run_id="s1")
    assert "h3" not in view.pending_hitl


class _NullBus:
    async def emit(self, *_a, **_k): ...


async def test_reconcile_invokes_only_dangling_tool_calls() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from ctx_weft.core.loop.steps.reconcile import ReconcileStep
    from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
    from ctx_weft.protocols.memory import MemoryEvent
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")

    def ev(tp, content, sec, role, **md):
        return MemoryEvent(type=tp, address=sc, content=content,
                           timestamp=base + timedelta(seconds=sec), role=role, metadata=md)

    await mem.ingest(ev(MemoryEventType.LLM_RESPONSE, "", 1, "assistant",
                        tool_calls=[{"id": "tc1", "name": "web", "input": {}},
                                    {"id": "tc2", "name": "ask_user", "input": {"question": "?"}}]), pctx)
    await mem.ingest(ev(MemoryEventType.TOOL_RESULT, "web out", 2, "tool", tool_call_id="tc1"), pctx)

    invoked: list[str] = []
    async def fake_invoke(tool_name, arguments, state, ctx, tool_call_id=""):
        invoked.append(tool_call_id)
        return SimpleNamespace(content="human text", is_error=False)

    gateway = SimpleNamespace(invoke=fake_invoke)
    state = SimpleNamespace(
        scope=sc, agent=SimpleNamespace(id="ag1"),
        task=SimpleNamespace(id="t1", status="ACTIVE"), session=SimpleNamespace(id="s1"),
        extra={},                          # 无 template → resolve_and_bind 解析为空,不动 cache
    )
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx, capability_gateway=gateway,
                          cancel_token=None, event_bus=_NullBus(),
                          capability_providers=[], capability_cache=None)

    outcome = await ReconcileStep().execute(state, ctx)
    assert invoked == ["tc2"]
    assert outcome.next_step == "prepare"   # 填完 dangling → prepare 重装 prompt,再 act


async def test_reconcile_no_dangling_routes_to_prepare() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from ctx_weft.core.loop.steps.reconcile import ReconcileStep
    from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
    from ctx_weft.protocols.memory import MemoryEvent
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=MemoryEventType.LLM_RESPONSE, address=sc, content="",
                                 timestamp=base + timedelta(seconds=1), role="assistant",
                                 metadata={"tool_calls": [{"id": "tcA", "name": "web", "input": {}}]}), pctx)
    await mem.ingest(MemoryEvent(type=MemoryEventType.TOOL_RESULT, address=sc, content="out",
                                 timestamp=base + timedelta(seconds=2), role="tool",
                                 metadata={"tool_call_id": "tcA"}), pctx)
    invoked = []
    async def fake_invoke(**kw): invoked.append(kw); return None
    gateway = SimpleNamespace(invoke=lambda *a, **k: fake_invoke(**k))
    state = SimpleNamespace(scope=sc, agent=SimpleNamespace(id="ag1"),
                            task=SimpleNamespace(id="t1", status="ACTIVE"), session=SimpleNamespace(id="s1"))
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx, capability_gateway=gateway,
                          cancel_token=None, event_bus=_NullBus())
    outcome = await ReconcileStep().execute(state, ctx)
    assert invoked == []                 # 全部已有 result → 不重跑
    assert outcome.next_step == "prepare"


async def test_resolve_reconcile_detection_helper() -> None:
    from datetime import UTC, datetime, timedelta
    from ctx_weft.core.runtime import _task_has_dangling_tool_call
    from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
    from ctx_weft.protocols.memory import MemoryEvent
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    base = datetime(2026, 1, 1, tzinfo=UTC)
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    sc = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")

    def ev(tp, sec, role, **md):
        return MemoryEvent(type=tp, address=sc, content="", timestamp=base + timedelta(seconds=sec),
                           role=role, metadata=md)

    await mem.ingest(ev(MemoryEventType.LLM_RESPONSE, 1, "assistant",
                        tool_calls=[{"id": "tc1", "name": "web", "input": {}}]), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is True
    await mem.ingest(ev(MemoryEventType.TOOL_RESULT, 2, "tool", tool_call_id="tc1"), pctx)
    assert await _task_has_dangling_tool_call(mem, sc, pctx) is False
