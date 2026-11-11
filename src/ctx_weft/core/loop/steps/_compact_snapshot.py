"""One compaction run's immutable rows and dependency-local token counts.

View IDs are checked again at commit boundaries. Only new immutable rows are
loaded; known folds update the cache directly. No cache survives the run or is
shared with background tasks. Related tasks render together so parent/child
visibility and legacy call/result pairing retain their normal semantics.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from ctx_weft.core.assembler.builder import compile_history
from ctx_weft.core.assembler.sources._history import history_facts
from ctx_weft.core.loop.task_memory_fold import capsule_origin
from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryRecord, MemoryScope

TASK_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]


@dataclass(frozen=True)
class CapsulePlan:
    origins: frozenset[str]
    rows: tuple[MemoryRecord, ...]
    closing: MemoryRecord | None
    demotable: bool
    needs_repair: bool


class CompactSnapshot:
    def __init__(self, state, ctx):
        self.state, self.ctx = state, ctx
        self.owner = asyncio.current_task()
        self.address = MemoryAddress(session_id=state.scope.session_id, agent_id=state.scope.agent_id)
        self.records: dict[str, MemoryRecord] = {}
        self.ids: dict[MemoryScope, list[str]] = {}
        self.plans: dict[str, CapsulePlan] | None = None
        self._record_groups: dict[str, str] = {}
        self._origin_groups: dict[str, str] = {}
        self._groups: dict[str, dict[str, MemoryRecord]] = {}
        self._costs: dict[str, int] = {}
        self._full_only = False

    async def refresh(self, *, known_mutation=False) -> bool:
        views = {}
        for scope, kinds in ((MemoryScope.TASK, TASK_KINDS),
                             (MemoryScope.AGENT, [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY])):
            views[scope] = await self.ctx.memory.load_view_ids(
                self.address, scope, self.ctx.provider_ctx, kinds=kinds)
        changed = {s: set(ids) for s, ids in views.items()} != {s: set(ids) for s, ids in self.ids.items()}
        external = bool(self.ids) and changed and not known_mutation
        if changed:
            wanted = {rid for ids in views.values() for rid in ids}
            added = wanted - self.records.keys()
            rows = await self.ctx.memory.read_records(list(added), self.ctx.provider_ctx) if added else []
            self.records = {rid: r for rid, r in self.records.items() if rid in wanted}
            self.records.update((r.id, r) for r in rows)
            self.plans = None
            self._groups.clear()
            self._costs.clear()
        self.ids = views
        return external

    def view(self):
        return tuple([self.records[rid] for rid in self.ids.get(scope, []) if rid in self.records]
                     for scope in (MemoryScope.AGENT, MemoryScope.TASK))

    async def fold(self, ids, replacements):
        if not self._groups:
            self._index_groups()
        new_ids = await self.ctx.memory.fold(ids, replacements, self.ctx.provider_ctx)
        rows = await self.ctx.memory.read_records(new_ids, self.ctx.provider_ctx)
        removed = set(ids)
        for rid in removed:
            self.records.pop(rid, None)
            group = self._record_groups.pop(rid, None)
            if group is not None:
                self._groups[group].pop(rid, None)
                self._costs.pop(group, None)
        for row in rows:
            self.records[row.id] = row
            origin = self._origin(row)
            group = self._origin_groups.get(origin, row.id)
            self._record_groups[row.id] = group
            self._groups.setdefault(group, {})[row.id] = row
            self._costs.pop(group, None)
        for scope in self.ids:
            kept = [rid for rid in self.ids[scope] if rid not in removed]
            kept.extend(r.id for r in rows if r.scope == scope and r.id not in kept)
            self.ids[scope] = sorted(kept, key=lambda rid: (
                self.records[rid].timestamp, self.records[rid].metadata.get("seq_no", 0)))
        return new_ids

    @staticmethod
    def _origin(row):
        return row.address.task_id if row.scope == MemoryScope.TASK else capsule_origin(row)

    def _index_groups(self):
        # Use the renderer's actual trees. A running parent with no closure does
        # not make all completed siblings one tree; joining metadata ancestry
        # alone would unnecessarily re-render every sibling after each fold.
        parents = {}

        def find(key):
            parents.setdefault(key, key)
            root = key
            while parents[root] != root:
                root = parents[root]
            while parents[key] != key:
                following = parents[key]
                parents[key] = root
                key = following
            return root

        def join(left, right):
            parents[find(left)] = find(right)

        origins, calls = {}, {}
        for row in self.records.values():
            origin = self._origin(row)
            key = ("task", origin) if origin else ("row", row.id)
            origins[row.id] = key
            find(key)
        rows, facts = history_facts(self._ordered(self.records.values()), task_id=self.state.scope.task_id)
        for task in facts["task_history"].values():
            join(("task", task["id"]), ("task", task["tree_key"][-1]))
        for item in rows:
            key = origins[item["record_id"]]
            call_ids = [tc.get("id") for tc in item["message"].tool_calls]
            call_ids.append(item["message"].tool_call_id)
            call_ids.append(item["marker"].get("group_id"))
            for call_id in filter(None, call_ids):
                if call_id in calls:
                    join(key, calls[call_id])
                calls[call_id] = key
        self._record_groups = {rid: repr(find(key)) for rid, key in origins.items()}
        self._origin_groups = {key[1]: repr(find(key)) for key in parents if key[0] == "task"}
        groups = defaultdict(dict)
        for rid, row in self.records.items():
            groups[self._record_groups[rid]][rid] = row
        self._groups = dict(groups)

    def tokens(self, *, full=False):
        if not self.records:
            return 0
        full = full or not self._groups
        if full:
            self._costs.clear()
            self._index_groups()
        count = self.ctx.llm.tokenizer.count
        for group, rows in self._groups.items():
            if group not in self._costs:
                self._costs[group] = compile_history(
                    self._ordered(rows.values()), token_counter=count, task_id=self.state.scope.task_id).token_count
        total = sum(self._costs.values())
        if full or self._full_only:
            complete = compile_history(self._ordered(self.records.values()), token_counter=count,
                                       task_id=self.state.scope.task_id).token_count
            # An unfamiliar legacy relationship must cost extra work, never
            # corrupt the budget. Fresh full checks also validate local sums.
            self._full_only = complete != total
            return complete
        return total

    @staticmethod
    def _ordered(rows):
        return sorted(rows, key=lambda r: (r.timestamp, r.metadata.get("seq_no", 0)))

    def close(self):
        # A background task may inherit a ContextVar even though it cannot use
        # this cache. Release payloads as soon as the foreground call ends.
        self.owner = None
        self.plans = None
        for mapping in (self.records, self.ids, self._groups, self._costs,
                        self._record_groups, self._origin_groups):
            mapping.clear()


_snapshot: ContextVar[CompactSnapshot | None] = ContextVar("compact_snapshot", default=None)


def current_snapshot(ctx):
    snapshot = _snapshot.get()
    return snapshot if snapshot is not None and snapshot.ctx is ctx and snapshot.owner is asyncio.current_task() else None


@contextmanager
def compact_snapshot(state, ctx):
    snapshot = CompactSnapshot(state, ctx)
    token = _snapshot.set(snapshot)
    try:
        yield
    finally:
        _snapshot.reset(token)
        snapshot.close()
