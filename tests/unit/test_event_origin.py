from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.protocols.events import Event, EventOrigin


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id=None, sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC),
    )
    base.update(kw)
    return Event(**base)


def test_origin_defaults_to_empty_string():
    """存量事件读出空串——§0 明说不做反推。"""
    assert _ev().origin == ""


def test_origin_round_trips():
    assert _ev(origin=EventOrigin.LOOP_ACT).origin == "loop.act"


def test_event_origin_has_17_values():
    assert len(EventOrigin.all()) == 17


def test_origin_values_are_two_level_dotted_or_bare():
    """§4：两级点号供 host 前缀匹配；分隔符用 . 不用 :（: 留给 capability id）。"""
    for v in EventOrigin.all():
        assert ":" not in v
        assert v.count(".") <= 1
        assert v == v.strip()


def test_loop_prefix_matches_all_loop_origins():
    loop = {v for v in EventOrigin.all() if v.startswith("loop.")}
    assert EventOrigin.LOOP_ACT in loop
    assert EventOrigin.LOOP_BACKGROUND_OBSERVE in loop
    assert EventOrigin.RUNTIME not in loop


# ── Task 2: LoopState.origin 管道与 make_event 默认取值 ──


from ctx_weft.core.loop.driver import make_event
from ctx_weft.protocols.events import EventType


class _FakeState:
    """make_event 只读这几个字段。"""
    def __init__(self, origin: str = ""):
        self.session_id = "s1"
        self.task_id = "t1"
        self.agent_id = "a1"
        self.run_id = "r1"
        self.tenant_id = "default"
        self.sequence_counter = 0
        self.origin = origin
        # 模拟 LoopState 中的 Session/Task/Agent 对象
        self.session = type('Session', (), {'id': 's1', 'tenant_id': 'default'})()
        self.task = type('Task', (), {'id': 't1'})()
        self.agent = type('Agent', (), {'id': 'a1'})()


def test_make_event_takes_origin_from_state():
    """§4 填充方式 1：40+ 个循环内发射点零改动。"""
    ev = make_event(_FakeState(origin=EventOrigin.LOOP_ACT), EventType.ACT_TURN_STARTED, {})
    assert ev.origin == "loop.act"


def test_make_event_explicit_origin_overrides_state():
    """§4 填充方式 2：给 background observe 这类脱离主 driver 序列的场景。"""
    ev = make_event(
        _FakeState(origin=EventOrigin.LOOP_OBSERVE),
        EventType.LLM_PROMPT_SENT,
        {},
        origin=EventOrigin.LOOP_BACKGROUND_OBSERVE,
    )
    assert ev.origin == "loop.background_observe"
