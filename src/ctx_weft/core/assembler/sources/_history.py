"""Shared conversation-record → history block mapping (spec/06 §4.1).

Used by AgentRecallSource (task-layer body + agent-layer AGENT_CONVERSATION_TURN
records) so a memory record renders
identically wherever it is recalled from. Tool fidelity is keyed off role:
assistant→tool_calls, tool→tool_call_id (matches how LLM_RESPONSE/TOOL_RESULT
are ingested).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils import (
    PROGRESS_SO_FAR_HEADING, content_to_text, estimate_tokens, generate_id,
)
from ctx_weft.protocols import MemoryEventType

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.protocols import MemoryRecord

COMPACT_SUMMARY_WRAPPER_PREFIX = (
    "［以下是先前对话/经验的压缩摘要，供你延续工作参考；并非用户的新指令］\n"
)


def wrap_compact_summary(text: str) -> str:
    """给 compaction summary 文本套显式包装前缀（渲染期，不落库）。"""
    return f"{COMPACT_SUMMARY_WRAPPER_PREFIX}{text}"


def record_to_history_block(
    record: "MemoryRecord", source: str, idx: int, *, current_task_id: str | None = None
) -> "ContextBlock":
    """Map one MemoryRecord to a history ContextBlock (newest-first callers pass idx).

    current_task_id：正在装配的 task。TASK_COMPACT_SUMMARY 段摘要仅当归属该 task（record 的
    scope task_id == current_task_id）时冠 ## Progress So Far 标题——即「当前任务的上一段复述」；
    跨 task 胶囊（别的 task_id）不冠，不改跨任务重建形态。None → 一律不冠（防御）。
    """
    from ctx_weft.core.assembler.assembler import ContextBlock

    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    role = record.role or "user"
    # 包装是给「以 user 身份呈现」的摘要消歧义；assistant 自述无需。新数据段摘要恒 assistant
    # → 不套；旧数据若残留 role=user 仍套（防御）。AGENT_COMPACT_SUMMARY 在 agent_experience/
    # agent_recall 自行包装，不走此分支。
    if record.type == MemoryEventType.TASK_COMPACT_SUMMARY and role == "user":
        text = wrap_compact_summary(text)
    elif (
        record.type == MemoryEventType.TASK_COMPACT_SUMMARY
        and role == "assistant"
        and current_task_id is not None
        and record.metadata.get("task_id") == current_task_id
    ):
        # 当前任务的「上一段执行复述」（max_turns / 边界 compact / plain_text 复用 act_recap）：
        # 冠以统一标题，与 composer 非压缩 retry 进度对齐。判据按 task_id 匹配当前 task，而非
        # source 名——AgentRecallSource（526859f 起统一召回）用同一 source="agent_recall" 承载
        # 当前 task 段摘要与跨 task 胶囊，只有 task_id 能区分二者；跨 task 胶囊不冠此标题。
        text = f"{PROGRESS_SO_FAR_HEADING}\n{text}"
    md = {
        "role": role,
        "type": record.type,
        "timestamp": record.timestamp.isoformat() if record.timestamp else "",
        "seq_no": record.metadata.get("seq_no", idx),
        "memory_event_id": record.id,
        # 承载来源 task（USER_PROMPT 记录带 metadata={"task_id": task.id}，见 driver）——
        # composer 据此把 ## Current Task/Message 框贴到「当前 task」自己的 user 回合，
        # 而非召回历史里最后一条（同 agent 子 body 更新时会误顶 parent 的头）。
        "task_id": record.metadata.get("task_id", ""),
        # budget 层据此判「agent 层回合」归属哪个 task（finish/dispatch 对来自哪个已结束 task）。
        "origin_task_id": record.metadata.get("origin_task_id", ""),
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
        priority=slot_priority("history", str(record.type)),
        token_estimate=record.metadata.get("token_count") or estimate_tokens(text),
        metadata=md,
    )
