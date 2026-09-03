"""task 的成果物与死因必须能从事件流折进投影（总账 A1）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.utils import generate_id, now_utc


def _ev(t: EventType, payload: dict, *, task_id: str = "tsk_1") -> Event:
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0,
        session_id="sess_1", type=t, timestamp=now_utc(),
        tenant_id="default", task_id=task_id, payload=payload,
    )


def _created() -> Event:
    return _ev(EventType.TASK_CREATED, {"task": {
        "id": "tsk_1", "session_id": "sess_1", "status": "PENDING", "title": "t",
        "description": "", "creator_agent_id": "", "assigned_agent_id": "",
        "parent_task_id": None, "user_prompt": "p", "priority": 0, "max_retries": 3,
        "timeout_ms": 0, "dag_deps": [], "interaction_mode": "", "settings": {},
        "origin_tool_call_id": None, "origin_tool_name": None,
        "result": None, "outputs": {}, "error": None,
        "created_at": None, "updated_at": None,
    }})


def test_task_finished_folds_outputs_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
        _ev(EventType.TASK_FINISHED, {
            "outcome": "success", "summary": "done", "outputs": {"result": "ok"},
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].outputs == {"result": "ok"}
    assert view.tasks["tsk_1"].status == "FINISHED"


def test_task_failed_folds_error_message_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "fail"}),
        _ev(EventType.TASK_FAILED, {
            "error_code": "TASK_FAILED_BY_OBSERVER",
            "error_message": "boom", "retry_count": 0,
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].error == "boom"
    assert view.tasks["tsk_1"].status == "FAILED"


def test_task_interrupted_folds_error_message_into_view():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_INTERRUPTED, {
            "reason": "run_crash", "error_code": "CONTEXT_OVERFLOW",
            "error_message": "window exceeded", "retry_count": 1,
        }),
    ], "run_1")
    assert view.tasks["tsk_1"].error == "window exceeded"


def test_task_finalized_no_longer_wipes_outputs():
    """TaskFinalized 在 TaskFinished 之前到达，不得把已折的成果清空。"""
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINISHED, {
            "outcome": "success", "summary": "s", "outputs": {"a": 1},
        }),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
    ], "run_1")
    assert view.tasks["tsk_1"].outputs == {"a": 1}


def test_task_finalized_still_sets_finished_at():
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_FINALIZED, {"task_id": "tsk_1", "outcome": "success"}),
    ], "run_1")
    assert view.tasks["tsk_1"].finished_at is not None
