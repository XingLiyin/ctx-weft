"""端到端：root task close 后，AgentRecallSource 渲染序符合 task-resident 胶囊形态。

task-resident（spec 2026-06-28 §3.3）：
  body 留 task 层（USER_PROMPT / TASK_COMPACT_SUMMARY，由 recall_recent_by_agent 读）
  + agent 层 finish 对（AGENT_CONVERSATION_TURN assistant finish_task + tool Process Report）
  → composer 按 (timestamp, seq_no) 归并出 [user][assistant 摘要][finish 对]。
不再镜像 body 进 agent 层，也不写 TASK_DISPATCH / TASK_DISPATCH_RESULT 作为 root 自残留。
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource
from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryScope, ProviderContext,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _pctx(): return ProviderContext(session_id="s1", tenant_id="default")
def _sc(agent="ag1"): return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _task():
    return Task(id="t1", session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id=None,
                title="PPTX转PDF", description="转 PDF", user_prompt="把 ppt 转 pdf",
                settings=NormalTaskSettings())


@pytest.mark.asyncio
async def test_capsule_renders_body_and_finish_pair():
    """task-resident：root task close 后：
    task 层 USER_PROMPT + TASK_COMPACT_SUMMARY 留 task 层（由 recall_recent_by_agent 读），
    agent 层只写 finish 对（assistant finish_task + tool Process Report）。
    composer 按 (timestamp, seq_no) 归并 → [user][assistant 摘要][finish 对]。
    不再镜像 body，也不写 TASK_DISPATCH / TASK_DISPATCH_RESULT 作为 root 自残留。
    """
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # task 层：一条 UP + 一条 TASK_COMPACT_SUMMARY（留 task 层，供 AgentRecallSource 读）
    tscope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=tscope,
                                 content="把 ppt 转 pdf", timestamp=_BASE, role="user",
                                 metadata={}), _pctx())
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tscope,
                                 content="### 会话目标\n转 PDF", timestamp=_BASE, role="assistant",
                                 metadata={}), _pctx())
    # 在 agent scope 合成 finish 对（task-resident：不镜像 body）
    await _synthesize_dispatch_pair(mem, scope, _task(), "## PDF 已完成", "success", _pctx())

    deps = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    req = SimpleNamespace(scope=scope)
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]
    blocks.sort(key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)))
    types = [b.metadata.get("type") for b in blocks]

    # task 层 body 类型仍由 AgentRecallSource 读到（recall_recent_by_agent）
    assert T.USER_PROMPT in types
    assert T.TASK_COMPACT_SUMMARY in types

    # finish 对在 agent 层（AGENT_CONVERSATION_TURN）；无 TASK_DISPATCH root 自残留
    assert T.AGENT_CONVERSATION_TURN in types
    assert T.TASK_DISPATCH not in types, "root self-residue must not use TASK_DISPATCH anymore"

    # 归并序：user(UP) → assistant(summary) → assistant(finish_task) → tool(Process Report)
    history = [b for b in blocks if b.metadata.get("type") in (
        T.USER_PROMPT, T.TASK_COMPACT_SUMMARY, T.AGENT_CONVERSATION_TURN)]
    roles = [b.metadata.get("role") for b in history]
    assert len(history) == 4, f"expected 4 history blocks (body + finish pair), got {len(history)}: {roles}"
    assert roles == ["user", "assistant", "assistant", "tool"], f"got roles={roles}"

    # user block carries original prompt (task layer)
    assert "把 ppt 转 pdf" in history[0].content
    # assistant summary block carries compaction content (TASK_COMPACT_SUMMARY, task layer)
    assert "会话目标" in history[1].content
    # finish_task tool_call (agent layer finish pair)
    finish_tc = history[2].metadata.get("tool_calls", [])
    assert finish_tc and finish_tc[0].get("name", "").endswith("finish_task")
    # Process Report (agent layer)
    assert history[3].content.startswith("Process Report:")
