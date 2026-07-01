from datetime import datetime, timedelta, UTC
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.compact import COLLAPSE_DELIM, collapse_task_layer
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider as InMemoryBlackboard
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType as T, MemoryScope, ProviderContext,
)

pytestmark = pytest.mark.asyncio

_BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _ctx():
    return ProviderContext(session_id="s", tenant_id="tn")


def _state(mem, scope):
    return SimpleNamespace(
        scope=scope, task=SimpleNamespace(id="t1"),
        agent=SimpleNamespace(), session=SimpleNamespace())


async def _ingest(mem, scope, typ, content, i, role="user"):
    await mem.ingest(MemoryEvent(type=typ, scope=scope, content=content,
                                 timestamp=_BASE + timedelta(seconds=i), role=role,
                                 metadata={"task_id": scope.task_id}), _ctx())


async def test_collapse_folds_early_keeps_recent():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "原始请求：做 X", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step1", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r1", 2, role="tool")
    await _ingest(mem, scope, T.LLM_RESPONSE, "step2", 3, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r2", 4, role="tool")

    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    n = await collapse_task_layer(_state(mem, scope), ctx, keep_last=2, summary_text="做了 step1/step2")

    assert n == 3  # 前 3 条被折
    recs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 100, _ctx())
    # newest-first；最老一条应是新坍缩 USER_PROMPT
    by_type = [(r.type, r.content) for r in reversed(recs)]
    assert by_type[0][0] == T.USER_PROMPT
    assert "原始请求：做 X" in by_type[0][1]       # 原始消息节
    assert COLLAPSE_DELIM in by_type[0][1]
    assert "做了 step1/step2" in by_type[0][1]     # 执行摘要节
    # 保留最近 2 条 raw
    assert by_type[1] == (T.LLM_RESPONSE, "step2")
    assert by_type[2] == (T.TOOL_RESULT, "r2")


async def test_collapse_noop_when_within_keep_last():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    await _ingest(mem, scope, T.USER_PROMPT, "orig", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step1", 1, role="assistant")
    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    n = await collapse_task_layer(_state(mem, scope), ctx, keep_last=5, summary_text="x")
    assert n == 0


async def test_recollapse_keeps_original_bounded():
    mem = InMemoryBlackboard()
    scope = MemoryScope(session_id="s", task_id="t1", agent_id="a")
    # 已坍缩过一次的 USER_PROMPT
    await _ingest(mem, scope, T.USER_PROMPT, f"原始请求：做 X{COLLAPSE_DELIM}旧摘要", 0)
    await _ingest(mem, scope, T.LLM_RESPONSE, "step3", 1, role="assistant")
    await _ingest(mem, scope, T.TOOL_RESULT, "r3", 2, role="tool")
    ctx = SimpleNamespace(memory=mem, provider_ctx=_ctx())
    await collapse_task_layer(_state(mem, scope), ctx, keep_last=1, summary_text="新摘要含 step3")

    recs = await mem.recall_recent(scope, [T.USER_PROMPT], 100, _ctx())
    newest = recs[0].content
    assert newest.count("原始请求：做 X") == 1     # 原始节没有嵌套膨胀
    assert "旧摘要" not in newest                   # 旧摘要节被新摘要替掉
    assert "新摘要含 step3" in newest
