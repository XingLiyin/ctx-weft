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
