"""EventFilter 的 agent 维度（2026-09-04 spec §5.1）。

信封本来就带 agent_id，这里只是让订阅侧能按它过滤——host 要为单个 agent
渲染事件流，不加这一维就只能订阅整个 session 再自己丢弃。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.protocols.events import Event, EventFilter
from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus, _matches


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id="run_1", sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC), origin="runtime",
    )
    base.update(kw)
    return Event(**base)


def test_filter_defaults_to_none():
    assert EventFilter().agent_id is None


def test_matches_when_agent_id_equal():
    assert _matches(_ev(agent_id="agt_1"), EventFilter(agent_id="agt_1"))


def test_rejects_when_agent_id_differs():
    assert not _matches(_ev(agent_id="agt_2"), EventFilter(agent_id="agt_1"))


def test_rejects_when_event_has_no_agent_id():
    """信封 agent_id 可空（SessionCreated 等就没有）。按 agent 过滤时它们不该混进来。"""
    assert not _matches(_ev(agent_id=None), EventFilter(agent_id="agt_1"))


def test_unset_filter_still_matches_everything():
    """回归：不传 agent_id 的既有订阅方行为一个字不变。"""
    assert _matches(_ev(agent_id="agt_1"), EventFilter())
    assert _matches(_ev(agent_id=None), EventFilter())


def test_agent_id_composes_with_other_dimensions():
    ev = _ev(agent_id="agt_1", task_id="tsk_1")
    assert _matches(ev, EventFilter(agent_id="agt_1", task_id="tsk_1"))
    assert not _matches(ev, EventFilter(agent_id="agt_1", task_id="tsk_2"))


@pytest.mark.asyncio
async def test_stream_filters_by_agent_id():
    """端到端：内置 bus 的 stream 只吐指定 agent 的事件。"""
    bus = InProcessEventBus()
    got: list[str] = []

    async def _consume():
        async for ev in bus.stream(EventFilter(agent_id="agt_1")):
            got.append(ev.id)
            if len(got) == 2:
                return

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", agent_id="agt_1"))
    await bus.emit(_ev(id="e2", agent_id="agt_2"))
    await bus.emit(_ev(id="e3", agent_id="agt_1"))
    await asyncio.wait_for(task, timeout=2.0)

    assert got == ["e1", "e3"]
