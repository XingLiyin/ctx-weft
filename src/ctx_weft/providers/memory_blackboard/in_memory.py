"""InMemoryMemoryProvider：单进程内存版 MemoryProvider，主要用于单测 + Phase 1 集成测试。

实现 §4.3 完整协议；recall_semantic 返空（V1 默认 StructuredBlackboard 行为）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ctx_weft.protocols import (
    EVENT_LAYER,
    CompactResult,
    MemoryEvent,
    MemoryEventType,
    MemoryLayer,
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecord,
    MemoryScope,
    ProviderContext,
    Subscription,
)


@dataclass
class _StoredEvent:
    """内存中存的一条事件。"""

    id: str
    event: MemoryEvent
    seq_no: int  # per-(agent_id, scope) 单调递增
    topic_seq_no: int  # per-topic 单调递增（None topic 不算）
    is_superseded: bool = False


class InMemoryMemoryProvider(MemoryProvider):
    """单进程内存版。线程不安全，仅供单测/单进程 demo 使用。"""

    name = "in_memory"

    def __init__(self) -> None:
        self._events: list[_StoredEvent] = []
        self._seq_counters: dict[str, int] = {}  # (tenant, session, agent) → seq
        self._topic_seq: dict[str, int] = {}  # topic → max seq
        self._subscriptions: dict[tuple[str, str, str], Subscription] = {}  # (session_id, task_id, topic) → sub
        self._next_id = 0
        self._lock = asyncio.Lock()

    # ── Ingestion ────────────────────────────────────────────────────────────

    async def ingest(
        self,
        event: MemoryEvent,
        ctx: ProviderContext,
    ) -> str:
        async with self._lock:
            if event.id is not None:
                # id 契约：已存在（含 superseded）= no-op，不比对内容、不推进计数器
                if any(s.id == event.id for s in self._events):
                    return event.id
                event_id = event.id
            else:
                self._next_id += 1
                event_id = f"mev_{self._next_id:08d}"

            layer = EVENT_LAYER[event.type]
            scope_key = self._scope_key(event.scope, ctx.tenant_id, layer)
            self._seq_counters[scope_key] = self._seq_counters.get(scope_key, 0) + 1
            seq_no = self._seq_counters[scope_key]

            topic_seq = 0
            if event.topic:
                # 覆盖语义：同 topic 的旧 BLACKBOARD_PUBLISH 标记 superseded，只保留最新一条
                if event.type == MemoryEventType.BLACKBOARD_PUBLISH:
                    for s in self._events:
                        if (not s.is_superseded
                                and s.event.topic == event.topic
                                and s.event.type == MemoryEventType.BLACKBOARD_PUBLISH):
                            s.is_superseded = True
                self._topic_seq[event.topic] = self._topic_seq.get(event.topic, 0) + 1
                topic_seq = self._topic_seq[event.topic]

            stored = _StoredEvent(
                id=event_id,
                event=event,
                seq_no=seq_no,
                topic_seq_no=topic_seq,
            )
            self._events.append(stored)
            return event_id

    # ── Recall ────────────────────────────────────────────────────────────────

    async def recall_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        type_set = set(types)
        # 目标 scope_key 按层预算（types 同层是常态；过渡期宽容跨层：各层各自匹配后归并）
        target_keys = {
            lyr: self._scope_key(scope, ctx.tenant_id, lyr)
            for lyr in {EVENT_LAYER[t] for t in type_set}
        }
        matching: list[_StoredEvent] = []
        for stored in self._events:
            if stored.is_superseded:
                continue
            if stored.event.type not in type_set:
                continue
            lyr = EVENT_LAYER[stored.event.type]
            if self._scope_key(stored.event.scope, ctx.tenant_id, lyr) != target_keys[lyr]:
                continue
            matching.append(stored)

        # 跨层用 timestamp 归并（同层即 seq 序）；newest-first 返回，limit 截最近 N
        matching.sort(key=lambda s: s.event.timestamp)
        recent = matching[-limit:] if limit and limit > 0 else matching
        return [self._to_record(s) for s in reversed(recent)]

    async def recall_recent_by_agent(
        self,
        agent_scope: MemoryScope,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        type_set = set(types)
        aid = agent_scope.agent_id
        matching = [
            s for s in self._events
            if not s.is_superseded
            and s.event.type in type_set
            and s.event.scope.session_id == agent_scope.session_id
            and s.event.scope.agent_id == aid
            and EVENT_LAYER[s.event.type] is MemoryLayer.TASK
        ]
        matching.sort(key=lambda s: s.event.timestamp)
        recent = matching[-limit:] if limit and limit > 0 else matching
        return [self._to_record(s) for s in reversed(recent)]

    async def recall_topic(
        self,
        topic: str,
        since: int,
        ctx: ProviderContext,
    ) -> tuple[list[MemoryRecord], int]:
        matching = [
            s for s in self._events
            if s.event.topic == topic and s.topic_seq_no > since and not s.is_superseded
        ]
        matching.sort(key=lambda s: s.topic_seq_no)
        records = [self._to_record(s) for s in matching]
        new_cursor = matching[-1].topic_seq_no if matching else since
        return records, new_cursor

    async def recall_semantic(
        self,
        query: str,
        scope: MemoryScope,
        top_k: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        # InMemory 不支持 semantic recall（声明 supports_semantic=False）
        return []

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def subscribe_topic(
        self,
        session_id: str,
        topic: str,
        intent: str,
        ctx: ProviderContext,
        task_id: str = "",
    ) -> str:
        key = (session_id, task_id, topic)
        # 幂等：已存在则保留原订阅（含 cursor），不重置
        if key not in self._subscriptions:
            self._subscriptions[key] = Subscription(
                session_id=session_id,
                topic=topic,
                cursor=0,
                intent=intent,  # type: ignore[arg-type]
                task_id=task_id,
            )
        return f"sub_{session_id}_{task_id}_{topic}"

    async def list_subscriptions(
        self,
        session_id: str,
        ctx: ProviderContext,
        task_id: str | None = None,
    ) -> list[Subscription]:
        out: list[Subscription] = []
        for (sid, tid, _t), s in self._subscriptions.items():
            if sid != session_id:
                continue
            # task_id 给定时：只返回该 task 的订阅 + session 级订阅（tid == ""）
            if task_id is not None and tid != task_id and tid != "":
                continue
            out.append(s)
        return out

    # ── Compact ───────────────────────────────────────────────────────────────

    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
        since_last: MemoryEventType | None = None,
    ) -> CompactResult:
        scope_key = self._scope_key(scope, ctx.tenant_id, layer)

        def _in_scope(s: _StoredEvent) -> bool:
            return (
                not s.is_superseded
                and EVENT_LAYER[s.event.type] is layer
                and self._scope_key(s.event.scope, ctx.tenant_id, layer) == scope_key
            )

        active = [s for s in self._events if _in_scope(s)]
        events_before = len(active)
        # 全函数统一按**渲染序** (timestamp, seq_no) 排（与 recall/装配一致；2026-07-21）。
        # 不能按 seq_no：L3 坍缩 UP 等「timestamp 回填、seq 最高」的记录会在 seq 序里
        # 排到所有 raw 之后——段界被推到末尾 → 归档池空 → 摘要照写而 raw 一条不折；
        # 锚点判定同理会把摘要错插到坍缩 UP 之前。
        _key = lambda s: (s.event.timestamp, s.seq_no)  # noqa: E731
        active.sort(key=_key)

        # since_last：归档池限定在「最后一条 active 该类型记录之后」（段作用域折叠，
        # 2026-07-21）。短段免折残留的更早 raw 落在该点之前 → 永不跨段折入本摘要；
        # 且被折区从该点之后起算 → 锚点走「段尾」分支，不会抢到前一条 UP 之前。
        # 该类型记录不存在 → 不限定（整 scope 照旧）。
        pool = active
        if since_last is not None:
            boundary_idx = next(
                (i for i in range(len(active) - 1, -1, -1)
                 if active[i].event.type is since_last),
                None,
            )
            if boundary_idx is not None:
                pool = active[boundary_idx + 1:]

        # protect_types 永不进 archive；keep_last 只对可折类型计
        archivable = [s for s in pool if s.event.type not in protect_types]
        to_archive = archivable[:-keep_last] if keep_last > 0 else archivable
        for s in to_archive:
            s.is_superseded = True

        summary_type = (
            MemoryEventType.TASK_COMPACT_SUMMARY
            if layer is MemoryLayer.TASK
            else MemoryEventType.AGENT_COMPACT_SUMMARY
        )
        # 摘要落在「被折区块之后、其后第一条幸存事件之前」→ [UP1][summary][UP2][kept]
        # 找「归档起点」：第一条被折事件的渲染位；摘要插在该起点之后第一条幸存事件之前
        archived_min_key = min((_key(s) for s in to_archive), default=None)
        # 第一条幸存且渲染位 >= 归档起点的事件即为 anchor
        following = ([] if archived_min_key is None
                     else [s for s in active if _key(s) >= archived_min_key and not s.is_superseded])
        if following:
            anchor = min(following, key=lambda s: (s.event.timestamp, s.seq_no))
            summary_ts = anchor.event.timestamp - timedelta(microseconds=1)
            summary_seq = anchor.seq_no - 1
        elif to_archive:
            # 段尾无后继幸存事件（典型：单段 plain_text 折叠 [UP, LLM]）→ 锚到被折段最后一条
            # 事件的位置，**不用 now()**。否则脱管的后台 observe 迟到收尾时，now() 可能晚于同刻
            # 注入的下一轮 USER_PROMPT，摘要越到新消息之后 → 下一轮装配误判为「续跑」（尾部非
            # user）并拼 continue cue、埋掉新消息（多轮对话空白回复 bug）。锚在原段时间位置后，
            # 后到的 USER_PROMPT（now_utc 更晚）天然排在其后。
            last = max(to_archive, key=lambda s: (s.event.timestamp, s.seq_no))
            summary_ts = last.event.timestamp
            summary_seq = last.seq_no
        else:
            summary_ts = datetime.now(UTC)
            self._seq_counters[scope_key] = self._seq_counters.get(scope_key, 0) + 1
            summary_seq = self._seq_counters[scope_key]

        # task 层段摘要 = LLM 对前段的自述（role=assistant）；agent 层折叠摘要是 prompt
        # 首条、Anthropic 首条 assistant 会 400，故保持 role=user。
        summary_role = "assistant" if layer is MemoryLayer.TASK else "user"
        compact_event = MemoryEvent(
            type=summary_type,
            scope=scope,
            content=summary,
            timestamp=summary_ts,
            role=summary_role,
            metadata={"keep_last": keep_last, "archived_count": len(to_archive)},
        )
        async with self._lock:
            self._next_id += 1
            compact_id = f"mev_{self._next_id:08d}"
            self._events.append(_StoredEvent(
                id=compact_id, event=compact_event, seq_no=summary_seq, topic_seq_no=0,
            ))

        events_after = sum(1 for s in self._events if _in_scope(s))
        return CompactResult(
            events_before=events_before,
            events_after=events_after,
            summary_event_id=compact_id,
        )

    async def supersede(
        self,
        event_ids: list[str],
        ctx: ProviderContext,
    ) -> int:
        wanted = set(event_ids)
        n = 0
        async with self._lock:
            for s in self._events:
                if s.id in wanted and not s.is_superseded:
                    s.is_superseded = True
                    n += 1
        return n

    # ── Utilities ─────────────────────────────────────────────────────────────

    async def count_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        ctx: ProviderContext,
    ) -> int:
        type_set = set(types)
        target_keys = {
            lyr: self._scope_key(scope, ctx.tenant_id, lyr)
            for lyr in {EVENT_LAYER[t] for t in type_set}
        }
        count = 0
        for stored in self._events:
            if stored.is_superseded:
                continue
            if stored.event.type not in type_set:
                continue
            lyr = EVENT_LAYER[stored.event.type]
            if self._scope_key(stored.event.scope, ctx.tenant_id, lyr) != target_keys[lyr]:
                continue
            count += 1
        return count

    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo:
        return MemoryProviderInfo(
            name=self.name,
            supports_semantic=False,
            supports_topic=True,
            supports_compact_archival=True,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scope_key(self, scope: MemoryScope, tenant_id: str, layer: MemoryLayer) -> str:
        if layer is MemoryLayer.TASK:
            return f"{tenant_id}|{scope.session_id}|task|{scope.task_id or ''}"
        if layer is MemoryLayer.AGENT:
            return f"{tenant_id}|{scope.session_id}|agent|{scope.agent_id or ''}"
        return f"{tenant_id}|{scope.session_id}|session"

    def _to_record(self, stored: _StoredEvent) -> MemoryRecord:
        return MemoryRecord(
            id=stored.id,
            type=stored.event.type,
            content=stored.event.content,
            timestamp=stored.event.timestamp,
            role=stored.event.role,
            topic=stored.event.topic,
            metadata={
                **stored.event.metadata,
                "seq_no": stored.seq_no,
                "topic_seq_no": stored.topic_seq_no,
                "task_id": stored.event.scope.task_id or "",
            },
        )
