"""InMemoryMemoryProvider：单进程内存版 MemoryProvider，主要用于单测 + Phase 1 集成测试。

实现 §4.3 完整协议；recall_semantic 返空（V1 默认 StructuredBlackboard 行为）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from ctx_weft.protocols import (
    EVENT_LAYER,
    MemoryAddress,
    MemoryEvent,
    MemoryEventType,
    MemoryKind,
    MemoryScope,
    MemoryProvider,
    MemoryProviderInfo,
    MemoryRecord,
    MemoryAddress,
    ProviderContext,
    Subscription,
)
from ctx_weft.protocols.memory_compat import (
    kind_of,
    layer_of,
    legacy_type_of,
    matches_legacy_type,
    normalize_view,
)


@dataclass
class _StoredEvent:
    """内存中存的一条事件。"""

    id: str
    event: MemoryEvent
    seq_no: int  # per-(agent_id, scope) 单调递增
    topic_seq_no: int  # per-topic 单调递增（None topic 不算）
    # ingest 时归一化的 v2 三元组（kind=None → 死类型，永不见于视图）
    kind: MemoryKind | None = None
    scope: MemoryScope | None = None
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
            return self._ingest_locked(event, ctx, event_id)

    def _ingest_locked(self, event: MemoryEvent, ctx: ProviderContext, event_id: str) -> str:
        """ingest 内核（须持 self._lock 调用）；fold 复用以保证原子性。"""
        # v2 三元组归一：kind 解析失败 = 死类型（OBSERVER_SUMMARY 等）→ 存储保留、视图不见
        try:
            kind = kind_of(event.type, event.kind)
        except ValueError:
            kind = None
        layer = layer_of(event.type, event.scope)

        scope_key = self._scope_key(event.address, ctx.tenant_id, layer)
        self._seq_counters[scope_key] = self._seq_counters.get(scope_key, 0) + 1
        seq_no = self._seq_counters[scope_key]

        topic_seq = 0
        if event.topic:
            # 覆盖语义（PUBLICATION 特例）：同 topic 旧发布标 superseded，只保留最新一条
            if kind is MemoryKind.PUBLICATION:
                for s in self._events:
                    if (not s.is_superseded
                            and s.event.topic == event.topic
                            and s.kind is MemoryKind.PUBLICATION):
                        s.is_superseded = True
            self._topic_seq[event.topic] = self._topic_seq.get(event.topic, 0) + 1
            topic_seq = self._topic_seq[event.topic]

        stored = _StoredEvent(
            id=event_id,
            event=event,
            seq_no=seq_no,
            topic_seq_no=topic_seq,
            kind=kind,
            scope=layer,
        )
        self._events.append(stored)
        return event_id

    async def fold(
        self,
        supersede_ids: list[str],
        replacements: list[MemoryEvent],
        ctx: ProviderContext,
    ) -> list[str]:
        """原子"遗忘 + 补偿"：单锁内标 superseded + 逐条走 ingest 内核（in-memory 用锁模拟事务）。"""
        wanted = set(supersede_ids)
        new_ids: list[str] = []
        async with self._lock:
            for s in self._events:
                if s.id in wanted and not s.is_superseded:
                    s.is_superseded = True
            for ev in replacements:
                if ev.id is not None and any(s.id == ev.id for s in self._events):
                    new_ids.append(ev.id)  # record-id 契约：已存在 = no-op
                    continue
                if ev.id is not None:
                    event_id = ev.id
                else:
                    self._next_id += 1
                    event_id = f"mev_{self._next_id:08d}"
                new_ids.append(self._ingest_locked(ev, ctx, event_id))
        return new_ids

    # ── load_view（v2 §4：工作记忆回放）─────────────────────────────────────────

    _DEFAULT_KINDS = (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY)

    async def load_view(
        self,
        address: MemoryAddress,
        scope: MemoryScope,
        ctx: ProviderContext,
        kinds: list[MemoryKind] | None = None,
    ) -> list[MemoryRecord]:
        self._validate_half_address(address, scope)
        wanted = set(kinds) if kinds is not None else set(self._DEFAULT_KINDS)

        matching = [
            s for s in self._events
            if not s.is_superseded
            and s.scope is scope
            and s.kind in wanted
            and self._address_match(s.event.address, address, scope)
        ]
        matching.sort(key=lambda s: (s.event.timestamp, s.seq_no))
        return normalize_view([self._to_record(s) for s in matching])

    @staticmethod
    def _validate_half_address(address: MemoryAddress, scope: MemoryScope) -> None:
        """半址矩阵（v2 §4）：非法非 None 字段 loud 失败，抓静默漏召回。"""
        if scope is MemoryScope.TASK:
            if address.task_id is None and address.agent_id is None:
                raise ValueError("TASK view requires task_id (single-task) or agent_id (cross-task)")
        elif scope is MemoryScope.AGENT:
            if not address.agent_id:
                raise ValueError("AGENT view requires agent_id")
            if address.task_id is not None:
                raise ValueError("AGENT view forbids task_id (pass task_id=None)")
        else:  # SESSION
            if address.task_id is not None or address.agent_id is not None:
                raise ValueError("SESSION view forbids task_id/agent_id")

    @staticmethod
    def _address_match(stored: MemoryAddress, address: MemoryAddress, scope: MemoryScope) -> bool:
        if stored.session_id != address.session_id:
            return False
        if scope is MemoryScope.TASK:
            if address.task_id is not None:
                if stored.task_id != address.task_id:
                    return False
                # 全址时防御性校验 agent 归属
                return address.agent_id is None or stored.agent_id == address.agent_id
            return stored.agent_id == address.agent_id  # 跨 task 聚合
        if scope is MemoryScope.AGENT:
            return stored.agent_id == address.agent_id
        return True  # SESSION：session_id 已匹配

    # ── Recall ────────────────────────────────────────────────────────────────

    def _matches_any_type(self, stored: _StoredEvent, type_set: set[MemoryEventType]) -> bool:
        """过渡期桥接：旧行按 type 精确匹配，v2 行按 LEGACY_TRIPLE 三元组匹配。"""
        return any(
            matches_legacy_type(stored.event.type, stored.kind, stored.scope,
                                stored.event.role, t)
            for t in type_set
        )

    def _to_legacy_record(self, stored: _StoredEvent) -> MemoryRecord:
        """recall wrapper 出口：v2 行回填 legacy 等价 type——wrapper 的契约就是 legacy 视界
        （旧断言/旧渲染按 record.type 消费）；load_view 出口不回填（kind 优先词汇）。"""
        rec = self._to_record(stored)
        if rec.type is None:
            rec.type = legacy_type_of(rec.kind, rec.scope, rec.role)
        return rec

    async def recall_recent(
        self,
        scope: MemoryAddress,
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
            if not self._matches_any_type(stored, type_set):
                continue
            lyr = stored.scope
            if lyr not in target_keys:
                continue
            if self._scope_key(stored.event.address, ctx.tenant_id, lyr) != target_keys[lyr]:
                continue
            matching.append(stored)

        # 跨层用 timestamp 归并（同层即 seq 序）；newest-first 返回，limit 截最近 N
        matching.sort(key=lambda s: s.event.timestamp)
        recent = matching[-limit:] if limit and limit > 0 else matching
        return [self._to_legacy_record(s) for s in reversed(recent)]

    async def recall_recent_by_agent(
        self,
        agent_scope: MemoryAddress,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        type_set = set(types)
        aid = agent_scope.agent_id
        matching = [
            s for s in self._events
            if not s.is_superseded
            and self._matches_any_type(s, type_set)
            and s.event.address.session_id == agent_scope.session_id
            and s.event.address.agent_id == aid
            and s.scope is MemoryScope.TASK
        ]
        matching.sort(key=lambda s: s.event.timestamp)
        recent = matching[-limit:] if limit and limit > 0 else matching
        return [self._to_legacy_record(s) for s in reversed(recent)]

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
        scope: MemoryAddress,
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

    # ── Legacy test-compat（v2 P4b-2）────────────────────────────────────────
    # apply_compact 已删：策展政策（keep_last/protect/段界/锚点）上移框架侧 segment_fold，
    # provider 只保留原子 fold。下方 recall_recent / recall_recent_by_agent / count_recent /
    # supersede 为**非协议**实例方法，仅供存量测试兼容（P4a 范围决策）；新代码一律
    # load_view / fold。日落路径：随存量测试逐文件迁移后删除。


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
        scope: MemoryAddress,
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
            if not self._matches_any_type(stored, type_set):
                continue
            lyr = stored.scope
            if lyr not in target_keys:
                continue
            if self._scope_key(stored.event.address, ctx.tenant_id, lyr) != target_keys[lyr]:
                continue
            count += 1
        return count

    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo:
        return MemoryProviderInfo(
            name=self.name,
            supports_semantic=False,
            supports_topic=True,
            archives_superseded=True,
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scope_key(self, scope: MemoryAddress, tenant_id: str, layer: MemoryScope) -> str:
        if layer is MemoryScope.TASK:
            return f"{tenant_id}|{scope.session_id}|task|{scope.task_id or ''}"
        if layer is MemoryScope.AGENT:
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
            kind=stored.kind,
            scope=stored.scope,
            address=stored.event.address,  # 来源回显（v2 §3；metadata 打标过渡期保留）
            metadata={
                **stored.event.metadata,
                "seq_no": stored.seq_no,
                "topic_seq_no": stored.topic_seq_no,
                "task_id": stored.event.address.task_id or "",
            },
        )
