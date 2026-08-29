"""EventStore 的单进程内存实现。

协议在 `ctx_weft.protocols.events`；本模块只是它的一个实现（spec 2026-08-27 三层划界）。
线程不安全，仅供开发 / 测试 / 单进程 demo；host 上生产要换 Postgres 等持久实现。
"""

from __future__ import annotations

import asyncio

from ctx_weft.protocols.events import (
    Event,
    EventStore,
    RunSnapshot,
)
from ctx_weft.providers.events._lifecycle import apply_lifecycle


# ── InMemoryEventStore ────────────────────────────────────────────────────────


class InMemoryEventStore(EventStore):
    """单进程内存版。线程不安全，仅供开发/测试/单进程 demo 使用。

    订阅由 `EventPersister` 负责，见 `providers/events/persister.py`。
    """

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}
        self._active: set[str] = set()
        self._snapshots: dict[str, RunSnapshot] = {}
        self._lock = asyncio.Lock()

    async def append(self, event: Event) -> None:
        # **不过滤瞬态事件**（spec 2026-08-29 §6.4）：那是订阅策略，归 EventPersister。
        # 本方法「让存什么就存什么」——一致性测试因此能直接测 append/read 往返。
        async with self._lock:
            sid = event.session_id
            if sid not in self._events:
                self._events[sid] = []
                self._active.add(sid)      # 种子：该 session 出现过即 active
            self._events[sid].append(event)
            apply_lifecycle(self._active, event)

    async def read_by_session(self, session_id: str) -> list[Event]:
        # 排序键是 id（ULID，全局单调），不是 sequence——sequence 只在同一 run_id 内
        # 单调，跨多个 run 的 session 按它排会把两个 run 的事件交错（见协议 docstring）。
        # 生产里这是 no-op：append 顺序即 id 顺序；只对乱序 append 生效。
        return sorted(self._events.get(session_id, []), key=lambda e: e.id)

    async def list_active_session_ids(self) -> list[str]:
        return list(self._active)

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        # 按 id 排序后取字典序严格大于 after_event_id 的部分，与 read_by_session
        # 同一排序键，且不依赖 append 顺序恰好等于 id 顺序。
        events = sorted(self._events.get(session_id, []), key=lambda e: e.id)
        return [ev for ev in events if ev.id > after_event_id]

    async def read_session_events_of_types(
        self, session_id: str, types: tuple[str, ...],
    ) -> list[Event]:
        type_set = set(types)
        events = sorted(self._events.get(session_id, []), key=lambda e: e.id)
        return [ev for ev in events if ev.type in type_set]

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        # 仅保留每个 session 的最新快照——恢复只需最新一条（snapshot + delta replay）。
        async with self._lock:
            self._snapshots[snapshot.session_id] = snapshot

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        return self._snapshots.get(session_id)
