"""AgentRecallSource：统一装配路径（spec 2026-06-23 重构；task-resident 语义 2026-06-28）。

agent 是上下文组织单元：一次召回 = 本 agent 名下所有 task 的记录，按 timestamp 归并。

task-resident（spec 2026-06-28）：
- **未折叠 task body**（ALL tasks，无论是否结束）→ task 层记录
  （USER_PROMPT/LLM_RESPONSE/TOOL_RESULT/TASK_COMPACT_SUMMARY），按 agent_id 跨 task 召回。
  body 不因 close 而 supersede（旧的「CLOSED body 已 supersede 故不返回」假设已失效）。
- **结束 task 的 finish 对**（AGENT_CONVERSATION_TURN：assistant finish_task + tool Process Report）
  → agent 层；按 (timestamp, seq_no) 与 body 归并 → `[body][finish 对]`。
- **运行中/暂停 task**（status ≠ FINISHED，无 finish 对）→ 只有 body，无 finish 对 → `[body]`。
- OPEN/CLOSED 判据：task.status（或等价地：finish 对是否存在），不依赖 supersession 状态。

agent 层另含：AGENT_CONVERSATION_TURN（finish 对 + dispatch 对 + inherit_memory 快照载体）
→ 原样按时序渲染（dispatch 对 = delegating task 对话里的一组普通 message，spec 2026-06-28 §2.3）；
AGENT_COMPACT_SUMMARY → user 摘要回合。存量 legacy TASK_DISPATCH/RESULT 由 normalize_legacy_dispatch
在读侧归一化成 conversation turn（§5.5），核心渲染只面对单一表示。

取代旧的 RecentMemorySource + AgentExperienceSource 双源。
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
    """単一装配源（task-resident，spec 2026-06-28）：
    ① task 层 body（所有未折叠 task，含已结束的）+ ② agent 层 finish 对/dispatch 对/经验，
    按 (timestamp, seq_no) 在 composer 归并。
    结束 task → `[body][finish 对]`；运行中/暂停 task → `[body]`（无 finish 对）。
    """

    name = "agent_recall"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock
        from ctx_weft.core.utils.ids import generate_id

        extra = getattr(request, "extra", None) or {}
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
                yield ContextBlock(id=generate_id("blk"), source=self.name, kind="history",
                                   value=None, memory_id=record_id)

        staged = [
            ev for ev in extra.get("staged_memory", [])
            if ev.scope is MemoryScope.TASK and ev.kind in _VIEW_TASK_KINDS
            and getattr(ev.address, "agent_id", None) == request.scope.agent_id
        ]
        for order, event in enumerate(staged):
            block = record_to_history_block(_staged_record(event), source=self.name, idx=order)
            # The compiler sees the selected rows' seq_no and places staged rows
            # after them on timestamp ties, without reading bodies during assembly.
            block.metadata = {"staged_order": order}
            yield block
