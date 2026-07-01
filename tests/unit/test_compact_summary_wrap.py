"""compaction summary 渲染期包装（§2.4）：渲染带前缀，存储不含。"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources._history import (
    COMPACT_SUMMARY_WRAPPER_PREFIX, PROGRESS_SO_FAR_HEADING,
    record_to_history_block, wrap_compact_summary,
)
from ctx_weft.protocols import MemoryEventType, MemoryRecord

T = MemoryEventType


def _rec(type_, content, role="user"):
    return MemoryRecord(id="m1", type=type_, content=content,
                        timestamp=datetime(2026, 1, 1, tzinfo=UTC), role=role,
                        topic=None, metadata={"seq_no": 1})


def test_wrap_helper_prefixes():
    out = wrap_compact_summary("### 会话目标\nX")
    assert out.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
    assert "### 会话目标" in out


def test_task_compact_summary_block_wrapped():
    blk = record_to_history_block(_rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX"), "task_conversation", 0)
    assert blk.content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)


def test_task_compact_summary_assistant_gets_progress_heading():
    """role=assistant 的 task_conversation 段摘要 = 上一段执行复述：不套「并非用户新指令」包装，
    而是冠以 PROGRESS_SO_FAR_HEADING，作为统一的"先前进度"锚点。"""
    blk = record_to_history_block(
        _rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX", role="assistant"),
        "task_conversation", 0,
    )
    assert not blk.content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
    assert blk.content == f"{PROGRESS_SO_FAR_HEADING}\n### 会话目标\nX"
    assert blk.metadata["role"] == "assistant"


def test_task_compact_summary_assistant_no_heading_for_capsule_source():
    """胶囊召回（非 task_conversation 来源）的 task 段摘要不冠 Progress So Far 标题——
    标题只用于当前任务的上一段复述，不改跨任务重建形态。"""
    blk = record_to_history_block(
        _rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX", role="assistant"),
        "agent_recall", 0,
    )
    assert blk.content == "### 会话目标\nX"
    assert PROGRESS_SO_FAR_HEADING not in blk.content


def test_plain_user_prompt_not_wrapped():
    blk = record_to_history_block(_rec(T.USER_PROMPT, "你好"), "task_conversation", 0)
    assert blk.content == "你好"


def test_agent_conversation_turn_not_wrapped():
    """胶囊里的 assistant summary 是 AGENT_CONVERSATION_TURN，不应被包装。"""
    blk = record_to_history_block(_rec(T.AGENT_CONVERSATION_TURN, "### 会话目标\nX", role="assistant"),
                                  "agent_experience", 0)
    assert blk.content == "### 会话目标\nX"


from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.protocols import ProviderContext


class _Mem:
    def __init__(self, recs): self._recs = recs
    async def recall_recent(self, scope, types, limit, ctx): return self._recs
    async def recall_recent_by_agent(self, agent_scope, types, limit, ctx): return []


@pytest.mark.asyncio
async def test_agent_compact_summary_rendered_wrapped():
    rec = _rec(T.AGENT_COMPACT_SUMMARY, "### 既往派发摘要\nY")
    deps = SimpleNamespace(memory=_Mem([rec]), provider_ctx=ProviderContext(session_id="s1", tenant_id="default"))
    req = SimpleNamespace(scope=SimpleNamespace())
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]
    summ = [b for b in blocks if b.metadata.get("type") == T.AGENT_COMPACT_SUMMARY]
    assert summ and summ[0].content.startswith(COMPACT_SUMMARY_WRAPPER_PREFIX)
