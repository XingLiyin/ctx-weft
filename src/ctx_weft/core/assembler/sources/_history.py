"""Shared conversation-record → history block mapping (spec/06 §4.1).

Used by AgentRecallSource (task-layer body + agent-layer AGENT_CONVERSATION_TURN
records) so a memory record renders
identically wherever it is recalled from. Tool fidelity is keyed off role:
assistant→tool_calls, tool→tool_call_id (matches how LLM_RESPONSE/TOOL_RESULT
are ingested).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.protocols import MemoryRecord

def record_to_history_block(
    record: "MemoryRecord",
    source: str,
    idx: int,
) -> "ContextBlock":
    """Wrap one raw MemoryRecord; prompt-specific rendering happens later."""
    from ctx_weft.core.assembler.assembler import ContextBlock
    return ContextBlock(
        id=generate_id("blk"),
        source=source,
        kind="history",
        value=record,
        metadata={"sequence": record.metadata.get("seq_no", idx)},
    )
