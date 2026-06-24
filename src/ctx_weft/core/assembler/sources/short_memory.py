"""RecentMemorySource：task 层对话 → 结构化 history blocks（spec/06 §4.1）。

PrepareStep 装配 messages 段的主路径——重建「当前任务自己的执行对话」（无损回合）：
USER_PROMPT / LLM_RESPONSE(+tool_calls) / TOOL_RESULT(+tool_call_id) / TASK_COMPACT_SUMMARY。
TOOL_INVOCATION 仅审计，重建跳过；派发(submit_*)的回合在 agent_experience source。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.sources._history import record_to_history_block
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


# 召回**所有**未 superseded 的 task 层记录。体量边界由「压缩」(把旧轮 supersede 成 summary)
# + BudgetStrategy(按 token 预算、priority-aware 裁剪)负责，**不**在召回处按 recency 截断——
# 那会把没被压缩的记录(如任务跑长后最旧的 USER_PROMPT)悄悄丢掉，导致 prompt 丢失任务框架。
# 2000 是代码库「实际等价于全部」的约定(见 fold / _preserve_conversation)，真实任务不会触顶。
_RECALL_ALL = 2000


class RecentMemorySource:
    """召回 task 层全部未 superseded 记录，重建为结构化回合 blocks。"""

    name = "task_conversation"

    def __init__(self, types: list[MemoryEventType] | None = None) -> None:
        self._types = types or DEFAULT_RECENT_TYPES

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        records = await deps.memory.recall_recent(
            scope=request.scope,
            types=self._types,
            limit=_RECALL_ALL,
            ctx=deps.provider_ctx,
        )

        # records 来自 recall_recent，按 timestamp 倒序；正序产出 block（composer 再按 timestamp 归并）
        for idx, record in enumerate(reversed(records)):
            yield record_to_history_block(record, source="task_conversation", idx=idx)
