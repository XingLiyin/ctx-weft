"""段折的 user 感知语义（v2 P4a：apply_compact 直调迁移 segment_fold）。

- user 回合永不被折（段锚点）；前段 raw 不跨段折入（段作用域）
- TASK 层段摘要 role=assistant（LLM 自述）；AGENT 层摘要 role=user（prompt 首条，
  Anthropic 首条 assistant 会 400）
"""
import pytest
from datetime import datetime, timedelta, UTC
from ctx_weft.core.loop.steps.segment_fold import segment_fold
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryAddress, MemoryEvent, MemoryEventType, MemoryScope, MemoryAddress,
)
from ctx_weft.protocols.context import ProviderContext


def _ctx():
    return ProviderContext(session_id="s1", tenant_id="t1", task_id="task1", agent_id="a1")


def _scope():
    return MemoryAddress(session_id="s1", task_id="task1", agent_id="a1")


async def _ingest(p, typ, content, ts, role):
    return await p.ingest(
        MemoryEvent(type=typ, address=_scope(), content=content, timestamp=ts, role=role),
        _ctx(),
    )


@pytest.mark.asyncio
async def test_segment_fold_protects_user_prompts_and_prior_segment():
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法1", base + timedelta(seconds=1), "assistant")
    await _ingest(p, MemoryEventType.TOOL_RESULT, "结果1", base + timedelta(seconds=2), "tool")
    await _ingest(p, MemoryEventType.USER_PROMPT, "HITL回复", base + timedelta(seconds=3), "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法2", base + timedelta(seconds=4), "assistant")

    await segment_fold(p, _scope(), MemoryScope.TASK, "段摘要", _ctx())

    recs = await p.recall_recent(
        _scope(),
        [MemoryEventType.USER_PROMPT, MemoryEventType.LLM_RESPONSE,
         MemoryEventType.TOOL_RESULT, MemoryEventType.TASK_COMPACT_SUMMARY],
        100, _ctx(),
    )
    recs = list(reversed(recs))  # newest-first → 时间序
    kinds = [(r.type, r.content) for r in recs]
    # 两条 USER_PROMPT 全留；段作用域：只折当前段（HITL 之后的 想法2），前段 raw 保留
    assert (MemoryEventType.USER_PROMPT, "原始诉求") in kinds
    assert (MemoryEventType.USER_PROMPT, "HITL回复") in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法1") in kinds, "前段 raw 不得跨段折入"
    assert (MemoryEventType.LLM_RESPONSE, "想法2") not in kinds
    # 摘要落在 HITL 之后（当前段位置），不抢占前段
    summary_idx = next(i for i, (t, _) in enumerate(kinds) if t == MemoryEventType.TASK_COMPACT_SUMMARY)
    up2_idx = kinds.index((MemoryEventType.USER_PROMPT, "HITL回复"))
    assert up2_idx < summary_idx


@pytest.mark.asyncio
async def test_task_segment_summary_role_is_assistant():
    """task 层段摘要 = LLM 自述 → role=assistant。"""
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法", base + timedelta(seconds=1), "assistant")

    await segment_fold(p, _scope(), MemoryScope.TASK, "段摘要", _ctx())

    recs = await p.recall_recent(_scope(), [MemoryEventType.TASK_COMPACT_SUMMARY], 100, _ctx())
    assert len(recs) == 1
    assert recs[0].role == "assistant"


@pytest.mark.asyncio
async def test_agent_layer_summary_role_stays_user():
    """agent 层折叠摘要是 prompt 首条，必须 role=user（B 不动）。"""
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    # 新词汇 agent 层回合（带 tool_call 配对，保证 normalize 后可见）
    await p.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=_scope(), content="delegate",
        timestamp=base, role="assistant",
        metadata={"origin_task_id": "c1", "tool_calls": [{"id": "tc1", "name": "control__delegate_task", "input": {}}]},
    ), _ctx())
    await p.ingest(MemoryEvent(
        type=MemoryEventType.AGENT_CONVERSATION_TURN, address=_scope(), content="done",
        timestamp=base + timedelta(seconds=1), role="tool",
        metadata={"origin_task_id": "c1", "tool_call_id": "tc1"},
    ), _ctx())

    await segment_fold(
        p, MemoryAddress(session_id="s1", agent_id="a1"), MemoryScope.AGENT, "派发摘要", _ctx())

    recs = await p.recall_recent(_scope(), [MemoryEventType.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert len(recs) == 1
    assert recs[0].role == "user"
