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


# ── Task 7（会话状态所有权重构）之后：HITL_OPENED / HITL_RESOLVED 不再驱动投影 ──
#
# 上面这段旧注释描述的是 Task 6 引入、Task 7 收回的行为：`HITL_OPENED` 分支已整个删除，
# `HITL_RESOLVED`（新模型）也已从会话回 RUNNING 的 elif 元组里移出——「等人」这件事的
# 会话状态改由 SessionManager 承载（docs/events-v2.md §2.1.1）。正面用例见
# `tests/unit/test_domain_facts_do_not_write_session_status.py`
# （`test_hitl_opened_no_longer_pauses_the_session` /
# `test_hitl_resolved_no_longer_returns_the_session_to_running`）。
# 下面只留一条：确认 HITL_RESOLVED 对已终结的会话仍是无操作（无论是不是因为它已被削掉写入）。


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
