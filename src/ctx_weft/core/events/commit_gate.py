"""提交门（spec: event-commit；change reliability-wp3，可靠性方案 §4.5/§4.7）。

required 模式的提交确认点：事件先经本门确认存储提交（WP2 的 ``append_batch``），
成功返回带 position 的 StoredEvent，失败先标记会话健康再抛
``PersistenceUnavailableError``——**通知成功从此等价于提交成功**。

位置：编排层（core/），由 Runtime 构造期经 ``EventBus.attach_commit_gate`` 接进
emit 路径；Provider/总线实现不反向依赖本模块。best_effort 模式不接本门（退回
``attach_persistence`` 的旧观察者路径并告警）——本门只有 required 语义，无双实现。
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from ctx_weft.protocols.events import (
    TRANSIENT_EVENT_TYPES,
    Event,
    EventConflictError,
    PersistenceUnavailableError,
    StoredEvent,
    TransientPersistenceError,
)

logger = logging.getLogger(__name__)

__all__ = ["CommitGate"]

# 会话不可用回调：(session_id, 原始异常) → 由 Runtime 侧标记健康表（先标记后抛，
# 保证任何并发路径读到的健康状态与异常一致）。
OnUnavailable = Callable[[str, BaseException], None]


def retryable_storage_error(error: BaseException) -> bool:
    return isinstance(error, (TransientPersistenceError, ConnectionError, TimeoutError))


@dataclass
class _PendingCommit:
    events: tuple[Event, ...]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    records: tuple[StoredEvent, ...] | None = None
    confirmed: asyncio.Event = field(default_factory=asyncio.Event)


class CommitGate:
    """Confirm immutable batches; serialize retries of the same batch only.

    Per-batch locks never enclose event notification. Provider transactions end
    before backoff, and failed/canceled submissions remain available for recovery.
    """

    def __init__(self, event_store: Any, *, on_unavailable: OnUnavailable | None = None,
                 max_attempts: int = 1, base_delay: float = 0.1):
        self._store = event_store
        self._on_unavailable = on_unavailable
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        # Include in-flight submissions: recovery cannot clear isolation while
        # an older concurrent commit still has no receipt.
        self._pending: dict[tuple[str, str], _PendingCommit] = {}

    def has_pending(self, session_id: str) -> bool:
        return any(sid == session_id for sid, _ in self._pending)

    async def confirm_pending(self, session_id: str) -> None:
        for (sid, bid), pending in list(self._pending.items()):
            if sid == session_id:
                await self.commit(list(pending.events), batch_id=bid)

    async def wait_confirmation(self, session_id, batch_id):
        pending = self._pending.get((session_id, batch_id))
        if pending is not None:
            await pending.confirmed.wait()

    def _unavailable(self, session_id, bid, count, exc):
        logger.error(
            "CommitGate: store append failed for session %s batch %s (%d events): %r",
            session_id, bid, count, exc, exc_info=True)
        if self._on_unavailable is not None:
            try:
                self._on_unavailable(session_id, exc)
            except Exception:
                logger.exception("CommitGate: on_unavailable callback failed")

    async def commit(
        self, events: list[Event], *, batch_id: str = "",
    ) -> list[StoredEvent]:
        """确认一批事件的存储提交。

        - 瞬态事件跳过（返回 []）——它们只为实时流而发，不是持久事实；
        - 单事件调用（窗口外 emit）：batch_id 缺省取 event.id（与 ``append`` 兼容键一致）；
        - 批次调用（commit_provisional）：bus 传入 round batch_id，重试不换；
        - 失败先标记隔离；仅识别出的临时故障按预算原样重试，不执行工具；
        - 预算耗尽保留原批次并抛 ``PersistenceUnavailableError``，不通知成功。
        """
        non_transient = [e for e in events if e.type not in TRANSIENT_EVENT_TYPES]
        if not non_transient:
            return []
        session_id = non_transient[0].session_id
        for e in non_transient:
            if e.session_id != session_id:
                raise ValueError(
                    f"CommitGate: batch spans sessions ({session_id!r} vs "
                    f"{e.session_id!r})——一个批次必须同属一个 session")
        bid = batch_id or non_transient[0].id
        key = (session_id, bid)
        pending = self._pending.setdefault(key, _PendingCommit(tuple(deepcopy(non_transient))))
        if pending.events != tuple(non_transient):
            raise EventConflictError(f"Unconfirmed batch {bid!r} cannot be changed")
        async with pending.lock:
            if pending.records is not None:
                return list(pending.records)
            for attempt in range(self._max_attempts):
                try:
                    receipt = await self._store.append_batch(session_id, bid, list(deepcopy(pending.events)))
                except asyncio.CancelledError as exc:
                    self._unavailable(session_id, bid, len(non_transient), exc)
                    raise
                except Exception as exc:
                    self._unavailable(session_id, bid, len(non_transient), exc)
                    if retryable_storage_error(exc) and attempt + 1 < self._max_attempts:
                        await asyncio.sleep(min(self._base_delay * 2 ** attempt, 5.0))
                        continue
                    raise PersistenceUnavailableError(
                        f"event commit not confirmed for session {session_id!r} "
                        f"(batch {bid!r}, {len(non_transient)} events); session enters "
                        f"storage_unavailable isolation") from exc
                pending.records = tuple(receipt.records)
                pending.confirmed.set()
                if self._pending.get(key) is pending:
                    self._pending.pop(key)
                return list(pending.records)
        raise AssertionError("CommitGate max_attempts must be positive")
