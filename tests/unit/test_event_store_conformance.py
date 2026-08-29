"""EventStore 协议一致性测试套（spec 2026-08-29 §8）。

**面向协议、不面向实现。** 每条用例只经 `protocols/events.py` 声明的方法操作 store，
不碰任何实现内部字段（`_events` / `_active` / `_snapshots`）。

═══ 接入点 ═══════════════════════════════════════════════════════════════════
新 store 接进来只需在 `_STORE_FACTORIES` 里加一行 + 一个工厂：

    _STORE_FACTORIES = {
        "in_memory": _make_in_memory,
        "sql": _make_sql,          # ← Task 10 加这一行
    }

工厂签名 `(tmp_path) -> AsyncIterator[EventStore]`（asynccontextmanager）。
═════════════════════════════════════════════════════════════════════════════

本套存在的直接理由：`list_active_session_ids` 的判据决定崩溃恢复捞哪些会话，两个实现
分叉的表现是「重启后某些会话不弹恢复」或「已结束的会话反复被恢复」——生产里极难归因。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.protocols.events import Event, EventStore, RunSnapshot
from ctx_weft.providers.events import InMemoryEventStore


@asynccontextmanager
async def _make_in_memory(tmp_path) -> AsyncIterator[EventStore]:
    yield InMemoryEventStore()


_STORE_FACTORIES = {
    "in_memory": _make_in_memory,
}


@pytest.fixture(params=sorted(_STORE_FACTORIES))
async def store(request, tmp_path) -> AsyncIterator[EventStore]:
    async with _STORE_FACTORIES[request.param](tmp_path) as s:
        yield s


_T0 = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _ev(
    seq: int,
    type_: str = "RunStarted",
    *,
    session: str = "s1",
    payload: dict | None = None,
    **kw,
) -> Event:
    """事件 id 用 ULID 字典序等价的零填充串——read_after 的排序契约靠它。"""
    return Event(
        id=f"evt_{seq:04d}",
        run_id=kw.pop("run_id", "r1"),
        sequence=seq,
        session_id=session,
        type=type_,
        timestamp=_T0 + timedelta(seconds=seq),
        payload=payload or {},
        **kw,
    )


# ── append / read 往返 ───────────────────────────────────────────────────────


async def test_append_read_roundtrip_preserves_every_field(store):
    """逐字段往返。schema_version 与 causation_id 尤其容易在 SQL 映射里被漏掉。"""
    ev = Event(
        id="evt_0001",
        run_id="r7",
        sequence=42,
        session_id="s1",
        type="RunStarted",
        timestamp=_T0,
        tenant_id="tenant-x",
        task_id="t1",
        agent_id="a1",
        payload={"k": "v", "n": 1},
        metadata={"m": True},
        causation_id="evt_0000",
        schema_version=3,
    )
    await store.append(ev)
    (got,) = await store.read_by_session("s1")
    for field in (
        "id", "run_id", "sequence", "session_id", "type", "tenant_id",
        "task_id", "agent_id", "payload", "metadata", "causation_id",
        "schema_version",
    ):
        assert getattr(got, field) == getattr(ev, field), field
    assert got.timestamp == _T0          # 时区保真：naive 回读会让这条恒 False


async def test_read_by_session_is_ordered(store):
    for seq in (3, 1, 2):
        await store.append(_ev(seq))
    assert [e.sequence for e in await store.read_by_session("s1")] == [1, 2, 3]


async def test_read_by_session_isolates_sessions(store):
    await store.append(_ev(1, session="s1"))
    await store.append(_ev(2, session="s2"))
    assert [e.session_id for e in await store.read_by_session("s1")] == ["s1"]


async def test_read_by_session_unknown_returns_empty(store):
    assert await store.read_by_session("nope") == []


# ── read_after ───────────────────────────────────────────────────────────────


async def test_read_after_returns_strictly_later(store):
    for seq in (1, 2, 3):
        await store.append(_ev(seq))
    got = await store.read_after("s1", "evt_0001")
    assert [e.sequence for e in got] == [2, 3]


async def test_read_after_last_event_returns_empty(store):
    await store.append(_ev(1))
    assert await store.read_after("s1", "evt_0001") == []


async def test_read_after_absent_marker_returns_everything_later(store):
    """标记不在 store 里时返回 id 大于它的全部事件，而不是空。

    这条路径是活的：rebuild_view 拿快照的 last_event_id 调 read_after
    （core/control/reducers.py:337）。快照引用了一个不在 store 里的 id 时，
    「静默返回空」会丢掉整段 delta——view 退化成只剩快照，且不可观测。
    协议口径是过滤式的（id > after_event_id），不是从标记处扫描。
    """
    for seq in (1, 2, 3):
        await store.append(_ev(seq))
    got = await store.read_after("s1", "evt_0000")   # 不存在的标记，排在全部之前
    assert [e.sequence for e in got] == [1, 2, 3]


async def test_read_after_absent_marker_sorted_after_all_returns_empty(store):
    """标记不在 store 里、但字典序排在全部事件之后时，过滤式语义自然返回空。"""
    for seq in (1, 2, 3):
        await store.append(_ev(seq))
    got = await store.read_after("s1", "evt_9999")
    assert got == []


# ── read_session_events_of_types ─────────────────────────────────────────────


async def test_read_of_types_filters(store):
    await store.append(_ev(1, "RunStarted"))
    await store.append(_ev(2, "RunFinished"))
    await store.append(_ev(3, "SessionFinished"))
    got = await store.read_session_events_of_types("s1", ("RunFinished", "SessionFinished"))
    assert [e.type for e in got] == ["RunFinished", "SessionFinished"]


async def test_read_of_types_empty_tuple(store):
    await store.append(_ev(1))
    assert await store.read_session_events_of_types("s1", ()) == []


# ── 快照 ─────────────────────────────────────────────────────────────────────


def _snap(sid: str = "s1", *, last_id: str, seq: int, reason: str = "periodic") -> RunSnapshot:
    return RunSnapshot(
        id=f"snp_{last_id}",
        run_id="r1",
        session_id=sid,
        last_event_id=last_id,
        last_event_sequence=seq,
        state_blob={"session_id": sid, "n": seq},
        snapshot_reason=reason,
        snapshot_at=_T0,
    )


async def test_snapshot_roundtrip(store):
    s = _snap(last_id="evt_0005", seq=5)
    await store.save_snapshot(s)
    got = await store.load_latest_snapshot("s1")
    assert got is not None
    for field in (
        "id", "run_id", "session_id", "last_event_id",
        "last_event_sequence", "state_blob", "snapshot_reason",
    ):
        assert getattr(got, field) == getattr(s, field), field


async def test_load_latest_snapshot_returns_newest(store):
    await store.save_snapshot(_snap(last_id="evt_0001", seq=1))
    await store.save_snapshot(_snap(last_id="evt_0009", seq=9))
    got = await store.load_latest_snapshot("s1")
    assert got.last_event_sequence == 9


async def test_load_latest_snapshot_none_when_absent(store):
    assert await store.load_latest_snapshot("s1") is None


# ── list_active_session_ids：会话生命周期状态机 ──────────────────────────────


async def test_active_after_session_created(store):
    await store.append(_ev(1, "SessionCreated"))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_inactive_after_session_finished(store):
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    assert set(await store.list_active_session_ids()) == set()


async def test_reactivated_by_session_resumed(store):
    """多轮会话：每轮结束发 SessionFinished，下一条消息发 SessionResumed。
    不重新计入的话崩溃恢复会漏掉所有已对话过的会话。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "SessionResumed"))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_inactive_after_terminal_status_changed(store):
    """参考实现的纯 SQL 判据完全忽略 SessionStatusChanged——这条钉住它。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionStatusChanged", payload={"new_status": "FAILED"}))
    assert set(await store.list_active_session_ids()) == set()


async def test_non_terminal_status_changed_keeps_active(store):
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionStatusChanged", payload={"new_status": "RUNNING"}))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_non_terminal_status_does_not_resurrect(store):
    """已 finished 的会话不该被一条非终态状态事件重新拉活。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "SessionStatusChanged", payload={"new_status": "RUNNING"}))
    assert set(await store.list_active_session_ids()) == set()


async def test_ordinary_event_does_not_resurrect(store):
    """普通事件既不激活也不停用——只有四类生命周期事件改变活跃性。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "RunStarted"))
    assert set(await store.list_active_session_ids()) == set()


async def test_active_sessions_are_independent(store):
    await store.append(_ev(1, "SessionCreated", session="s1"))
    await store.append(_ev(2, "SessionCreated", session="s2"))
    await store.append(_ev(3, "SessionFinished", session="s1"))
    assert set(await store.list_active_session_ids()) == {"s2"}
