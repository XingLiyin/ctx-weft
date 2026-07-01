from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.observe import ObserveStep
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext

pytestmark = pytest.mark.asyncio

_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _pctx():
    return ProviderContext(session_id="s", tenant_id="tn")


async def _ingest(mem, scope, typ, content, i, role="user"):
    await mem.ingest(MemoryEvent(type=typ, scope=scope, content=content,
                                 timestamp=_BASE + timedelta(seconds=i), role=role,
                                 metadata={"task_id": scope.task_id}), _pctx())


async def test_retry_folds_current_attempt_and_deletes_raw():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "原始请求", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "本轮回复", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "本轮工具结果", 2, role="tool")

    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="t1"),
                            agent=SimpleNamespace(id="a", loop_config=SimpleNamespace(compact_keep_last=6)),
                            session=SimpleNamespace(id="s", tenant_id="tn"),
                            run_id="r1", sequence_counter=0)
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    verdict = SimpleNamespace(task_outcome="retry", act_recap="本段摘要：调了工具X", reported=False)

    events = []
    await ObserveStep()._fold_retry_segment(state, ctx, verdict, events)

    recs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE,
                                           T.TOOL_RESULT, T.TASK_COMPACT_SUMMARY], 100, _pctx())
    types = {r.type for r in recs}
    # 本轮 raw 被折掉，USER_PROMPT 锚保留，新增一条段摘要
    assert T.LLM_RESPONSE not in types and T.TOOL_RESULT not in types
    assert T.USER_PROMPT in types
    summ = [r for r in recs if r.type == T.TASK_COMPACT_SUMMARY]
    assert len(summ) == 1 and summ[0].content == "本段摘要：调了工具X" and summ[0].role == "assistant"


async def test_non_retry_outcome_does_not_fold():
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.LLM_RESPONSE, "回复", 1, role="assistant")
    state = SimpleNamespace(scope=scope, task=SimpleNamespace(id="t1"),
                            agent=SimpleNamespace(loop_config=SimpleNamespace(compact_keep_last=6)),
                            session=SimpleNamespace())
    ctx = SimpleNamespace(memory=mem, provider_ctx=_pctx())
    verdict = SimpleNamespace(task_outcome="success", act_recap="done", reported=True)
    events = []
    await ObserveStep()._fold_retry_segment(state, ctx, verdict, events)
    recs = await mem.recall_recent(scope, [T.LLM_RESPONSE, T.TASK_COMPACT_SUMMARY], 100, _pctx())
    assert any(r.type == T.LLM_RESPONSE for r in recs)  # 未折
    assert not any(r.type == T.TASK_COMPACT_SUMMARY for r in recs)


def test_should_use_llm_forces_on_context_limit():
    template = SimpleNamespace(identity={"observe": object()})
    state = SimpleNamespace(
        extra={"template": template},
        act_exit_reason="context_limit",
        task=SimpleNamespace(parent_task_id=None))  # root
    assert ObserveStep()._should_use_llm(state) is True
