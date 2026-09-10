"""事件总线的进程内实现。

协议在 `ctx_weft.protocols.events`；本模块只是它的一个实现——按 spec 2026-08-27
的三层划界（契约 protocols / 实现 providers / 编排 core），实现归这里。

⚠️ 进程内实现：不跨进程。host 要多进程部署须换 Redis Streams 等外部总线，
实现同一个 `EventBus` 协议即可。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from ctx_weft.protocols.events import Event, EventBus, EventFilter, SubscriptionHandle

logger = logging.getLogger(__name__)


# ── In-process implementation ─────────────────────────────────────────────────


@dataclass
class _Subscriber:
    """每个订阅者一条独立 queue。

    ``provisional``：True 表示「进程内状态机」类订阅者——未提交窗口里的事件照收不误
    （见 `EventBus` 的类 docstring）。默认 False = 只收已提交的。
    """

    id: str
    queue: asyncio.Queue[Event]
    handler: Callable[[Event], Awaitable[None]] | None
    filter: EventFilter
    dropped: int = 0
    provisional: bool = False


class InProcessEventBus(EventBus):
    """单进程 asyncio.Queue 实现，独立 queue + 背压丢弃旧事件。"""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, _Subscriber] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        #: task_id → 未提交窗口里按序攒下的事件（见 `EventBus` 类 docstring）。
        #: 键存在即「窗口开着」，空 list 与不存在语义不同，勿用真值判断。
        self._provisional: dict[str, list[Event]] = {}

    async def emit(self, event: Event) -> None:
        task_id = getattr(event, "task_id", "") or ""
        buffered = self._provisional.get(task_id) if task_id else None
        if buffered is not None:
            # 未提交窗口：进程内状态机立刻看到，其余人等提交。顺序由这条 list 保住——
            # 提交时原样重放，`TASK_CREATED` 先于 `TASK_STARTED` 这类次序不会被打乱。
            buffered.append(event)
            await self._fanout(event, to_provisional=True, to_rest=False)
            return
        await self._fanout(event, to_provisional=True, to_rest=True)

    async def _fanout(
        self, event: Event, *, to_provisional: bool, to_rest: bool,
    ) -> None:
        """把一条事件投给选定的那一类订阅者。

        两趟的分工与改造前一致：先 `put_nowait` 到每条队列（`stream()` 订阅者靠它），
        再 drain handler 订阅者。**drain 只拉订阅者自己队列里的东西**，所以未提交窗口
        里没收到事件的订阅者不会在这一趟被误喂。
        """
        targets = [
            sub for sub in self._subscribers.values()
            if (sub.provisional and to_provisional) or (not sub.provisional and to_rest)
        ]

        # 向每个匹配的订阅者投递
        for sub in targets:
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
        for sub in targets:
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
        *,
        provisional: bool = False,
    ) -> SubscriptionHandle:
        """``provisional=True``：未提交窗口里的事件也照收（进程内状态机专用）。

        默认 False 是刻意的——落盘与出会话边界的订阅者必须显式地**不**声明它，
        新增的订阅者忘了传参数时落在安全的一侧（看不到未提交的东西）。
        """
        self._next_id += 1
        sub_id = f"sub_{self._next_id}"
        sub = _Subscriber(
            id=sub_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            handler=handler,
            filter=EventFilter(types=[event_type] if event_type else None),
            provisional=provisional,
        )
        self._subscribers[sub_id] = sub
        return SubscriptionHandle(subscriber_id=sub_id, _bus=self)

    # ── 未提交窗口 ────────────────────────────────────────────────────────────

    def begin_provisional(self, task_id: str) -> None:
        if not task_id:
            return
        self._provisional.setdefault(task_id, [])

    async def commit_provisional(self, task_id: str) -> None:
        buffered = self._provisional.pop(task_id, None)
        if not buffered:
            return
        # provisional 订阅者在 emit 那一刻就收过了，这里只补其余人，不重复投递。
        for event in buffered:
            await self._fanout(event, to_provisional=False, to_rest=True)

    def discard_provisional(self, task_id: str) -> None:
        self._provisional.pop(task_id, None)

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
    if filter.agent_id and event.agent_id != filter.agent_id:
        return False
    if filter.types is not None and event.type not in filter.types:
        return False
    return True
