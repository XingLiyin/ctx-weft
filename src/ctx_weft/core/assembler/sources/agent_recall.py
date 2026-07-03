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

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.assembler.sources._history import record_to_history_block, wrap_compact_summary
from ctx_weft.core.loop.steps.legacy_dispatch import normalize_legacy_dispatch
from ctx_weft.core.utils import content_to_text, estimate_tokens, generate_id
from ctx_weft.protocols import MemoryEventType

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest

# task 层 body 类型（ALL 未折叠 task，含已结束的；按 agent_id 跨 task 召回）
# task-resident：body 不因 close 而 supersede，OPEN/CLOSED 由 task.status / finish 对判，不靠 supersession。
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

        # ── 1) task 层 body：按 agent_id 跨 task 召回（ALL 未折叠 task，含已结束的） ──
        task_records = await deps.memory.recall_recent_by_agent(
            agent_scope=request.scope,
            types=_TASK_TYPES,
            limit=_RECALL_ALL,
            ctx=deps.provider_ctx,
        )
        for idx, record in enumerate(reversed(task_records)):
            yield record_to_history_block(record, source="agent_recall", idx=idx)

        # ── 2) agent 层残留 / 经验 ──
        # 全召回未 superseded（同 task 层）：体量交给 supersede + BudgetStrategy 的 token 守卫，
        # 不在召回处按条数截断——否则滚动 AGENT_COMPACT_SUMMARY（锚在最早）会被截出窗、丢经验。
        agent_records = await deps.memory.recall_recent(
            scope=request.scope,
            types=_AGENT_TYPES,
            limit=_RECALL_ALL,
            ctx=deps.provider_ctx,
        )
        # §5.5：存量 legacy dispatch 对在读侧归一化成 conversation turn，下面统一走 conversation 渲染。
        agent_records = normalize_legacy_dispatch(agent_records)

        summaries: list = []
        conversation: list = []
        for r in agent_records:
            if r.type == MemoryEventType.AGENT_COMPACT_SUMMARY:
                summaries.append(r)
            elif r.type == MemoryEventType.AGENT_CONVERSATION_TURN:
                conversation.append(r)

        def _ts(rec) -> str:
            return rec.timestamp.isoformat() if getattr(rec, "timestamp", None) else ""

        for s in summaries:
            text = content_to_text(s.content) if not isinstance(s.content, str) else s.content
            text = wrap_compact_summary(text)
            yield ContextBlock(
                id=generate_id("blk"),
                source="agent_recall",
                kind="history",
                target="messages",
                content=text,
                priority=slot_priority("history", "agent_compact_summary"),
                token_estimate=estimate_tokens(text),
                metadata={"role": "user", "type": s.type, "timestamp": _ts(s),
                          "seq_no": s.metadata.get("seq_no", 0)},
            )

        # finish 对 + dispatch 对（均为 AGENT_CONVERSATION_TURN）统一按时序渲染：assistant 携
        # tool_calls、tool 携 tool_call_id（record_to_history_block 据 role 无损重建）。悬空 tool_call
        # （在途 dispatch 尚无 result）由 llm_gateway 的 drop_dangling_tool_calls 兜底。
        for idx, c in enumerate(reversed(conversation)):
            yield record_to_history_block(c, source="agent_recall", idx=idx)
