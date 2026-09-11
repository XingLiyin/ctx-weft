"""WP0 基线夹具（H2）：延迟提交的旧事件 ID 被快照增量恢复跳过。

钉住 2026-09-11 可靠性方案 H2 的**缺陷现状**（探针 verify_agent_architecture.py
`late_commit` 的 pytest 移植；上游 docs/plans/2026-09-11-agent-core-reliability-
plan.md §1.2/§4.1/§4.8）：快照游标取触发事件 ID（snapshot.py `_write`），增量恢复
按事件 ID 过滤（reducers.rebuild_view `read_after`）；事件 ID 在创建时铸造（ULID），
提交顺序可与之不同（并发未提交窗口）——后提交的旧 ID 永久落在快照游标之外。

⚠️ 本文件断言的是**旧契约**（缺陷行为），供 WP4（position 游标 + 一致切面）实施时
**有意翻转**：翻转后全量回放与 snapshot(C)+delta(>C) 等价，两条恢复路径都见 a、b。

时序说明：begin/commit_provisional 正是 TaskManager.begin_round/commit_round 包装的
同一对 bus 钩子（orchestrator/task/manager.py）——这里直接驱动 bus 即驱动了 round
生命周期，交错顺序由代码顺序确定性保证，不需要 barrier。
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


async def test_late_committed_event_is_skipped_by_snapshot_recovery():
    """旧契约锚：B 触发快照后 A 才提交 → 快照恢复只见 b，全量回放见 a、b。"""
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=1)

    # 会话基线（已提交）
    await bus.emit(_event(1, "SessionCreated", payload={"root_agent_id": "root"}))

    # task a 开未提交窗口：其后 a 的事件进入缓冲（persister/SnapshotWriter 不可见）
    bus.begin_provisional("a")
    await bus.emit(_event(2, "TaskCreated", task_id="a",
                          payload={"task": {"id": "a", "assigned_agent_id": "agent_a"}}))
    # task b 无窗口：事件即时提交；RunFinished 触发快照（snapshot_every_n=1）
    # ——此刻 store 里没有 evt_0002，快照内容只含 b，游标 = evt_0004
    await bus.emit(_event(3, "TaskCreated", task_id="b",
                          payload={"task": {"id": "b", "assigned_agent_id": "agent_b"}}))
    await bus.emit(_event(4, "RunFinished", task_id="b", payload={"outcome": "completed"}))
    snapshot = await store.load_latest_snapshot("s")
    assert snapshot is not None and snapshot.last_event_id == "evt_0004"

    # a 的窗口此刻才提交：evt_0002 迟到落库（ID < 快照游标）
    await bus.commit_provisional("a")

    # 全量回放：a、b 都在（日志里 evt_0002 确实存在）
    full = reduce_events(await store.read_by_session("s"), "s")
    assert sorted(full.tasks) == ["a", "b"], (
        f"full replay must see both tasks; got {sorted(full.tasks)}"
    )

    # 快照增量恢复（缺陷）：read_after(evt_0004) 永远排除 evt_0002 → a 丢失
    restored = await rebuild_view(store, "s")
    assert sorted(restored.tasks) == ["b"], (
        "OLD CONTRACT PIN: snapshot recovery currently DROPS late-committed task 'a' "
        f"(expected defect: only ['b']); got {sorted(restored.tasks)}. "
        "WP4 (position-based cursor + consistent cut) must flip this to ['a', 'b']."
    )
