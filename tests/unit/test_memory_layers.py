"""分层 memory 行为测试（spec/06）。

锁定 in_memory provider 的 layer 语义：
- task 层按 task_id 隔离；agent 层按 agent_id 跨 task 累积
- apply_compact(TASK) 只折叠 task 层，不动 agent 层；apply_compact(AGENT) 写 AGENT_COMPACT_SUMMARY
- 过渡期跨层召回宽容合并（按 timestamp）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.protocols import (
    EVENT_LAYER,
    MemoryEvent,
    MemoryEventType,
    MemoryLayer,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str, agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


def _ev(type_: MemoryEventType, scope: MemoryScope, content: str, t: int = 0) -> MemoryEvent:
    return MemoryEvent(
        type=type_,
        scope=scope,
        content=content,
        timestamp=_BASE + timedelta(seconds=t),
    )


async def test_task_layer_isolated_per_task() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_ev(T.USER_PROMPT, _sc("tA"), "promptA"), _ctx())
    await m.ingest(_ev(T.USER_PROMPT, _sc("tB"), "promptB"), _ctx())

    recs_a = await m.recall_recent(_sc("tA"), [T.USER_PROMPT], 10, _ctx())
    assert [r.content for r in recs_a] == ["promptA"]  # 不含 tB


async def test_agent_layer_accumulates_across_tasks() -> None:
    m = InMemoryMemoryProvider()
    # agent 层按 agent_id 归集，忽略 task_id
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, _sc("tA"), "r1"), _ctx())
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, _sc("tB"), "r2"), _ctx())

    recs = await m.recall_recent(_sc("tZ"), [T.TASK_DISPATCH_RESULT], 10, _ctx())
    assert sorted(r.content for r in recs) == ["r1", "r2"]


async def test_task_compact_folds_task_layer_only() -> None:
    m = InMemoryMemoryProvider()
    sc = _sc("tA")
    await m.ingest(_ev(T.USER_PROMPT, sc, "u", t=0), _ctx())
    await m.ingest(_ev(T.LLM_RESPONSE, sc, "a1", t=1), _ctx())
    await m.ingest(_ev(T.LLM_RESPONSE, sc, "a2", t=2), _ctx())
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, "agent-exp", t=3), _ctx())  # agent 层

    await m.apply_compact(sc, "SUMMARY", keep_last=1, ctx=_ctx(), layer=MemoryLayer.TASK)

    task_recs = await m.recall_recent(
        sc, [T.USER_PROMPT, T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY], 10, _ctx()
    )
    assert any(r.type == T.TASK_COMPACT_SUMMARY and r.content == "SUMMARY" for r in task_recs)
    assert "u" not in [r.content for r in task_recs]  # 旧转录被折叠
    assert "a2" in [r.content for r in task_recs]      # keep_last=1 保留最近一条
    # recall 倒序：保留的 a2(最新) 在前，摘要逻辑置前 → 在后
    assert [r.content for r in task_recs] == ["a2", "SUMMARY"]

    # agent 层不受 task compact 影响
    ag = await m.recall_recent(sc, [T.TASK_DISPATCH_RESULT], 10, _ctx())
    assert [r.content for r in ag] == ["agent-exp"]


async def test_agent_compact_writes_agent_summary() -> None:
    m = InMemoryMemoryProvider()
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, _sc("tA"), "r1", t=0), _ctx())
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, _sc("tB"), "r2", t=1), _ctx())

    await m.apply_compact(_sc("tA"), "AGSUM", keep_last=0, ctx=_ctx(), layer=MemoryLayer.AGENT)

    recs = await m.recall_recent(
        _sc("tZ"), [T.TASK_DISPATCH_RESULT, T.AGENT_COMPACT_SUMMARY], 10, _ctx()
    )
    assert any(r.type == T.AGENT_COMPACT_SUMMARY and r.content == "AGSUM" for r in recs)


async def test_mixed_layer_recall_merges_by_timestamp() -> None:
    m = InMemoryMemoryProvider()
    sc = _sc("tA")
    await m.ingest(_ev(T.USER_PROMPT, sc, "u", t=0), _ctx())          # task 层
    await m.ingest(_ev(T.TASK_DISPATCH_RESULT, sc, "d", t=1), _ctx())  # agent 层

    recs = await m.recall_recent(sc, [T.USER_PROMPT, T.TASK_DISPATCH_RESULT], 10, _ctx())
    # newest-first
    assert [r.content for r in recs] == ["d", "u"]


def test_agent_conversation_turn_is_agent_layer() -> None:
    assert MemoryEventType.AGENT_CONVERSATION_TURN in EVENT_LAYER
    assert EVENT_LAYER[MemoryEventType.AGENT_CONVERSATION_TURN] is MemoryLayer.AGENT
