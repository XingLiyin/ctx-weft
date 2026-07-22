"""Manual compact-only operation over an idle session (Option B + idle-guard)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.errors import SessionBusyError
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.protocols import (
    LoopConfig,
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


def _runtime() -> CtxWeftRuntime:
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="SUMMARY")])
    rt = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())
    return rt


def test_session_busy_error_carries_session_id() -> None:
    err = SessionBusyError("ses_1")
    assert "ses_1" in str(err)


async def test_compact_session_rejects_busy_session() -> None:
    rt = _runtime()
    rt._busy_sessions.add("ses_busy")  # simulate an active drain
    with pytest.raises(SessionBusyError):
        await rt.compact_session("ses_busy")


async def test_compact_session_unknown_session_raises() -> None:
    rt = _runtime()
    with pytest.raises(RuntimeError):
        await rt.compact_session("ses_missing")


async def test_compact_session_folds_agent_layer() -> None:
    resolver = InlineAgentTemplateProvider()
    # small keep_last so a handful of dispatch pairs is over budget
    tmpl = dataclasses.replace(make_echo_template(),
                               loop_config=LoopConfig(compact_keep_last=2))
    resolver.register(tmpl)
    # escalating_compact 预算门总开（compact_session 强制立即压）→ L1 折 agent 层一次调用
    # summarize_for_compact；本例 task_id="" 的当前 task 层无材料可折，L3 guard 拦下、不再空调
    # 第二次 LLM，故只需 1 条 mock 响应。
    llm = MockLLMAdapter(responses=[MockResponse(text="SUMMARY")])
    rt = make_runtime(llm=llm, agent_provider=resolver)
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)

    sid, aid = "ses_c", "agt_root"
    ts = datetime(2026, 6, 16, tzinfo=timezone.utc)
    await rt.event_store.append(Event(
        id="evt_0001", run_id="run_1", sequence=1, session_id=sid,
        type=EventType.SESSION_CREATED, timestamp=ts,
        payload={"template_id": f"agent:{tmpl.id}", "user_prompt": "x", "root_agent_id": aid,
                 "llm_model": "mock", "context_limit": 180000},
    ))

    # 新格式：用 AGENT_CONVERSATION_TURN（parent=None）作 root 胶囊触发 agent 层压缩。
    # scope key ignores task_id, uses agent_id.
    scope = MemoryScope(session_id=sid, task_id="t_seed", agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id="t_seed", agent_id=aid)
    for i in range(5):
        # task 层 body（task_id=root{i}）使每组成为真实 L0 单元（Task-4 §4）
        await mem.ingest(MemoryEvent(
            type=MemoryEventType.USER_PROMPT,
            scope=MemoryScope(session_id=sid, task_id=f"root{i}", agent_id=aid),
            content=f"body {i}", role="user",
            timestamp=ts + timedelta(seconds=i * 10)), pctx)
        await mem.ingest(MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=f"user prompt {i}", role="user",
            timestamp=ts + timedelta(seconds=i * 10),
            metadata={"origin_task_id": f"root{i}", "parent_task_id": None}), pctx)
        await mem.ingest(MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=f"assistant summary {i}", role="assistant",
            timestamp=ts + timedelta(seconds=i * 10 + 1),
            metadata={"origin_task_id": f"root{i}", "parent_task_id": None}), pctx)

    result = await rt.compact_session(sid)

    assert result["session_id"] == sid
    assert result["agent_id"] == aid
    # agent-layer scope key ignores task_id (spec/06 §2), so task_id="" matches the seeded layer
    summaries = await mem.recall_recent(
        scope=MemoryScope(session_id=sid, task_id="", agent_id=aid),
        types=[MemoryEventType.AGENT_COMPACT_SUMMARY], limit=10, ctx=pctx,
    )
    assert len(summaries) >= 1
    assert sid not in rt._busy_sessions
