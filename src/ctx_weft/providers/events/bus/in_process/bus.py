"""事件总线的进程内实现。

协议在 `ctx_weft.protocols.events`；本模块只是它的一个实现——按 spec 2026-08-27
的三层划界（契约 protocols / 实现 providers / 编排 core），实现归这里。

⚠️ 进程内实现：不跨进程。host 要多进程部署须换 Redis Streams 等外部总线，
实现同一个 `EventBus` 协议即可。

提交门（spec: event-commit，change reliability-wp3）：``attach_commit_gate`` 之后，
窗口外事件 **先经 gate 确认存储提交、再 fanout**（required 语义）；未提交窗口在
``commit_provisional`` 时按固定批次提交，确认期间追加的事件进入后续批次。
观察者队列溢出的丢弃以 ``EventsDropped`` 元事件通报（payload 带 position，可按
``read_range`` 补读）。``required=True`` 订阅者的 handler 异常穿出 emit——必要状态
消费者的失败不再被吞。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field

from ctx_weft.protocols.events import (
    TRANSIENT_EVENT_TYPES,
    Event,
    EventBus,
    EventFilter,
    EventOrigin,
    EventType,
    SubscriptionHandle,
)

logger = logging.getLogger(__name__)

#: 当前未提交窗口的 key（task_id）。emit 进入窗口路径时设置、fanout drain 期间生效、
#: 结束复原——消费侧回调（如 ALM）同步再 emit 的派生事件据此继承窗口归属，无 task_id
#: 的会话/agent 级派生事件不会逃逸（spec: event-commit，方案 §4.6）。
_current_round: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ctx_weft_event_round", default=None)


# ── In-process implementation ─────────────────────────────────────────────────


@dataclass
class _Subscriber:
    """每个订阅者一条独立 queue。

    ``provisional``：True 表示「进程内状态机」类订阅者——未提交窗口里的事件照收不误
    （见 `EventBus` 的类 docstring）。默认 False = 只收已提交的。

    ``required``：必要状态消费者（spec: event-commit）——handler 异常穿出 emit 而非被
    bus 吞掉；必须搭配 ``provisional=True``（构造期校验）。仍走同步 drain，不经可丢弃
    队列语义（queue 仅作 drain 缓冲）。
    """

    id: str
    queue: asyncio.Queue[Event]
    handler: Callable[[Event], Awaitable[None]] | None
    filter: EventFilter
    dropped: int = 0
    provisional: bool = False
    required: bool = False


@dataclass
class _RoundWindow:
    """An immutable pending batch plus later events awaiting their own batch ID."""

    batch_id: str
    events: list[Event]
    independent_runs: frozenset[str] = frozenset()
    frozen: tuple[Event, ...] = ()
    positions: dict[str, int] | None = None
    notified: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = None


class InProcessEventBus(EventBus):
    """单进程 asyncio.Queue 实现，独立 queue + 背压丢弃旧事件 + 提交门。"""

    def __init__(self, queue_size: int = 1000) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[str, _Subscriber] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        #: task_id → 未提交窗口（见 `EventBus` 类 docstring）。键存在即「窗口开着」。
        self._provisional: dict[str, _RoundWindow] = {}
        # Background producers already started outside a future user-message
        # window keep their own lifetime; retracting that message cannot eat
        # their remaining events (especially RunFinished).
        self._background_runs: dict[str, str] = {}
        self._actor_runs: dict[str, str] = {}
        #: 提交门（required 模式；None = 未接——best_effort 旧路径）
        self._gate = None

    # ── 提交门 ────────────────────────────────────────────────────────────────

    def attach_commit_gate(self, gate) -> None:
        """接入提交门（spec: event-commit）。emit/commit_provisional 先经 gate 确认。"""
        self._gate = gate

    async def emit_batch(self, events, *, on_committed=None) -> None:
        """Commit one immutable, non-retractable lifecycle batch before notification.

        The caller retains the original envelopes until this returns. Retrying uses
        the same first event ID and therefore confirms the same atomic transaction.
        """
        if not events:
            return
        stored = await self._gate.commit(events, batch_id=events[0].id) if self._gate else []
        positions = {row.event.id: row.position for row in stored}
        if on_committed is not None:
            on_committed()
        for event in events:
            await self._fanout(event, to_provisional=True, to_rest=True,
                               position=positions.get(event.id))

    async def emit(self, event: Event) -> None:
        if event.type == EventType.RUN_STARTED and event.origin == EventOrigin.LOOP_BACKGROUND_OBSERVE:
            self._track_background_run(event)
            window = self._provisional.get(event.task_id or "")
            if window is not None:
                window.independent_runs = window.independent_runs | {event.run_id}
        task_id = getattr(event, "task_id", "") or ""
        window_key = task_id
        window = self._provisional.get(task_id) if task_id else None
        if window is None and not task_id:
            # 派生事件常无 task_id：经执行上下文继承当前窗口，不逃逸（方案 §4.6）
            window_key = _current_round.get() or ""
            window = self._provisional.get(window_key) if window_key else None
        if window is not None and event.run_id in window.independent_runs:
            window = None
        if window is not None:
            # 未提交窗口：进程内状态机立刻看到，其余人等提交。顺序由这条 list 保住——
            # 提交时原样重放，`TASK_CREATED` 先于 `TASK_STARTED` 这类次序不会被打乱。
            window.events.append(event)
            token = _current_round.set(window_key)
            try:
                await self._fanout(event, to_provisional=True, to_rest=False)
            finally:
                _current_round.reset(token)
            return
        # 窗口外：required 语义——先确认提交，再对外通知。瞬态事件不逐条确认（gate 过滤）。
        position: int | None = None
        if event.type == EventType.RUN_STARTED and event.origin != EventOrigin.RUNTIME:
            self._track_background_run(event)
        if self._gate is not None and event.type not in TRANSIENT_EVENT_TYPES:
            stored = await self._gate.commit([event])  # 失败 → PersistenceUnavailableError
            position = stored[0].position if stored else None
        if event.type in (EventType.RUN_STARTED, EventType.RUN_FINISHED):
            self._track_background_run(event)
        await self._fanout(event, to_provisional=True, to_rest=True, position=position)

    def _track_background_run(self, event: Event) -> None:
        if event.type == EventType.RUN_STARTED and event.run_id:
            runs = self._actor_runs if event.origin == EventOrigin.RUNTIME else self._background_runs
            runs[event.run_id] = event.task_id or ""
        elif event.type == EventType.RUN_FINISHED:
            self._background_runs.pop(event.run_id, None)
            self._actor_runs.pop(event.run_id, None)

    async def _fanout(
        self, event: Event, *, to_provisional: bool, to_rest: bool,
        position: int | None = None,
    ) -> None:
        """把一条事件投给选定的那一类订阅者。

        两趟的分工与改造前一致：先 `put_nowait` 到每条队列（`stream()` 订阅者靠它），
        再 drain handler 订阅者。**drain 只拉订阅者自己队列里的东西**，所以未提交窗口
        里没收到事件的订阅者不会在这一趟被误喂。

        ``position``：本事件的提交位置（gate 路径）。观察者队列溢出丢弃时据此发
        `EventsDropped`（payload 带补读锚点）；无 gate（best_effort）回落纯计数旧行为。
        """
        targets = [
            sub for sub in self._subscribers.values()
            if (sub.provisional and to_provisional) or (not sub.provisional and to_rest)
        ]

        # ── 第一相：必要消费者（spec: event-commit）────────────────────────────
        # required 订阅者先投先 drain——它抛错时观察者还没收到任何东西（committed 通知
        # 不流出），异常穿出 emit 让会话进健康故障。
        for sub in targets:
            if not sub.required:
                continue
            if not _matches(event, sub.filter):
                continue
            sub.queue.put_nowait(event)
            while True:
                try:
                    ev = sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                try:
                    await sub.handler(ev)
                except Exception:
                    logger.exception("EventBus required handler raised for subscriber %s", sub.id)
                    raise

        # ── 第二相：观察者 ─────────────────────────────────────────────────────
        # 向每个匹配的观察者投递（溢出丢弃 + EventsDropped）
        for sub in targets:
            if sub.required:
                continue
            if not _matches(event, sub.filter):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 背压：丢弃最旧事件；gate 路径下追加 EventsDropped 元事件（可按 position 补读）
                try:
                    _ = sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                sub.dropped += 1
                logger.warning(
                    "EventBus subscriber %s queue full; dropped %d events so far",
                    sub.id, sub.dropped,
                )
                try:
                    sub.queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass
                if position is not None:
                    # 元事件比队列里最旧的一条真事件更值钱（它承载补读锚点）——塞不下时
                    # 再丢一条最旧的给它腾位；连这也失败只剩 warning 日志兜底。
                    #
                    # 封套字段逐个照抄触发它的那条事件，**`tenant_id` 也在内**：
                    # `Event.tenant_id` 有缺省值 `"default"`，漏抄一个就把这条元事件归到
                    # 了别的租户。它是瞬态、不落库，但按 tenant 分流的订阅者照样会分错。
                    try:
                        sub.queue.put_nowait(Event(
                            id=f"evt_dropped_{sub.id}_{position}",
                            run_id=event.run_id, sequence=0,
                            session_id=event.session_id, type=EventType.EVENTS_DROPPED,
                            timestamp=event.timestamp, task_id=event.task_id,
                            agent_id=event.agent_id, tenant_id=event.tenant_id,
                            payload={"subscriber_id": sub.id, "position": position,
                                     "dropped": sub.dropped},
                        ))
                    except asyncio.QueueFull:
                        try:
                            sub.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            sub.queue.put_nowait(Event(
                                id=f"evt_dropped_{sub.id}_{position}",
                                run_id=event.run_id, sequence=0,
                                session_id=event.session_id, type=EventType.EVENTS_DROPPED,
                                timestamp=event.timestamp, task_id=event.task_id,
                                agent_id=event.agent_id, tenant_id=event.tenant_id,
                                payload={"subscriber_id": sub.id, "position": position,
                                         "dropped": sub.dropped},
                            ))
                        except asyncio.QueueFull:
                            pass  # 极端 churn：只剩 warning 日志兜底

        # 观察者 handler 的 drain（异常照旧吞——观察者失败不阻塞必要状态推进）。
        for sub in targets:
            if sub.required or sub.handler is None:
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
        required: bool = False,
    ) -> SubscriptionHandle:
        """``provisional=True``：未提交窗口里的事件也照收（进程内状态机专用）。

        默认 False 是刻意的——落盘与出会话边界的订阅者必须显式地**不**声明它，
        新增的订阅者忘了传参数时落在安全的一侧（看不到未提交的东西）。

        ``required=True``：必要状态消费者（spec: event-commit）——异常穿出 emit；
        必须搭配 ``provisional=True``（必要消费者要看推测态，pause/cancel 依赖）。
        """
        if required and not provisional:
            raise ValueError("required subscriber must also be provisional=True")
        self._next_id += 1
        sub_id = f"sub_{self._next_id}"
        sub = _Subscriber(
            id=sub_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            handler=handler,
            filter=EventFilter(types=[event_type] if event_type else None),
            provisional=provisional,
            required=required,
        )
        self._subscribers[sub_id] = sub
        return SubscriptionHandle(subscriber_id=sub_id, _bus=self)

    # ── 未提交窗口 ────────────────────────────────────────────────────────────

    def begin_provisional(self, task_id: str, *, batch_id: str = "") -> None:
        """开窗。``batch_id``：round 级批次号（TM 生成透传；空则提交时按首事件推导，
        重试不换——WP2 幂等的前提）。

        幂等——重复开窗不清空已有缓冲（`retry` 重排会重进同一条路径）。
        """
        if not task_id:
            return
        self._provisional.setdefault(task_id, _RoundWindow(batch_id=batch_id, events=[],
            independent_runs=frozenset(rid for rid, tid in
                (*self._background_runs.items(), *self._actor_runs.items()) if tid == task_id)))

    def bind_provisional_run(self, task_id: str, run_id: str) -> None:
        window = self._provisional.get(task_id)
        if window is not None:
            window.independent_runs = window.independent_runs - {run_id}

    def is_run_provisional(self, task_id: str, run_id: str) -> bool:
        window = self._provisional.get(task_id)
        return window is not None and run_id not in window.independent_runs

    async def commit_provisional(self, task_id: str) -> None:
        """Confirm fixed batches in order, then close the window.

        An uncertain batch keeps its ID and copied envelopes. Later emissions
        remain buffered separately. Provisional subscribers already saw them;
        only confirmed batches are delivered to ordinary subscribers.
        """
        window = self._provisional.get(task_id)
        if window is None or window.owner is asyncio.current_task():
            return
        async with window.lock:
            window.owner = asyncio.current_task()
            try:
                while self._provisional.get(task_id) is window:
                    if not window.frozen:
                        if not window.events:
                            self._provisional.pop(task_id, None)
                            return
                        window.frozen = tuple(deepcopy(window.events))
                        window.events.clear()
                        if not window.batch_id:
                            window.batch_id = f"round:{task_id}:{window.frozen[0].id}"
                    if window.positions is None:
                        stored = (await self._gate.commit(list(window.frozen), batch_id=window.batch_id)
                                  if self._gate is not None else [])
                        window.positions = {se.event.id: se.position for se in stored}
                    while window.notified < len(window.frozen):
                        event = window.frozen[window.notified]
                        self._track_background_run(event)
                        await self._fanout(event, to_provisional=False, to_rest=True,
                                           position=window.positions.get(event.id))
                        window.notified += 1
                    # New events belong to a new immutable batch. Keep the window
                    # visible throughout I/O so additions cannot escape ahead of it.
                    window.frozen = ()
                    window.batch_id = ""
                    window.positions = None
                    window.notified = 0
            finally:
                window.owner = None

    def has_pending_commit(self, task_id: str) -> bool:
        window = self._provisional.get(task_id)
        return window is not None and bool(window.frozen)

    def discard_provisional(self, task_id: str) -> None:
        """关窗并丢弃缓冲——这一轮当作没发生过。未开窗时 no-op。

        返回受影响窗口里出现过的 agent_id 集合由 ``collect_round_agents`` 在丢弃**前**
        调用方完成（bus 不回调宿主）——保持 bus 无宿主依赖。
        """
        self._provisional.pop(task_id, None)

    def round_agents(self, task_id: str) -> set[str]:
        """当前（或最近一次）窗口缓冲里出现过的 agent_id 集合——discard 前取样，
        供编排层做受影响 agent 的重聚合（spec: event-commit）。"""
        window = self._provisional.get(task_id)
        if window is None:
            return set()
        return {e.agent_id for e in (*window.frozen, *window.events) if getattr(e, "agent_id", None)}

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
