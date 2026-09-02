"""Plain-text pause uses session status PAUSED (distinct from ask_user's PAUSED_HITL)."""

from __future__ import annotations

import pytest

from ctx_weft.protocols.events import EventType

pytestmark = pytest.mark.asyncio


async def test_session_status_literal_includes_paused() -> None:
    from typing import get_args
    from ctx_weft.core.state.models import SessionStatus
    assert "PAUSED" in get_args(SessionStatus)


# `SessionPausedHitl` 不再由 core 发出——会话暂停态改由 pending 集合的 delivery 推导
# （`CtxWeftRuntime._derive_paused_status`，见 `test_hitl_recovery_v2.py` 的四条状态用例）。
# 旧事件仍在存量日志里，故下面的 reducer 投影分支保留并继续被测。


def _paused_event(session_id: str, form: str):
    from ctx_weft.protocols.events import Event
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
