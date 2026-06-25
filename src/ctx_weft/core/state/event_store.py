"""EventStore Protocol + RunSnapshot + InMemoryEventStore。

host 必须实现 append + read_by_session；
快照相关的三个方法为可选扩展——不实现时抛 NotImplementedError，
core 会自动降级为全量 replay。
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ctx_weft.core.events.types import TRANSIENT_EVENT_TYPES, Event

if TYPE_CHECKING:
    from ctx_weft.core.events.bus import EventBus


# ── RunSnapshot ───────────────────────────────────────────────────────────────


@dataclass
class RunSnapshot:
    """事件流的某一时刻快照（供 host 实现 snapshot/restore 优化用）。"""

    id: str
    run_id: str
    session_id: str
    last_event_id: str
    last_event_sequence: int
    state_blob: dict[str, Any]
    snapshot_reason: str = ""
    snapshot_at: datetime | None = None


# ── EventStore Protocol ───────────────────────────────────────────────────────


@runtime_checkable
class EventStore(Protocol):
    """事件流持久化抽象。host 提供具体实现（Postgres / SQLite / in-memory）。"""

    @abstractmethod
    async def append(self, event: Event) -> None:
        """持久化单条事件。"""
        ...

    @abstractmethod
    async def read_by_session(self, session_id: str) -> list[Event]:
        """按 session_id 加载全部事件，按 sequence 排序。"""
        ...

    # ── 可选快照扩展 ──────────────────────────────────────────────────────────
    # 未实现时抛 NotImplementedError；core 捕获后降级为全量 replay。

    async def list_active_session_ids(self) -> list[str]:
        """返回有 SessionCreated 但无终态事件的 session ID 列表（用于启动时 crash recovery）。"""
        raise NotImplementedError

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        """加载 session 中 id > after_event_id 的增量事件（ULID 字典序）。"""
        raise NotImplementedError

    async def read_session_events_of_types(
        self, session_id: str, types: "tuple[str, ...]",
    ) -> list[Event]:
        """只加载 session 中指定类型的事件（按 sequence 排序）。

        轻查询——供恢复决策按事件折叠（如 HITL 待解决判定）而**不必全量回放**。
        未实现时抛 NotImplementedError；调用方降级为 read_by_session + 内存过滤。
        """
        raise NotImplementedError

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        """持久化一个状态快照。"""
        raise NotImplementedError

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        """加载 session 最新快照，无快照时返回 None。"""
        raise NotImplementedError


# ── InMemoryEventStore ────────────────────────────────────────────────────────


_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"})


class InMemoryEventStore(EventStore):
    """单进程内存版。线程不安全，仅供开发/测试/单进程 demo 使用。

    传入 event_bus= 时自动订阅所有事件，无需手动调用 append。
    """

    def __init__(self, event_bus: "EventBus | None" = None) -> None:
        self._events: dict[str, list[Event]] = {}
        self._active: set[str] = set()
        self._snapshots: dict[str, RunSnapshot] = {}
        self._lock = asyncio.Lock()
        # 保存订阅句柄，便于 host 切换到外部 event_store 时注销（见 detach）。
        self._subscription = event_bus.subscribe(None, self.append) if event_bus else None

    async def detach(self) -> None:
        """停止订阅 event_bus。

        host 在构造 runtime 后才异步初始化 DB，会把 runtime.event_store 替换为
        Postgres 实现；若不注销本实例，它会作为孤儿订阅者继续在内存里堆积事件。
        """
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None

    async def append(self, event: Event) -> None:
        # 瞬态 delta（每 token 一个）只为实时流而发，不落存储——否则内存无界堆积、
        # 且会被 read_by_session / reduce_events 全量回放。真相由 LLMResponseFinished 承载。
        if event.type in TRANSIENT_EVENT_TYPES:
            return
        async with self._lock:
            sid = event.session_id
            if sid not in self._events:
                self._events[sid] = []
                self._active.add(sid)
            self._events[sid].append(event)
            t = event.type
            if t == "SessionFinished":
                self._active.discard(sid)
            elif t == "SessionResumed":
                # 多轮会话每轮结束发 SessionFinished、下一条消息发 SessionResumed 重新激活；
                # 故 resume 后须重新计入 active，否则崩溃恢复会漏掉已对话过的会话。
                self._active.add(sid)
            elif t == "SessionStatusChanged":
                new_status = (event.payload or {}).get("new_status", "")
                if new_status in _TERMINAL_STATUSES:
                    self._active.discard(sid)

    async def read_by_session(self, session_id: str) -> list[Event]:
        return list(self._events.get(session_id, []))

    async def list_active_session_ids(self) -> list[str]:
        return list(self._active)

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        events = self._events.get(session_id, [])
        result = []
        found = False
        for ev in events:
            if found:
                result.append(ev)
            elif ev.id == after_event_id:
                found = True
        return result

    async def read_session_events_of_types(
        self, session_id: str, types: tuple[str, ...],
    ) -> list[Event]:
        type_set = set(types)
        return [ev for ev in self._events.get(session_id, []) if ev.type in type_set]

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        # 仅保留每个 session 的最新快照——恢复只需最新一条（snapshot + delta replay）。
        async with self._lock:
            self._snapshots[snapshot.session_id] = snapshot

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        return self._snapshots.get(session_id)
