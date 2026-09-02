"""SessionManager 持有会话状态并按状态机转移（Task 4）。"""

from __future__ import annotations

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.orchestrator.session_state import SessionInput
from ctx_weft.protocols.events import EventType

from tests.unit._session_helpers import RecordingBus


def _sm(bus: RecordingBus) -> SessionManager:
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    sm.register_session("sess_1", tenant_id="t1")
    return sm


async def test_register_session_starts_running():
    assert _sm(RecordingBus()).status_of("sess_1") == "RUNNING"


async def test_unknown_session_has_empty_status_not_an_exception():
    """host 会拿任意 id 来问；抛异常会把一次查询变成一次 500。"""
    sm = SessionManager(lifecycle_manager=None, event_bus=RecordingBus())
    assert sm.status_of("nope") == ""


async def test_blocked_on_human_emits_awaiting_and_moves_state():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    assert sm.status_of("sess_1") == "WAITING"
    assert bus.types() == [EventType.SESSION_WAITING]
    assert bus.events[0].payload == {}          # 会话事件不带展示数据（裁定 R3）
    assert bus.events[0].session_id == "sess_1"
    assert bus.events[0].run_id is None          # 会话级事件不属于任何 run
    assert bus.events[0].tenant_id == "t1"


async def test_repeating_the_same_signal_emits_nothing():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    assert bus.types() == [EventType.SESSION_WAITING]


async def test_terminal_state_absorbs_every_later_input():
    bus = RecordingBus()
    sm = _sm(bus)
    await sm._apply("sess_1", SessionInput.CANCEL)
    before = len(bus.events)
    await sm._apply("sess_1", SessionInput.QUEUE_BLOCKED)
    await sm._apply("sess_1", SessionInput.QUEUE_INTERRUPTED, reason="llm_outage")
    await sm._apply("sess_1", SessionInput.TASK_STARTED)
    assert sm.status_of("sess_1") == "CANCELED"
    assert len(bus.events) == before
    assert sm.is_terminal("sess_1") is True


async def test_applying_to_an_unregistered_session_is_a_noop():
    bus = RecordingBus()
    sm = SessionManager(lifecycle_manager=None, event_bus=bus)
    await sm._apply("nope", SessionInput.QUEUE_DRAINED, final_status="SUCCEEDED")
    assert bus.types() == []


async def test_forget_session_releases_the_state():
    sm = _sm(RecordingBus())
    sm.forget_session("sess_1")
    assert sm.status_of("sess_1") == ""
