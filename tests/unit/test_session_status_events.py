"""会话状态事件在 reducer 里的折叠；TM 信号不折叠（Task 3）。"""

from __future__ import annotations

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.events import Event, EventType


def _ev(t: EventType, payload: dict, seq: int) -> Event:
    return Event(id=generate_id("evt"), run_id=None, sequence=seq, session_id="sess_1",
                 type=t, timestamp=now_utc(), payload=payload)


def _created() -> Event:
    return _ev(EventType.SESSION_CREATED, {"root_agent_id": "ag_1"}, 0)


def test_session_interrupted_sets_interrupted():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_INTERRUPTED, {"reason": "llm_outage"}, 1)],
                         "run_1")
    assert view.session_status == "INTERRUPTED"
    assert view.sessions["sess_1"].status == "INTERRUPTED"


def test_session_waiting_sets_waiting():
    view = reduce_events([_created(),
                          _ev(EventType.SESSION_WAITING, {}, 1)],
                         "run_1")
    assert view.session_status == "WAITING"


def test_session_running_returns_to_running():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_WAITING, {}, 1),
        _ev(EventType.SESSION_RUNNING, {"reason": "human_replied"}, 2),
    ], "run_1")
    assert view.session_status == "RUNNING"
    assert view.sessions["sess_1"].status == "RUNNING"


def test_session_running_does_not_resurrect_a_finished_session():
    view = reduce_events([
        _created(),
        _ev(EventType.SESSION_FINISHED, {"final_status": "SUCCEEDED"}, 1),
        _ev(EventType.SESSION_RUNNING, {"reason": "resumed"}, 2),
    ], "run_1")
    assert view.session_status == "SUCCEEDED"


def test_task_manager_signals_do_not_touch_the_projection():
    """TM 信号是 SM 的输入，不是投影的输入（O 档）。reducer 折叠它们就等于
    会话状态有了第二个写入者，正是本次要消灭的东西。"""
    view = reduce_events([
        _created(),
        _ev(EventType.TASK_QUEUE_BLOCKED, {"count": 1}, 1),
        _ev(EventType.TASK_QUEUE_INTERRUPTED, {"reason": "llm_outage"}, 2),
        _ev(EventType.TASK_QUEUE_DRAINED, {"final_status": "SUCCEEDED"}, 3),
    ], "run_1")
    assert view.session_status == "RUNNING"


def test_none_of_the_six_new_types_reuse_a_legacy_string():
    for member, value in [
        (EventType.TASK_QUEUE_BLOCKED, "TaskQueueBlocked"),
        (EventType.TASK_QUEUE_INTERRUPTED, "TaskQueueInterrupted"),
        (EventType.TASK_QUEUE_DRAINED, "TaskQueueDrained"),
        (EventType.SESSION_INTERRUPTED, "SessionInterrupted"),
        (EventType.SESSION_WAITING, "SessionWaiting"),
        (EventType.SESSION_RUNNING, "SessionRunning"),
    ]:
        assert member == value
