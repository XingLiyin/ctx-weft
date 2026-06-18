"""Shared conversation-record → history block mapping (spec/06 §4.1).

Used by RecentMemorySource (task-layer records) and AgentExperienceSource
(agent-layer AGENT_CONVERSATION_TURN records) so a memory record renders
identically wherever it is recalled from. Tool fidelity is keyed off role:
assistant→tool_calls, tool→tool_call_id (matches how LLM_RESPONSE/TOOL_RESULT
are ingested).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.utils import content_to_text, estimate_tokens, generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.protocols import MemoryRecord


def record_to_history_block(record: "MemoryRecord", source: str, idx: int) -> "ContextBlock":
    """Map one MemoryRecord to a history ContextBlock (newest-first callers pass idx)."""
    from ctx_weft.core.assembler.assembler import ContextBlock

    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    role = record.role or "user"
    md = {
        "role": role,
        "type": record.type,
        "timestamp": record.timestamp.isoformat() if record.timestamp else "",
        "seq_no": record.metadata.get("seq_no", idx),
        "memory_event_id": record.id,
    }
    # 无损重建：assistant 携 tool_calls；tool 携 tool_call_id
    if role == "assistant":
        md["tool_calls"] = record.metadata.get("tool_calls", [])
    elif role == "tool":
        md["tool_call_id"] = record.metadata.get("tool_call_id", "")
    return ContextBlock(
        id=generate_id("blk"),
        source=source,
        kind="history",
        target="messages",
        content=text,
        priority=3,
        token_estimate=record.metadata.get("token_count") or estimate_tokens(text),
        metadata=md,
    )
