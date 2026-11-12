"""Recall raw task/agent history candidates by ID, including closed tasks.

History facts describe task relationships. The shared Jinja history program
selects full/minimal views, renders legacy facts and places task labels. This
source neither renders nor truncates history. Fixed observe IDs preserve the
original observation snapshot; staged TASK records supplement live actor input.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryRecord, MemoryScope

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest

# v2（P3a）：召回改 load_records——task 层 body = TASK 视图默认 kinds（CONVERSATION_TURN+SUMMARY，
# 与旧 _TASK_TYPES 四类型等价）；agent 层 = AGENT 视图默认 kinds（覆盖旧 _AGENT_TYPES，legacy
# dispatch 配对已在 normalize_view 内完成）。全量幸存、升序——体量边界由 close/compact 的
# fold + PromptBuilder 负责，不在召回处截断。


#: TASK 视图默认召回的 kinds（与 `load_records` 默认口径一致：TOOL_AUDIT 不进装配）。
_VIEW_TASK_KINDS = (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY)


def _staged_record(ev) -> MemoryRecord:
    """暂存的 `MemoryEvent` → 与 `load_records` 同形的 `MemoryRecord`（v2 行：type=None）。"""
    return MemoryRecord(
        id=ev.id or "", type=None, content=ev.content, timestamp=ev.timestamp,
        role=ev.role, topic=ev.topic, kind=ev.kind, scope=ev.scope, address=ev.address,
        metadata=dict(ev.metadata or {}), blob_refs=list(ev.blob_refs or []),
    )


class AgentRecallSource:
    """Recall surviving task bodies and agent records for the current agent."""

    name = "agent_recall"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock
        from ctx_weft.core.utils.ids import generate_id

        extra = getattr(request, "extra", None) or {}
        organized = extra.get("organize_records")
        if organized is not None:
            for idx, record in enumerate(organized):
                yield record_to_history_block(record, source=self.name, idx=idx)
            return
        excluded = set(extra.get("exclude_memory_ids", ()))
        fixed_ids = extra.get("observe_record_ids")
        if fixed_ids is not None:
            for record_id in fixed_ids:
                yield ContextBlock(id=generate_id("blk"), source=self.name, kind="history",
                                   value=None, memory_id=record_id)
            return
        # Compatibility for direct Source consumers; runtime observe passes IDs.
        fixed = extra.get("observe_records")
        if fixed is not None:
            for idx, record in enumerate(fixed):
                if record.kind in _VIEW_TASK_KINDS:
                    yield record_to_history_block(record, source=self.name, idx=idx)
            return

        address = MemoryAddress(session_id=request.scope.session_id,
                                agent_id=request.scope.agent_id)
        end = extra.get("observe_end_at")
        for scope in (MemoryScope.TASK, MemoryScope.AGENT):
            ids = await deps.memory.load_view_ids(
                address, scope, deps.provider_ctx, kinds=list(_VIEW_TASK_KINDS), end_at=end)
            for record_id in ids:
                if record_id in excluded:
                    continue
                yield ContextBlock(id=generate_id("blk"), source=self.name, kind="history",
                                   value=None, memory_id=record_id)

        staged = [
            ev for ev in extra.get("staged_memory", [])
            if ev.scope is MemoryScope.TASK and ev.kind in _VIEW_TASK_KINDS
            and getattr(ev.address, "agent_id", None) == request.scope.agent_id
            and ev.id not in excluded
        ]
        for order, event in enumerate(staged):
            block = record_to_history_block(_staged_record(event), source=self.name, idx=order)
            # The compiler sees the selected rows' seq_no and places staged rows
            # after them on timestamp ties, without reading bodies during assembly.
            block.metadata = {"staged_order": order}
            yield block
