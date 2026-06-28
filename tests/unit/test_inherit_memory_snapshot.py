"""inherit_memory：spawn 时把 parent 当前召回快照复制进 child scope（spec 2026-06-23）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.runtime import _copy_memory_for_inherit
from ctx_weft.core.state.models import Agent, NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _ev(type_, scope, content, t, role=None) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role)


async def test_inherit_copies_parent_recall_into_child_scope() -> None:
    mem = InMemoryMemoryProvider()
    parent_scope = MemoryScope(session_id="s1", task_id="p1", agent_id="ag1")
    await mem.ingest(_ev(T.USER_PROMPT, parent_scope, "parent ask", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, parent_scope, "parent reply", 1, role="assistant"), _ctx())

    parent_task = Task(id="p1", session_id="s1", status="SUSPENDED", assigned_agent_id="ag1",
                       creator_agent_id="ag1", title="P", settings=NormalTaskSettings())
    child_task = Task(id="c1", session_id="s1", status="PENDING", assigned_agent_id="ag2",
                      creator_agent_id="ag1", parent_task_id="p1", title="C",
                      settings=NormalTaskSettings(inherit_memory=True))
    sub_agent = Agent(id="ag2", session_id="s1", template_id="t", template_version="1",
                      status="IDLE", parent_agent_id="ag1")

    await _copy_memory_for_inherit(parent_task, child_task, sub_agent, mem, "s1", "default")

    child_scope = MemoryScope(session_id="s1", task_id="c1", agent_id="ag2")
    turns = await mem.recall_recent(child_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    contents = {t.content for t in turns}
    assert contents == {"parent ask", "parent reply"}
    # original parent events untouched
    parent_recs = await mem.recall_recent(parent_scope, [T.USER_PROMPT, T.LLM_RESPONSE], 100, _ctx())
    assert len(parent_recs) == 2


async def test_inherit_preserves_assistant_segment_summary() -> None:
    """parent 段摘要（role=assistant）inherit 后透传为 child 的 assistant 回合，
    且不误加 tool_calls；user 锚点恒在其前（段摘要非首条，R1 守护）。"""
    mem = InMemoryMemoryProvider()
    parent_scope = MemoryScope(session_id="s1", task_id="p1", agent_id="ag1")
    await mem.ingest(_ev(T.USER_PROMPT, parent_scope, "原始诉求", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, parent_scope, "段①摘要", 1, role="assistant"), _ctx())

    parent_task = Task(id="p1", session_id="s1", status="SUSPENDED", assigned_agent_id="ag1",
                       creator_agent_id="ag1", title="P", settings=NormalTaskSettings())
    child_task = Task(id="c1", session_id="s1", status="PENDING", assigned_agent_id="ag2",
                      creator_agent_id="ag1", parent_task_id="p1", title="C",
                      settings=NormalTaskSettings(inherit_memory=True))
    sub_agent = Agent(id="ag2", session_id="s1", template_id="t", template_version="1",
                      status="IDLE", parent_agent_id="ag1")

    await _copy_memory_for_inherit(parent_task, child_task, sub_agent, mem, "s1", "default")

    child_scope = MemoryScope(session_id="s1", task_id="c1", agent_id="ag2")
    turns = list(reversed(await mem.recall_recent(child_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())))
    summary = next(t for t in turns if t.content == "段①摘要")
    assert summary.role == "assistant"
    assert not summary.metadata.get("tool_calls"), "段摘要无 tool_calls，不应误加"
    # 段摘要非首条：其前有 user 锚点
    assert turns[0].role == "user" and turns[0].content == "原始诉求"
