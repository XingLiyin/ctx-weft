"""会话活跃判据认识新的会话状态事件（Task 8）。"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.providers.events._lifecycle import LIFECYCLE_EVENT_TYPES, apply_lifecycle


def _ev(t: str, payload: dict | None = None, sid: str = "sess_1") -> SimpleNamespace:
    return SimpleNamespace(session_id=sid, type=t, payload=payload or {})


def test_session_interrupted_removes_the_session_from_active():
    """与旧的 SessionStatusChanged(INTERRUPTED) 同语义：已标中断的会话等 /resume。"""
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionInterrupted", {"reason": "process_restart"}))
    assert active == set()


def test_waiting_keeps_the_session_active():
    """停着但正常的会话必须留在活跃集——重启后要重新装填它的未决 HITL。"""
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionWaiting", {}))
    assert active == {"sess_1"}


def test_session_running_re_activates():
    active: set[str] = set()
    apply_lifecycle(active, _ev("SessionRunning", {"reason": "resumed"}))
    assert active == {"sess_1"}


def test_legacy_status_changed_still_recognised():
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionStatusChanged", {"new_status": "CANCELED"}))
    assert active == set()


def test_new_types_are_in_the_narrowing_tuple():
    """SQL 侧据此收窄查询范围；漏一个就等于这条事件对活跃判定不存在。"""
    for t in ("SessionInterrupted", "SessionWaiting", "SessionRunning"):
        assert t in LIFECYCLE_EVENT_TYPES
