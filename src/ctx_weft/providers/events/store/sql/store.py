"""SqlEventStore：SQLAlchemy async 的 EventStore 实现（默认 SQLite，postgres 同源）。

协议面七个方法齐备。与 `providers/events/store/in_memory` 是同一套契约的两个实现，
`tests/unit/test_event_store_conformance.py` 对两者跑同一套用例。

设计要点：

- **活跃判据不自己写。** `list_active_session_ids` 把生命周期事件捞出来，交给
  `providers/events/_lifecycle` 那台**两个实现共用**的状态机重放——见该方法 docstring。
- **`append` 不过滤瞬态事件**（spec 2026-08-29 §6.4）：那是订阅策略，归 `EventPersister`。
  与 `InMemoryEventStore` 同口径，否则一致性套没法用同一份用例跑两边。
- **快照剪枝**：每个 session 只留最新 `keep_snapshots` 张。恢复只取最新一张，
  定期写入会让旧快照无界累积。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx_weft.protocols.events import Event, EventStore, RunSnapshot
from ctx_weft.providers._sqlalchemy import make_session_factory
from ctx_weft.providers.events._lifecycle import (
    LIFECYCLE_EVENT_TYPES,
    replay_lifecycle,
)
from ctx_weft.providers.events.store.sql.models import Base, EventModel, SnapshotModel

logger = logging.getLogger(__name__)

__all__ = ["SqlEventStore", "open_sqlite_event_store"]


class SqlEventStore(EventStore):
    """SQLAlchemy-backed event store。"""

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession]",
        *,
        keep_snapshots: int = 3,
    ) -> None:
        self._factory = session_factory
        self._keep_snapshots = max(1, keep_snapshots)

    # ── 写 ────────────────────────────────────────────────────────────────────

    async def append(self, event: Event) -> None:
        async with self._factory() as db, db.begin():
            db.add(EventModel(
                id=event.id,
                run_id=event.run_id,
                session_id=event.session_id,
                task_id=event.task_id,
                agent_id=event.agent_id,
                tenant_id=event.tenant_id,
                type=event.type,
                sequence=event.sequence,
                payload_json=json.dumps(event.payload),
                metadata_json=json.dumps(event.metadata),
                causation_id=event.causation_id,
                origin=event.origin,
                schema_version=event.schema_version,
                timestamp=event.timestamp,
            ))

    # ── 读 ────────────────────────────────────────────────────────────────────

    async def read_by_session(self, session_id: str) -> list[Event]:
        """按 id（ULID，字典序即时间序）升序返回该 session 的全部事件。"""
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(EventModel.session_id == session_id)
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(
                    EventModel.session_id == session_id,
                    EventModel.id > after_event_id,
                )
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def read_session_events_of_types(
        self, session_id: str, types: "tuple[str, ...]",
    ) -> list[Event]:
        if not types:
            return []
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(
                    EventModel.session_id == session_id,
                    EventModel.type.in_(tuple(str(t) for t in types)),
                )
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def list_active_session_ids(self) -> list[str]:
        """有开启边界、未被终结的 session（崩溃恢复用）。

        **判据不在这里，在 `providers/events/_lifecycle`** ——与 `InMemoryEventStore`
        共用同一台状态机。各写一遍必然分叉，而分叉表现为「重启后某些会话不弹恢复」或
        「已结束的会话反复被恢复」，生产里极难归因。

        **为什么不做成纯 SQL 表达式。** 终态判据藏在 `payload` JSON 里
        （`SessionStatusChanged.payload["new_status"]`），提取要方言分叉
        （SQLite `json_extract` vs Postgres `->>`）。参考宿主那版纯 SQL 判据
        （`max(opened.id) > max(finished.id)`）**完全忽略了 `SessionStatusChanged`**——
        经它终结的会话会被永远报成 active。

        **两步查询的等价性**：先 `SELECT DISTINCT session_id` 把所有出现过的 session 置
        为 active（种子），再按 id 升序重放生命周期事件。`InMemoryEventStore` 是在每个
        session 的**首次出现**处 add 的；由于操作只有 add/discard 且逐 session 独立，
        「在 -∞ 处 add」与「在首个事件处 add」结果完全一致——首个事件必然先于该 session
        的其余事件。

        代价是行数 = 会话数 × 每会话几条生命周期事件，且只在启动时调一次。真的大到不能
        接受时，正确的下一步是加一张 session 状态投影表，**而不是**把判据塞回 SQL 表达式。
        """
        async with self._factory() as db:
            seen = await db.execute(select(EventModel.session_id).distinct())
            session_ids = list(seen.scalars().all())
            lifecycle = await db.execute(
                select(EventModel)
                .where(EventModel.type.in_(LIFECYCLE_EVENT_TYPES))
                .order_by(EventModel.id)
            )
            # 在 session 内就物化成 Event：ORM 行出了 session 就是 detached 实例，
            # 靠「属性已加载所以还能读」是脆的，且与 memory/sql provider 的既有写法不一致。
            events = [_row_to_event(r) for r in lifecycle.scalars().all()]
        return list(replay_lifecycle(session_ids, events))

    # ── 快照 ──────────────────────────────────────────────────────────────────

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        async with self._factory() as db, db.begin():
            db.add(SnapshotModel(
                id=snapshot.id,
                session_id=snapshot.session_id,
                run_id=snapshot.run_id or "",
                last_event_id=snapshot.last_event_id,
                last_event_sequence=snapshot.last_event_sequence,
                state_blob_json=json.dumps(snapshot.state_blob),
                snapshot_reason=snapshot.snapshot_reason,
                created_at=snapshot.snapshot_at,
            ))
            await db.flush()          # 让新行参与下面的「保留最新」排序
            await self._prune_snapshots(db, snapshot.session_id)

    async def _prune_snapshots(self, db: AsyncSession, session_id: str) -> None:
        """删除该 session 除最新 keep_snapshots 张之外的旧快照。

        恢复只取最新一张（`load_latest_snapshot`），逐 RunFinished 定期写入会让旧快照
        无界累积，故每次写入后顺手清理。按 id（ULID，时间可排序）取最新 N 个保留；
        子查询带 LIMIT，Postgres / SQLite 均支持。
        """
        keep_ids = (
            select(SnapshotModel.id)
            .where(SnapshotModel.session_id == session_id)
            .order_by(SnapshotModel.id.desc())
            .limit(self._keep_snapshots)
        )
        await db.execute(
            delete(SnapshotModel).where(
                SnapshotModel.session_id == session_id,
                SnapshotModel.id.not_in(keep_ids),
            )
        )

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        async with self._factory() as db:
            result = await db.execute(
                select(SnapshotModel)
                .where(SnapshotModel.session_id == session_id)
                .order_by(SnapshotModel.created_at.desc(), SnapshotModel.id.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return RunSnapshot(
                id=row.id,
                run_id=row.run_id,
                session_id=row.session_id,
                last_event_id=row.last_event_id,
                last_event_sequence=row.last_event_sequence,
                state_blob=json.loads(row.state_blob_json),
                snapshot_reason=row.snapshot_reason,
                snapshot_at=row.created_at,
            )


def _row_to_event(row: EventModel) -> Event:
    return Event(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        session_id=row.session_id,
        type=row.type,
        timestamp=row.timestamp,
        tenant_id=row.tenant_id,
        task_id=row.task_id,
        agent_id=row.agent_id,
        payload=json.loads(row.payload_json),
        metadata=json.loads(row.metadata_json),
        causation_id=row.causation_id,
        # 存量行该列为 NULL → 回落 ""，与 docs/events-v2.md §0「存量事件读出空串」一致。
        origin=row.origin if row.origin is not None else "",
        # 存量行（参考宿主写的）该列为 NULL → 回落 1，零迁移。
        schema_version=row.schema_version if row.schema_version is not None else 1,
    )


@asynccontextmanager
async def open_sqlite_event_store(
    db_path: str | Path,
    *,
    keep_snapshots: int = 3,
) -> AsyncIterator[SqlEventStore]:
    """开一个 SQLite backed 的 event store（建表 → yield → dispose）。

    测试与单机部署用。宿主接 postgres 时自带 engine / migration，直接构造
    ``SqlEventStore(session_factory)`` 即可，不必走这里。
    """
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield SqlEventStore(factory, keep_snapshots=keep_snapshots)
    finally:
        await engine.dispose()
