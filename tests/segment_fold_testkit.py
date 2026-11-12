"""Adapt old segment fixtures to the memory organize protocol."""

import dataclasses
from dataclasses import dataclass


@dataclass
class SegmentFoldResult:
    events_before: int
    events_after: int
    summary_event_id: str


from ctx_weft.protocols import MemoryKind


async def segment_fold(
    memory,
    address,
    layer,
    summary,
    ctx,
    segment_end_at=None,
    *,
    operation_id="",
    context_record_ids=None,
):
    ids = context_record_ids
    if ids is None:
        ids = await memory.load_view_ids(
            address,
            layer,
            ctx,
            kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT],
            end_at=segment_end_at,
        )
    result = await memory.organize(
        dataclasses.replace(ctx, agent_id=address.agent_id, task_id=address.task_id),
        operation="fold_segment",
        record_ids=ids,
        summary=summary,
    )
    return SegmentFoldResult(
        events_before=result.records_before,
        events_after=result.records_after,
        summary_event_id=result.replacements[0].id if result.replacements else "",
    )
