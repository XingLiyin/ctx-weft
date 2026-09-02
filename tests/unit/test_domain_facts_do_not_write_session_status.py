"""领域事实不写会话状态（Task 7 · docs/events-v2.md §2.1.1）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int, *, task_id: str | None = None) -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), task_id=task_id, payload=payload)


def _created() -> Event:
    return _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0)


def test_hitl_opened_no_longer_pauses_the_session():
    view = reduce_events([_created(), _ev(EventType.HITL_OPENED, {
        "hitl_id": "hit_1",
        "delivery": {"kind": "tool_result", "tool_call_id": "c1"}}, 1)], "run_1")
    assert view.session_status == "RUNNING"


def test_hitl_resolved_no_longer_returns_the_session_to_running():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.HITL_RESOLVED, {"hitl_id": "hit_1", "outcome": "accepted"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_run_finished_no_longer_writes_the_session_status():
    view = reduce_events([_created(),
                          _ev(EventType.RUN_FINISHED, {"final_status": "FINISHED"}, 1)],
                         "run_1")
    assert view.session_status == "RUNNING"


def test_run_started_no_longer_writes_the_session_status():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.RUN_STARTED, {"run_id": "run_1", "initial_step": "prepare"}, 2),
    ], "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_paused_hitl_still_folds_for_old_logs():
    # 旧模型按 form 分 PAUSED / PAUSED_HITL 两档；新值域只有 WAITING（Task 9 收敛掉
    # PAUSED/PAUSED_HITL）。L 档的职责是把旧事实翻译进当前词表，故这里恒折进 WAITING，
    # 不再区分 form。见 task-7-report.md「C1 复议」。
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_PAUSED_HITL, {"form": "approval"}, 1)],
                         "run_1")
    assert view.session_status == "WAITING"


def test_legacy_session_status_changed_still_folds_for_old_logs():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": "INTERRUPTED"}, 1)], "run_1")
    assert view.session_status == "INTERRUPTED"
