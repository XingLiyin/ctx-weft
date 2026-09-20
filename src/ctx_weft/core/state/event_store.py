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
    from collections.abc import AsyncIterator

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

    #: `replay` 每批多少条。见 `replay` 的 docstring。
    REPLAY_BATCH: int = 2000

    async def replay(self, session_id: str) -> "AsyncIterator[list[Event]]":
        """按 id 升序**分批**产出该会话的全部事件，供全量重放折叠。

        为什么不是 `read_by_session`：它一次性把整条流变成 `list[Event]` 驻留内存——实测
        一条 3 万事件 / 45MB `events` 表的会话约 **130MB 常驻、读一次 3.5 秒**（SQLite
        本地文件；Postgres 走网更慢）。而重放本身是左折叠（`reduce_events` 就是
        `apply_events` 在空 view 上的调用，reducers.py 里两段循环体逐字相同），且 apply 是
        for 循环、可结合，所以
            reduce(0..n) == apply(batch_k, ... apply(batch_1, 空 view))
        分批与整批**逐字段等价**，内存峰值从 O(全部事件) 降到 O(batch)。

        **分批是 store 的事，不是 core 的事。** core 只 `async for batch in store.replay(sid)`
        然后折叠：它既不问「你支不支持分页」，也不替谁挑降级路。从前那个判断写在 core 里
        （`getattr` 探一次 `read_by_session_after`、再 `except NotImplementedError`），
        一个坏设计生出两个分支和两种失败形态——后者上线时炸掉了 6 条宿主测试。游标怎么走、
        一批多大，只有 store 知道。

        默认实现**只能一次性**产出：本协议的必需读法只有 `read_by_session`，没有游标可用，
        所以基类不假装能分批。能分批的 store 自己覆盖它（`InMemoryEventStore` 照 id 切片；
        宿主的 Postgres 实现走 `id > after_id LIMIT` 查询）。

        `REPLAY_BATCH` 取 2000 是实测的权衡点（3 万事件，SQLite 本地）：

            批大小    查询次数    耗时      内存峰值
            一次性        1      3.75s    121.5 MB
             1000       30      6.10s      6.3 MB
             2000       15      5.09s     12.3 MB     ← 取这档
             5000        6      4.58s     30.2 MB
            10000        3      4.47s     60.1 MB

        判据是「内存是硬约束、耗时是软约束」：121MB 单会话在并发恢复下会叠成几百 MB ~ GB，
        那是会崩的；而这条路只在**无快照**时走（罕见），多花一秒用户等得起。数字来自 SQLite
        本地文件——Postgres 每次查询多一个 RTT，真要调就在目标库上照这个方法重测。

        一致切面：分批期间若有新事件写入，最后几批会把它们一并读到。那不是问题——重放的
        口径本就是「读到当下为止」，多读到的是真事实。（本分支的 store 没有 `committed_head`
        这类提交位点可用作上界，所以这里不假装有。）
        """
        events = await self.read_by_session(session_id)
        if events:
            yield events

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

    async def replay(self, session_id: str) -> "AsyncIterator[list[Event]]":
        """真分批产出（覆盖协议默认的一次性）。

        不是为了省内存——这些事件本来就在进程里驻留着。是为了**契约保真**：core 自己的
        重放测试因此跑在真分批路径上，而不是全都退回一次性；而且游标语义照 `id > after_id`
        走，和宿主 Postgres 实现同形，两边不容易分叉。
        """
        after_id = ""
        while True:
            batch = [
                ev for ev in self._events.get(session_id, []) if ev.id > after_id
            ][: self.REPLAY_BATCH]
            if not batch:
                return
            yield batch
            after_id = batch[-1].id

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
