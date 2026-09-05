"""会话活跃判据。

2026-09-05：`SessionInterrupted` / `SessionWaiting` / `SessionRunning` 三个类型已连
枚举一并删除（它们生于 2026-09-02、死于 09-03，全在 `master` 之后的分支内部，
`master` 的 `EventType` 里从来没有它们，故不可能出现在任何存量事件流中）。活跃性
现在由 `SessionCreated` / `SessionResumed` / `SessionFinished` 三条真实发射的事件，
外加 L 档的 `SessionStatusChanged` 承担。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.providers.events._lifecycle import LIFECYCLE_EVENT_TYPES, apply_lifecycle


def _ev(t: str, payload: dict | None = None, sid: str = "sess_1") -> SimpleNamespace:
    return SimpleNamespace(session_id=sid, type=t, payload=payload or {})


def test_session_finished_removes_the_session_from_active():
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionFinished", {"final_status": "SUCCEEDED"}))
    assert active == set()


def test_session_resumed_re_activates():
    """续跑 = 还活着，重启后要重新装填它的未决 HITL。"""
    active: set[str] = set()
    apply_lifecycle(active, _ev("SessionResumed", {}))
    assert active == {"sess_1"}


def test_legacy_status_changed_still_recognised():
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionStatusChanged", {"new_status": "CANCELED"}))
    assert active == set()


def test_legacy_status_changed_interrupted_is_treated_as_inactive():
    """L 档：存量日志里 `SessionStatusChanged(INTERRUPTED)` 等显式 /resume，不算活跃。"""
    active = {"sess_1"}
    apply_lifecycle(active, _ev("SessionStatusChanged", {"new_status": "INTERRUPTED"}))
    assert active == set()


def test_non_lifecycle_events_are_noop():
    active = {"sess_1"}
    apply_lifecycle(active, _ev("TaskStarted", {}))
    assert active == {"sess_1"}


def test_narrowing_tuple_covers_every_type_apply_lifecycle_reacts_to():
    """SQL 侧据此收窄查询范围；漏一个就等于这条事件对活跃判定不存在。"""
    for t in ("SessionCreated", "SessionResumed", "SessionFinished", "SessionStatusChanged"):
        assert t in LIFECYCLE_EVENT_TYPES


def test_deleted_types_are_not_in_the_narrowing_tuple():
    for t in ("SessionInterrupted", "SessionWaiting", "SessionRunning"):
        assert t not in LIFECYCLE_EVENT_TYPES
