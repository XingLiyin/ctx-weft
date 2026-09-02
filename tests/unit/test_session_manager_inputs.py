"""SM 的输入只有 TM 的四类事件 + 外部命令（Task 5）。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import Event, EventType

from tests.unit._session_helpers import RecordingBus


def _sm(bus: RecordingBus) -> SessionManager:
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    sm.register_session("sess_1", tenant_id="t1")
    return sm


def _ev(t: EventType, payload: dict) -> Event:
    return Event(id=generate_id("evt"), run_id="run_1", sequence=1, session_id="sess_1",
                 type=t, timestamp=now_utc(), payload=payload)


async def test_blocked_on_human_signal_pauses_the_session():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 2}))
    assert sm.status_of("sess_1") == "WAITING"
    assert bus.types() == [EventType.SESSION_WAITING]


async def test_blocked_without_panel_pauses_without_panel():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 1}))
    assert sm.status_of("sess_1") == "WAITING"


async def test_interrupted_signal_interrupts():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_INTERRUPTED, {"reason": "llm_outage"}))
    assert sm.status_of("sess_1") == "INTERRUPTED"
    assert bus.events[0].payload == {"reason": "llm_outage"}


async def test_drained_signal_finishes():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_DRAINED, {"final_status": "SUCCEEDED"}))
    assert bus.types() == [EventType.SESSION_FINISHED]
    assert bus.events[0].payload == {"final_status": "SUCCEEDED"}


async def test_task_started_brings_a_paused_session_back():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.TASK_QUEUE_BLOCKED,
                              {"count": 1}))
    await sm.handle_event(_ev(EventType.TASK_STARTED, {"assigned_agent_id": "ag_1"}))
    assert sm.status_of("sess_1") == "RUNNING"
    assert bus.types()[-1] == EventType.SESSION_RUNNING
    assert bus.events[-1].payload == {"reason": "human_replied"}


async def test_lower_layer_events_are_not_subscribed():
    """HITL 决定 task 状态、loop 决定 run 是否被打断——SM 看不见这两层
    （docs/events-v2.md §2.1.1 严格分层）。"""
    bus = RecordingBus()
    sm = _sm(bus)
    for t, p in ((EventType.HITL_OPENED, {"hitl_id": "hit_1",
                                          "delivery": {"kind": "tool_result"}}),
                 (EventType.HITL_RESOLVED, {"hitl_id": "hit_1", "outcome": "accepted"}),
                 (EventType.TASK_AWAITING_HUMAN, {"hitl_id": "hit_1"}),
                 (EventType.RUN_INTERRUPTED, {"reason": "llm_outage"}),
                 (EventType.TASK_INTERRUPTED, {"reason": "run_crash"}),
                 (EventType.TASK_SUSPENDED, {"summary": "waiting"})):
        await sm.handle_event(_ev(t, p))
    assert sm.status_of("sess_1") == "RUNNING"
    assert bus.types() == []


async def test_session_status_events_are_ignored_by_the_handler():
    """SM 自己发的事件会经总线回流到自己（in-process bus 在 emit 内同步 drain）。
    handler 必须对它们 no-op，否则一次转移会引发无穷递归。"""
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.handle_event(_ev(EventType.SESSION_WAITING, {}))
    assert bus.types() == []


async def test_cancel_command_finishes_the_session():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm.cancel("sess_1")
    assert bus.types() == [EventType.SESSION_FINISHED]
    assert bus.events[0].payload == {"final_status": "CANCELED"}


async def test_attach_to_bus_registers_one_handler():
    bus = RecordingBus()
    SessionManager(lifecycle_manager=None, event_bus=bus).attach_to_bus()
    assert len(bus.handlers) == 1
