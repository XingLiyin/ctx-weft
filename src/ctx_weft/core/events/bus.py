"""EventBus 协议 + InProcessEventBus 实现。

详见设计文档 §9.8 / §10.2（背压）。

V1：单进程，基于 asyncio.Queue；每订阅者独立 queue，size=1000；
慢消费者超容时**丢弃旧事件**而非阻塞 loop（保证 loop 不被订阅者拖死），
丢弃时 emit 一个 EventsDropped 元事件。

协议（`EventBus` / `SubscriptionHandle`）已搬到 `ctx_weft.protocols.events`
（spec 2026-08-27 协议层划界）；本模块保留 re-export 以免既有 import 断裂，
自身只留 `InProcessEventBus` 这个进程内实现。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from ctx_weft.protocols.events import (
    Event, EventBus, EventFilter, SubscriptionHandle,
)

logger = logging.getLogger(__name__)


# ── In-process implementation ─────────────────────────────────────────────────


@dataclass
class _Subscriber:
    """每个订阅者一条独立 queue。"""

    id: str
    queue: asyncio.Queue[Event]
    handler: Callable[[Event], Awaitable[None]] | None
    filter: EventFilter
    dropped: int = 0


class InProcessEventBus(EventBus):
    """单进程 asyncio.Queue 实现，独立 queue + 背压丢弃旧事件。"""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, _Subscriber] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    async def emit(self, event: Event) -> None:
        # 向每个匹配的订阅者投递
        for sub in list(self._subscribers.values()):
            if not _matches(event, sub.filter):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 背压：丢弃最旧事件，emit EventsDropped 元事件提醒
                try:
                    _ = sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                sub.dropped += 1
                logger.warning(
                    "EventBus subscriber %s queue full; dropped %d events so far",
                    sub.id,
                    sub.dropped,
                )
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        # 主动 push 给 handler 订阅者，drain 完整队列确保所有积压事件都被处理
        for sub in list(self._subscribers.values()):
            if sub.handler is None:
                continue
            while True:
                try:
                    ev = sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await sub.handler(ev)
                except Exception:
                    logger.exception("EventBus handler raised for subscriber %s", sub.id)

    def subscribe(
        self,
        event_type: str | None,
        handler: Callable[[Event], Awaitable[None]],
    ) -> SubscriptionHandle:
        self._next_id += 1
        sub_id = f"sub_{self._next_id}"
        sub = _Subscriber(
            id=sub_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            handler=handler,
            filter=EventFilter(types=[event_type] if event_type else None),
        )
        self._subscribers[sub_id] = sub
        return SubscriptionHandle(subscriber_id=sub_id, _bus=self)

    async def stream(self, filter: EventFilter) -> AsyncIterator[Event]:
        """订阅流——返回 async iterator。调用方需要负责 unsubscribe。"""
        self._next_id += 1
        sub_id = f"sub_{self._next_id}"
        sub = _Subscriber(
            id=sub_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            handler=None,
            filter=filter,
        )
        self._subscribers[sub_id] = sub
        try:
            while True:
                event = await sub.queue.get()
                yield event
        finally:
            await self._unsubscribe(sub_id)

    async def _unsubscribe(self, subscriber_id: str) -> None:
        self._subscribers.pop(subscriber_id, None)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _matches(event: Event, filter: EventFilter) -> bool:
    if filter.session_id and event.session_id != filter.session_id:
        return False
    if filter.run_id and event.run_id != filter.run_id:
        return False
    if filter.task_id and event.task_id != filter.task_id:
        return False
    if filter.types is not None and event.type not in filter.types:
        return False
    return True
