from ctx_weft.core.control.reducers import fold_pending_task_recap
from ctx_weft.protocols.events import Event
from ctx_weft.protocols.events import EventType
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id


def _ev(type_, payload):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="ses1",
        type=type_, timestamp=now_utc(), tenant_id="default",
        task_id=payload.get("task_id"), payload=payload,
    )


def test_started_without_done_is_pending():
    events = [_ev(EventType.TASK_RECAP_STARTED,
                  {"task_id": "t1", "boundary": "finish", "agent_id": "a1"})]
    assert fold_pending_task_recap(events) == {"t1": {"boundary": "finish", "agent_id": "a1"}}


def test_started_then_done_is_empty():
    events = [
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
    ]
    assert fold_pending_task_recap(events) == {}


def test_last_write_wins_per_task():
    # 同 task 二次 started（如 recover 又崩一次）：以最后一次 boundary 为准
    events = [
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "interrupt", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, {"task_id": "t1"}),
        _ev(EventType.TASK_RECAP_STARTED, {"task_id": "t1", "boundary": "finish", "agent_id": "a1"}),
    ]
    assert fold_pending_task_recap(events) == {"t1": {"boundary": "finish", "agent_id": "a1"}}
