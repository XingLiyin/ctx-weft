import pytest
from datetime import datetime, timedelta, UTC
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryLayer, MemoryScope,
)
from ctx_weft.protocols.context import ProviderContext


def _ctx():
    return ProviderContext(session_id="s1", tenant_id="t1", task_id="task1", agent_id="a1")


def _scope():
    return MemoryScope(session_id="s1", task_id="task1", agent_id="a1")


async def _ingest(p, typ, content, ts, role):
    return await p.ingest(
        MemoryEvent(type=typ, scope=_scope(), content=content, timestamp=ts, role=role),
        _ctx(),
    )


@pytest.mark.asyncio
async def test_apply_compact_task_protects_user_prompt():
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法1", base + timedelta(seconds=1), "assistant")
    await _ingest(p, MemoryEventType.TOOL_RESULT, "结果1", base + timedelta(seconds=2), "tool")
    await _ingest(p, MemoryEventType.USER_PROMPT, "HITL回复", base + timedelta(seconds=3), "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法2", base + timedelta(seconds=4), "assistant")

    await p.apply_compact(
        scope=_scope(), summary="段摘要", keep_last=0, ctx=_ctx(),
        layer=MemoryLayer.TASK, protect_types=(MemoryEventType.USER_PROMPT,),
    )

    recs = await p.recall_recent(
        _scope(),
        [MemoryEventType.USER_PROMPT, MemoryEventType.LLM_RESPONSE,
         MemoryEventType.TOOL_RESULT, MemoryEventType.TASK_COMPACT_SUMMARY],
        100, _ctx(),
    )
    recs = list(reversed(recs))  # newest-first → 时间序
    kinds = [(r.type, r.content) for r in recs]
    # 两条 USER_PROMPT 全留、LLM/TOOL 被折成摘要、摘要落在原始诉求之后 HITL 之前
    assert (MemoryEventType.USER_PROMPT, "原始诉求") in kinds
    assert (MemoryEventType.USER_PROMPT, "HITL回复") in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法1") not in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法2") not in kinds
    summary_idx = next(i for i, (t, _) in enumerate(kinds) if t == MemoryEventType.TASK_COMPACT_SUMMARY)
    up1_idx = kinds.index((MemoryEventType.USER_PROMPT, "原始诉求"))
    up2_idx = kinds.index((MemoryEventType.USER_PROMPT, "HITL回复"))
    assert up1_idx < summary_idx < up2_idx
