"""Plain-text pause uses session status PAUSED (distinct from ask_user's PAUSED_HITL)."""

from __future__ import annotations

import pytest

from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.events import EventType
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.orchestrator.control_capability import WAIT_FOR_USER_CAPABILITY_ID

pytestmark = pytest.mark.asyncio


def _collect_events(bus: InProcessEventBus) -> list:
    seen: list = []

    async def handler(ev):
        seen.append(ev)

    bus.subscribe(None, handler)
    return seen


async def test_session_status_literal_includes_paused() -> None:
    from typing import get_args
    from ctx_weft.core.state.models import SessionStatus
    assert "PAUSED" in get_args(SessionStatus)


async def test_session_paused_hitl_event_carries_capability_id() -> None:
    bus = InProcessEventBus()
    events = _collect_events(bus)
    mgr = HitlManager(event_bus=bus)

    await mgr.request(
        form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id=WAIT_FOR_USER_CAPABILITY_ID, question="",
    )

    paused = [e for e in events if e.type == EventType.SESSION_PAUSED_HITL]
    assert len(paused) == 1
    assert paused[0].payload.get("capability_id") == WAIT_FOR_USER_CAPABILITY_ID


def _paused_event(session_id: str, form: str):
    from ctx_weft.core.events.types import Event
    from ctx_weft.core.utils import generate_id, now_utc
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id=session_id,
        type=EventType.SESSION_PAUSED_HITL, timestamp=now_utc(), task_id=None,
        payload={"capability_id": "whatever", "form": form},
    )


@pytest.mark.parametrize("form,expected", [
    ("wait", "PAUSED"),
    ("question", "PAUSED_HITL"),
    ("approval", "PAUSED_HITL"),
])
async def test_session_paused_hitl_routes_by_form(form, expected) -> None:
    """SESSION_PAUSED_HITL 按显式 form 分流,不再看 capability sentinel。"""
    from ctx_weft.core.control.reducers import reduce_events
    view = reduce_events([_paused_event("s1", form)], run_id="r1")
    assert view.session_status == expected
