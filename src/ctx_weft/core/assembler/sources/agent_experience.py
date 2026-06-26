"""AgentExperienceSource：agent 层派发日志 → 结构化 history blocks（spec/06 §4.2）。

把该 agent 的真实派发记录重建为 delegate_task(tool_call) ↔ tool_result(output+report) 配对回合：
- TASK_DISPATCH        → assistant 回合（tool_calls=[delegate_task(id=tool_call_id, args)]）
- TASK_DISPATCH_RESULT → tool 回合（tool_call_id 配对；delegate_plan 的多 child 结果聚合为一条）
- AGENT_COMPACT_SUMMARY → assistant 摘要回合
- AGENT_CONVERSATION_TURN → user/assistant/tool 回合（原样透传；保全的 root task 对话）

未配对的 TASK_DISPATCH（child 未回填）整条隐去，避免悬空 tool_call。
与 task_conversation 的 blocks 一起按 timestamp 在 composer 归并。
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

_AGENT_TYPES = [
    MemoryEventType.TASK_DISPATCH,
    MemoryEventType.TASK_DISPATCH_RESULT,
    MemoryEventType.AGENT_COMPACT_SUMMARY,
    MemoryEventType.AGENT_CONVERSATION_TURN,
]


class AgentExperienceSource:
    """按 recall_recent 拉取 agent 层派发日志，重建配对回合 blocks。"""

    name = "agent_experience"

    def __init__(self, limit: int = 50) -> None:
        self._limit = limit

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        records = await deps.memory.recall_recent(
            scope=request.scope,
            types=_AGENT_TYPES,
            limit=self._limit,
            ctx=deps.provider_ctx,
        )

        dispatches: dict[str, object] = {}        # tool_call_id → dispatch record（保最早一条）
        results: dict[str, list] = {}             # tool_call_id → [result records]
        summaries: list = []
        conversation: list = []
        for r in records:
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

        # AGENT_COMPACT_SUMMARY → user 摘要回合（渲染期套包装前缀，消歧义）
        from ctx_weft.core.assembler.sources._history import wrap_compact_summary
        for s in summaries:
            text = content_to_text(s.content) if not isinstance(s.content, str) else s.content
            text = wrap_compact_summary(text)
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_experience",
                kind="history",
                target="messages",
                content=text,
                priority=3,
                token_estimate=estimate_tokens(text),
                metadata={"role": "user", "type": s.type, "timestamp": _ts(s),
                          "seq_no": s.metadata.get("seq_no", 0)},
            )

        # AGENT_CONVERSATION_TURN → 原样回合（user/assistant/tool），composer 按 timestamp 归并
        for idx, c in enumerate(reversed(conversation)):
            yield record_to_history_block(c, source="agent_experience", idx=idx)

        # 配对回合；未配对 dispatch 隐去
        for tcid, d in dispatches.items():
            res = results.get(tcid)
            if not res:
                continue
            tool_name = d.metadata.get("tool_name", qualify("control:delegate_task"))
            args = d.metadata.get("arguments", {})
            ts = _ts(d)
            seq = d.metadata.get("seq_no", 0)

            # assistant：delegate_task tool_call（content 空，仅 tool_call）
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_experience",
                kind="history",
                target="messages",
                content="",
                priority=3,
                token_estimate=estimate_tokens(str(args)),
                metadata={"role": "assistant", "type": d.type, "timestamp": ts, "seq_no": seq,
                          "tool_calls": [{"id": tcid, "name": tool_name, "input": args}]},
            )
            # tool：聚合 N 个 child 结果（delegate_plan 一调多 child）按 timestamp 升序
            res_sorted = sorted(res, key=_ts)
            combined = "\n\n".join(
                content_to_text(rr.content) if not isinstance(rr.content, str) else rr.content
                for rr in res_sorted
            )
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_experience",
                kind="history",
                target="messages",
                content=combined,
                priority=3,
                token_estimate=estimate_tokens(combined),
                # 与 dispatch 同 timestamp/seq，归并后紧邻其后（emit 顺序稳定）
                metadata={"role": "tool", "type": MemoryEventType.TASK_DISPATCH_RESULT,
                          "timestamp": ts, "seq_no": seq, "tool_call_id": tcid},
            )
