from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT
from ctx_weft.protocols.events import EVENT_TYPES, EventType, TRANSIENT_EVENT_TYPES
from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event
from ctx_weft.core.util import generate_id, now_utc


def _ev(type_, session_id="ses1", payload=None):
    return Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id=session_id,
        type=type_, timestamp=now_utc(), tenant_id="default", payload=payload or {},
    )


def test_task_recap_events_registered_and_persisted():
    assert EventType.TASK_RECAP_STARTED in EVENT_TYPES
    assert EventType.TASK_RECAP_DONE in EVENT_TYPES
    # 必须落盘：不得是 transient
    assert EventType.TASK_RECAP_STARTED not in TRANSIENT_EVENT_TYPES
    assert EventType.TASK_RECAP_DONE not in TRANSIENT_EVENT_TYPES
    # 无状态机含义
    assert EventType.TASK_RECAP_STARTED not in TASK_STATUS_BY_EVENT
    assert EventType.TASK_RECAP_DONE not in TASK_STATUS_BY_EVENT


def test_task_recap_events_are_reducer_noops():
    events = [
        _ev(EventType.SESSION_CREATED, payload={"template_id": "t", "root_agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_STARTED, payload={"task_id": "tsk1", "boundary": "finish", "agent_id": "a1"}),
        _ev(EventType.TASK_RECAP_DONE, payload={"task_id": "tsk1"}),
    ]
    view = reduce_events(events, run_id="ses1")
    # session 仍是 RUNNING（recap 事件不改会话/任务状态）
    assert view.sessions["ses1"].status == "RUNNING"
