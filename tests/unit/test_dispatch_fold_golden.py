"""端到端：compact 过的 root task close 后，agent 层渲染序符合新胶囊形态（Task 6 §3.3）。

新形态（Task 6 重写后）：
  TASK_COMPACT_SUMMARY（task 层，保留）
  + AGENT_CONVERSATION_TURN×N（镜像 task 层幸存对话，时间序）
  + AGENT_CONVERSATION_TURN assistant(finish_task tool_call) + tool(Process Report)
不再写 TASK_DISPATCH / TASK_DISPATCH_RESULT 作为 root 自残留。
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
async def test_capsule_renders_interleaved_turns_and_finish_pair():
    """compact 过的 root task close 后：
    task 层 TASK_COMPACT_SUMMARY 仍存在，agent 层写出交错时间线 AGENT_CONVERSATION_TURN。
    末尾是 assistant(finish_task tool_call) + tool(Process Report)。
    不再写 TASK_DISPATCH / TASK_DISPATCH_RESULT 作为 root 自残留。
    """
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # task 层：一条 UP + 一条 TASK_COMPACT_SUMMARY（存活，保留供 AgentRecallSource 读）
    tscope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=tscope,
                                 content="把 ppt 转 pdf", timestamp=_BASE, role="user",
                                 metadata={}), _pctx())
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tscope,
                                 content="### 会话目标\n转 PDF", timestamp=_BASE, role="user",
                                 metadata={}), _pctx())
    # 在 agent scope 合成胶囊（新形态）
    await _synthesize_dispatch_pair(mem, scope, _task(), "## PDF 已完成", "success", _pctx())

    deps = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    req = SimpleNamespace(scope=scope)
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]
    blocks.sort(key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)))
    types = [b.metadata.get("type") for b in blocks]

    # task 层 TASK_COMPACT_SUMMARY 仍由 AgentRecallSource 读到（recall_recent_by_agent）
    assert T.TASK_COMPACT_SUMMARY in types

    # agent 层全是 AGENT_CONVERSATION_TURN（不再有 TASK_DISPATCH / TASK_DISPATCH_RESULT 作为 root 残留）
    assert T.AGENT_CONVERSATION_TURN in types
    assert T.TASK_DISPATCH not in types or all(
        b.metadata.get("type") != T.TASK_DISPATCH
        for b in blocks
        if b.metadata.get("origin_task_id") == "t1"
    ), "root self-residue must not use TASK_DISPATCH anymore"

    # 检验交错时间线：UP → assistant(summary) → assistant(finish_task) → tool(Process Report)
    turn_blocks = [b for b in blocks if b.metadata.get("type") == T.AGENT_CONVERSATION_TURN]
    roles = [b.metadata.get("role") for b in turn_blocks]
    # 4 turns: user(UP) + assistant(summary) + assistant(finish_task) + tool(Process Report)
    assert len(turn_blocks) == 4, f"expected 4 AGENT_CONVERSATION_TURN, got {len(turn_blocks)}: {roles}"
    assert roles[0] == "user"
    assert roles[1] == "assistant"
    assert roles[2] == "assistant"
    assert roles[3] == "tool"

    # user turn carries original prompt
    assert "把 ppt 转 pdf" in turn_blocks[0].content

    # assistant summary turn carries compaction content (TASK_COMPACT_SUMMARY → assistant)
    assert "会话目标" in turn_blocks[1].content

    # finish_task tool_call
    finish_tc = turn_blocks[2].metadata.get("tool_calls", [])
    assert finish_tc and finish_tc[0].get("name", "").endswith("finish_task")

    # Process Report
    assert turn_blocks[3].content.startswith("Process Report:")
