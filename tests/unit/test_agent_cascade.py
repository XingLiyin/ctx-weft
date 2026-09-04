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


async def test_cancel_only_finalizes_hitl_of_target_agent(monkeypatch):
    """同 session 下 a1、a2 各自都有未决 HITL；只 cancel a1 时，a2 的未决提问不受影响。"""
    rt = _rt()
    _plant(rt, "a1", None, status="waiting_human", session_id="s1")
    _plant(rt, "a2", None, status="waiting_human", session_id="s1")

    class _V:
        def __init__(self, id_, agent_id):
            self.id = id_
            self.agent_id = agent_id
            self.resolved = False

    pending = [_V("h1", "a1"), _V("h2", "a2")]
    canceled: list[str] = []

    def _fake_list(session_id=None):
        return list(pending)

    async def _fake_cancel(hitl_id, **_kw):
        canceled.append(hitl_id)

    monkeypatch.setattr(rt, "list_pending_hitl", _fake_list, raising=False)
    monkeypatch.setattr(rt.hitl, "cancel", _fake_cancel, raising=False)

    await rt.cancel_agent("a1")

    assert canceled == ["h1"]
    assert rt._agent_registry.status_of("a1") == "terminated"
    assert rt._agent_registry.status_of("a2") == "waiting_human"


async def test_cancel_finalizes_hitl_before_agent_terminated_event(monkeypatch):
    """事件流顺序：未决 HITL 的终局必须先于该 agent 的 AgentTerminated。"""
    rt = _rt()
    _plant(rt, "a1", None, status="waiting_human")

    order: list[str] = []

    def _fake_list(session_id=None):
        class _V:
            id = "h1"
            agent_id = "a1"
            resolved = False
        return [_V()]

    async def _fake_cancel(hitl_id, **_kw):
        order.append(f"hitl_canceled:{hitl_id}")

    def _on_event(ev):
        if ev.type == EventType.AGENT_TERMINATED:
            order.append(f"agent_terminated:{ev.agent_id}")
        return _noop()

    monkeypatch.setattr(rt, "list_pending_hitl", _fake_list, raising=False)
    monkeypatch.setattr(rt.hitl, "cancel", _fake_cancel, raising=False)
    rt._event_bus.subscribe(None, _on_event)

    await rt.cancel_agent("a1")

    assert order == ["hitl_canceled:h1", "agent_terminated:a1"]
