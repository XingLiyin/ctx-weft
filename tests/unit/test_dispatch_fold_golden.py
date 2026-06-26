"""端到端：compact 过的 root task close 后，agent 层渲染序符合 §2.2。"""
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
async def test_capsule_renders_user_summary_dispatch_result_in_order():
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # task scope 有一条存活 compaction summary
    tscope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tscope,
                                 content="### 会话目标\n转 PDF", timestamp=_BASE, role="user",
                                 metadata={}), _pctx())
    # 在 agent scope 合成胶囊
    await _synthesize_dispatch_pair(mem, scope, _task(), "## PDF 已完成", "success", _pctx())

    deps = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    req = SimpleNamespace(scope=scope)
    blocks = [b async for b in AgentRecallSource().fetch(req, deps)]
    blocks.sort(key=lambda b: (b.metadata.get("timestamp", ""), b.metadata.get("seq_no", 0)))
    roles = [b.metadata.get("role") for b in blocks]
    types = [b.metadata.get("type") for b in blocks]
    # AgentRecallSource 分两路：
    #   recall_recent_by_agent → task 层 TASK_COMPACT_SUMMARY（wrapped user）
    #   recall_recent → agent 层胶囊四件套：user(原始诉求) → assistant(summary) → assistant(delegate) → tool(result)
    # TASK_COMPACT_SUMMARY 时间戳最早（_BASE），胶囊各件为 now_utc()
    assert T.TASK_COMPACT_SUMMARY in types
    assert T.AGENT_CONVERSATION_TURN in types
    assert T.TASK_DISPATCH in types
    assert T.TASK_DISPATCH_RESULT in types
    # 胶囊中的 user(原始诉求) 和 assistant(summary) 来自 AGENT_CONVERSATION_TURN
    turn_blocks = [b for b in blocks if b.metadata.get("type") == T.AGENT_CONVERSATION_TURN]
    assert len(turn_blocks) == 2
    user_turn = next(b for b in turn_blocks if b.metadata.get("role") == "user")
    asst_turn = next(b for b in turn_blocks if b.metadata.get("role") == "assistant")
    assert "把 ppt 转 pdf" in user_turn.content
    assert "会话目标" in asst_turn.content
    # 结果回合
    result_block = next(b for b in blocks if b.metadata.get("type") == T.TASK_DISPATCH_RESULT)
    assert result_block.content.startswith("## PDF 已完成")
