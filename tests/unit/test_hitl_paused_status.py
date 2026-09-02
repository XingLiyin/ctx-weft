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


# ── 新模型：投影直接由 HITL_OPENED / HITL_RESOLVED 驱动（复审 I4）─────────────
#
# `SessionPausedHitl` 不再被发出，而 reducer 里原本也没有 HITL_OPENED 分支——于是一个停在
# 普通纯文本暂停上的会话在投影与 SSE 里一直显示 RUNNING，直到进程重启才被 `recover()`
# 纠正；被崩溃恢复标成 PAUSED_HITL 的会话则永远回不到 RUNNING（旧的回 RUNNING 分支只列了
# 五个 legacy 终态事件，没有 HITL_RESOLVED）。


def _hitl_event(session_id: str, etype, payload: dict):
    from ctx_weft.protocols.events import Event
    from ctx_weft.core.utils import generate_id, now_utc
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id=session_id,
        type=etype, timestamp=now_utc(), task_id="t1", payload=payload,
    )


def _opened(session_id: str, delivery: dict):
    return _hitl_event(session_id, EventType.HITL_OPENED,
                       {"hitl_id": "h1", "form": "whatever", "delivery": delivery})


@pytest.mark.parametrize("delivery,expected", [
    ({"kind": "user_turn", "task_id": "t1"}, "PAUSED"),
    ({"kind": "tool_result", "tool_call_id": "call_1"}, "PAUSED_HITL"),
    ({"kind": "no_resume"}, "PAUSED_HITL"),
])
async def test_hitl_opened_pauses_the_session_by_delivery(delivery, expected) -> None:
    """判据是 **delivery**，不是 form——与 `_derive_paused_status` 共用 `paused_status_for`。"""
    from ctx_weft.core.control.reducers import reduce_events
    view = reduce_events([_opened("s1", delivery)], run_id="r1")
    assert view.session_status == expected


async def test_hitl_resolved_returns_the_session_to_running() -> None:
    from ctx_weft.core.control.reducers import reduce_events
    events = [
        _opened("s1", {"kind": "tool_result", "tool_call_id": "call_1"}),
        _hitl_event("s1", EventType.HITL_RESOLVED,
                    {"hitl_id": "h1", "outcome": "accepted"}),
    ]
    view = reduce_events(events, run_id="r1")
    assert view.session_status == "RUNNING"


async def test_hitl_resolved_does_not_overwrite_a_terminal_status() -> None:
    """仅当仍处暂停态才掰回 RUNNING——迟到的 HITL_RESOLVED 不得复活一个已收尾的会话。"""
    from ctx_weft.core.control.reducers import reduce_events
    from ctx_weft.protocols.events import Event
    from ctx_weft.core.utils import generate_id, now_utc
    finished = Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="s1",
        type=EventType.SESSION_FINISHED, timestamp=now_utc(), task_id=None,
        payload={"final_status": "SUCCEEDED"},
    )
    view = reduce_events(
        [_opened("s1", {"kind": "no_resume"}), finished,
         _hitl_event("s1", EventType.HITL_RESOLVED,
                     {"hitl_id": "h1", "outcome": "accepted"})],
        run_id="r1")
    assert view.session_status == "SUCCEEDED"
