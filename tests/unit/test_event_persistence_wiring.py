"""EventPersister / attach_persistence 的接线契约（spec 2026-08-29 §6.4）。

订阅策略（丢弃瞬态 delta）归 persister，不归 store——store 变成「让存什么就存什么」，
一致性测试才能直接测 append/read 往返而不被 store 悄悄吃掉测试事件。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.protocols.events import TRANSIENT_EVENT_TYPES, Event
from ctx_weft.providers.events import (
    EventPersister,
    InMemoryEventStore,
    InProcessEventBus,
    attach_persistence,
)


def _ev(type_: str, seq: int = 1, session: str = "s1") -> Event:
    return Event(
        id=f"evt_{seq:04d}",
        run_id="r1",
        sequence=seq,
        session_id=session,
        type=type_,
        timestamp=datetime.now(UTC),
    )


async def test_store_no_longer_accepts_event_bus():
    """自订阅已抽出成 EventPersister——store 不再自己订阅。"""
    with pytest.raises(TypeError):
        InMemoryEventStore(event_bus=InProcessEventBus())


async def test_store_append_no_longer_filters_transient():
    """行为变化（spec §6.4）：过滤是订阅策略，归 persister。"""
    transient = next(iter(TRANSIENT_EVENT_TYPES))
    store = InMemoryEventStore()
    await store.append(_ev(transient))
    assert len(await store.read_by_session("s1")) == 1


async def test_persister_drops_transient():
    transient = next(iter(TRANSIENT_EVENT_TYPES))
    store = InMemoryEventStore()
    p = EventPersister(store)
    await p.on_event(_ev(transient, 1))
    await p.on_event(_ev("SessionCreated", 2))
    stored = await store.read_by_session("s1")
    assert [e.type for e in stored] == ["SessionCreated"]


async def test_persister_subscribes_when_given_a_bus():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    EventPersister(store, bus)
    await bus.emit(_ev("SessionCreated"))
    assert len(await store.read_by_session("s1")) == 1


async def test_persister_swallows_store_errors():
    """append 失败不得掀掉 loop——bus handler 在 emit 里内联执行。"""
    class _Boom:
        async def append(self, event):
            raise RuntimeError("db down")

    p = EventPersister(_Boom())
    await p.on_event(_ev("SessionCreated"))  # 不抛即通过


async def test_detach_stops_receiving():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    p = EventPersister(store, bus)
    await p.detach()
    await bus.emit(_ev("SessionCreated"))
    assert await store.read_by_session("s1") == []


async def test_attach_persistence_wires_persister():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    handle = attach_persistence(bus, store)
    await bus.emit(_ev("SessionCreated"))
    assert len(await store.read_by_session("s1")) == 1
    await handle.detach()
    await bus.emit(_ev("SessionFinished", 2))
    assert len(await store.read_by_session("s1")) == 1
