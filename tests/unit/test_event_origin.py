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
