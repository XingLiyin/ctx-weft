"""已知缺陷（H2）：迟提交的旧事件 ID 被「快照 + 增量」恢复永久跳过。

现状：事件 ID 在创建时铸造（ULID）；未提交窗口（`begin_provisional` / `commit_provisional`，
即 TaskManager.begin_round / commit_round 包的那对钩子）里的事件要等提交时才落库，提交顺序
因此可以晚于别的 task 的更新 ID。SnapshotWriter 的游标取触发事件的 ID（snapshot.py `_write`），
增量恢复按 `id > 游标` 过滤（reducers.rebuild_view → `read_after`）——游标之后才落库的
旧 ID 永远不在增量里。

触发条件：宿主开了 `snapshot_every_n > 0`（README / 迁移文档推荐 50），且同一会话里
task A 的窗口未提交期间，task B 的 RunFinished 触发了快照。

本文件断言**应有行为**（快照恢复与全量回放等价），用 `xfail(strict=True)` 标记为已知缺陷；
修好后 XPASS 报红，届时删掉标记。前置条件用 `pytest.fail`（不是 assert）——它们失败说明
夹具本身坏了，不能被 xfail 吞掉。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.core.control.reducers import rebuild_view, reduce_events
from ctx_weft.protocols.events import Event
from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus
from ctx_weft.providers.events.persister import attach_persistence

pytestmark = pytest.mark.asyncio


def _event(n: int, kind: str, *, task_id: str | None = None, payload: dict | None = None) -> Event:
    """ID 单调递增的合成事件（evt_0001 < evt_0002 …），模拟 ULID 时间序。"""
    return Event(
        id=f"evt_{n:04d}", run_id="run_b", sequence=n,
        session_id="s", type=kind, timestamp=datetime.now(UTC),
        task_id=task_id, payload=payload or {},
    )


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="H2 未修复：快照游标按事件 ID 过滤，迟提交的旧 ID 落在游标之外",
)
async def test_late_committed_event_survives_snapshot_recovery():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=1)

    await bus.emit(_event(1, "SessionCreated", payload={"root_agent_id": "root"}))

    # task a 开窗：evt_0002 进缓冲，persister / SnapshotWriter 暂时看不到
    bus.begin_provisional("a")
    await bus.emit(_event(2, "TaskCreated", task_id="a",
                          payload={"task": {"id": "a", "assigned_agent_id": "agent_a"}}))
    # task b 无窗口，即时提交；RunFinished 触发快照（snapshot_every_n=1），游标 = evt_0004
    await bus.emit(_event(3, "TaskCreated", task_id="b",
                          payload={"task": {"id": "b", "assigned_agent_id": "agent_b"}}))
    await bus.emit(_event(4, "RunFinished", task_id="b", payload={"outcome": "completed"}))
    snapshot = await store.load_latest_snapshot("s")
    if snapshot is None or snapshot.last_event_id != "evt_0004":
        pytest.fail(f"fixture broken: expected snapshot at evt_0004, got {snapshot!r}")

    # a 此刻才提交：evt_0002 迟到落库，ID 小于快照游标
    await bus.commit_provisional("a")

    full = reduce_events(await store.read_by_session("s"), "s")
    if sorted(full.tasks) != ["a", "b"]:
        pytest.fail(f"fixture broken: full replay should see a and b, got {sorted(full.tasks)}")

    restored = await rebuild_view(store, "s")
    assert sorted(restored.tasks) == ["a", "b"], (
        f"snapshot recovery dropped late-committed task 'a'; got {sorted(restored.tasks)}"
    )
