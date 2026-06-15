"""RecentMemorySource：task 层对话 → 结构化 history blocks（spec/06 §4.1）。

PrepareStep 装配 messages 段的主路径——重建「当前任务自己的执行对话」（无损回合）：
USER_PROMPT / LLM_RESPONSE(+tool_calls) / TOOL_RESULT(+tool_call_id) / TASK_COMPACT_SUMMARY。
TOOL_INVOCATION 仅审计，重建跳过；派发(submit_*)的回合在 agent_experience source。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.utils import content_to_text, estimate_tokens, generate_id
from ctx_weft.protocols import MemoryEventType

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


# 默认 PrepareStep 装配时拉取的 task 层类型集合（同层）
DEFAULT_RECENT_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]


class RecentMemorySource:
    """按 recall_recent 拉取 task 层最近 N 条，重建为结构化回合 blocks。"""

    name = "task_conversation"

    def __init__(self, limit: int = 40, types: list[MemoryEventType] | None = None) -> None:
        self._limit = limit
        self._types = types or DEFAULT_RECENT_TYPES

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        records = await deps.memory.recall_recent(
            scope=request.scope,
            types=self._types,
            limit=self._limit,
            ctx=deps.provider_ctx,
        )

        # records 来自 recall_recent，按 timestamp 倒序；正序产出 block（composer 再按 timestamp 归并）
        for idx, record in enumerate(reversed(records)):
            text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
            md = {
                "role": record.role or "user",
                "type": record.type,
                "timestamp": record.timestamp.isoformat() if record.timestamp else "",
                "seq_no": record.metadata.get("seq_no", idx),
                "memory_event_id": record.id,
            }
            # 无损重建：assistant 携 tool_calls；tool 携 tool_call_id
            if record.type == MemoryEventType.LLM_RESPONSE:
                md["tool_calls"] = record.metadata.get("tool_calls", [])
            elif record.type == MemoryEventType.TOOL_RESULT:
                md["tool_call_id"] = record.metadata.get("tool_call_id", "")
            yield ContextBlock(
                id=generate_id("blk"),
                source="task_conversation",
                kind="history",
                target="messages",
                content=text,
                priority=3,  # 中等优先级；超出 budget 时优先于 task_spec 被裁
                token_estimate=record.metadata.get("token_count") or estimate_tokens(text),
                metadata=md,
            )
