"""AgentRecallSource：统一装配路径（spec 2026-06-23 重构）。

agent 是上下文组织单元：一次召回 = 本 agent 名下所有 task 的记录，按 timestamp 归并。
- OPEN task → 完整对话（task 层 USER_PROMPT/LLM_RESPONSE/TOOL_RESULT/TASK_COMPACT_SUMMARY，
  按 agent_id 跨 task 召回；CLOSED task 的对话已 supersede，不会返回）。
- CLOSED task → 残留：TASK_DISPATCH ↔ TASK_DISPATCH_RESULT 配对（未配对 dispatch 隐去，
  避免悬空 tool_call）。AGENT_COMPACT_SUMMARY → user 摘要回合（与 apply_compact 写入的 role=user 一致）；
  AGENT_CONVERSATION_TURN → 原样回合（inherit_memory 快照载体）。

取代旧的 RecentMemorySource + AgentExperienceSource 双源。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.core.utils import content_to_text, estimate_tokens, generate_id
from ctx_weft.protocols import MemoryEventType
from ctx_weft.protocols.capability import qualify

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest

# OPEN task 对话类型（task 层，按 agent_id 跨 task 召回）
_TASK_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
    MemoryEventType.TASK_COMPACT_SUMMARY,
]
# 残留 / 经验类型（agent 层）
_AGENT_TYPES = [
    MemoryEventType.TASK_DISPATCH,
    MemoryEventType.TASK_DISPATCH_RESULT,
    MemoryEventType.AGENT_COMPACT_SUMMARY,
    MemoryEventType.AGENT_CONVERSATION_TURN,
]

# 召回全部未 superseded（体量边界由 close/compact 的 supersede + BudgetStrategy 负责，不在召回处截断）
_RECALL_ALL = 2000


class AgentRecallSource:
    """单一装配源：OPEN task 全对话 + CLOSED 残留，按 timestamp 在 composer 归并。"""

    name = "agent_recall"

    def __init__(self, limit: int = 50) -> None:
        self._limit = limit

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        # ── 1) OPEN task 对话：按 agent_id 跨 task 召回 ──
        task_records = await deps.memory.recall_recent_by_agent(
            agent_scope=request.scope,
            types=_TASK_TYPES,
            limit=_RECALL_ALL,
            ctx=deps.provider_ctx,
        )
        for idx, record in enumerate(reversed(task_records)):
            yield record_to_history_block(record, source="agent_recall", idx=idx)

        # ── 2) agent 层残留 / 经验 ──
        agent_records = await deps.memory.recall_recent(
            scope=request.scope,
            types=_AGENT_TYPES,
            limit=self._limit,
            ctx=deps.provider_ctx,
        )

        dispatches: dict[str, object] = {}   # tool_call_id → dispatch record
        results: dict[str, list] = {}        # tool_call_id → [result records]
        summaries: list = []
        conversation: list = []
        for r in agent_records:
            if r.type == MemoryEventType.TASK_DISPATCH:
                tcid = r.metadata.get("tool_call_id")
                if tcid:
                    dispatches[tcid] = r
            elif r.type == MemoryEventType.TASK_DISPATCH_RESULT:
                tcid = r.metadata.get("tool_call_id")
                if tcid:
                    results.setdefault(tcid, []).append(r)
            elif r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
                summaries.append(r)
            elif r.type == MemoryEventType.AGENT_CONVERSATION_TURN:
                conversation.append(r)

        def _ts(rec) -> str:
            return rec.timestamp.isoformat() if getattr(rec, "timestamp", None) else ""

        for s in summaries:
            text = content_to_text(s.content) if not isinstance(s.content, str) else s.content
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_recall",
                kind="history",
                target="messages",
                content=text,
                priority=3,
                token_estimate=estimate_tokens(text),
                metadata={"role": "user", "type": s.type, "timestamp": _ts(s),
                          "seq_no": s.metadata.get("seq_no", 0)},
            )

        for idx, c in enumerate(reversed(conversation)):
            yield record_to_history_block(c, source="agent_recall", idx=idx)

        # 配对回合；未配对 dispatch 隐去（避免悬空 tool_call）
        for tcid, d in dispatches.items():
            res = results.get(tcid)
            if not res:
                continue
            tool_name = d.metadata.get("tool_name", qualify("control:delegate_task"))
            args = d.metadata.get("arguments", {})
            ts = _ts(d)
            seq = d.metadata.get("seq_no", 0)
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_recall",
                kind="history",
                target="messages",
                content="",
                priority=3,
                token_estimate=estimate_tokens(str(args)),
                metadata={"role": "assistant", "type": d.type, "timestamp": ts, "seq_no": seq,
                          "tool_calls": [{"id": tcid, "name": tool_name, "input": args}]},
            )
            res_sorted = sorted(res, key=_ts)
            combined = "\n\n".join(
                content_to_text(rr.content) if not isinstance(rr.content, str) else rr.content
                for rr in res_sorted
            )
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_recall",
                kind="history",
                target="messages",
                content=combined,
                priority=3,
                token_estimate=estimate_tokens(combined),
                metadata={"role": "tool", "type": MemoryEventType.TASK_DISPATCH_RESULT,
                          "timestamp": ts, "seq_no": seq, "tool_call_id": tcid},
            )
