"""Manual compact-only operation over an idle session (Option B + idle-guard)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.tokens import CancelToken
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
    InMemoryTemplateResolver,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio


def _runtime() -> CtxWeftRuntime:
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(text="SUMMARY")])
    rt = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())
    return rt


def test_session_busy_error_carries_session_id() -> None:
    err = SessionBusyError("ses_1")
    assert "ses_1" in str(err)


async def test_compact_session_rejects_busy_session() -> None:
    rt = _runtime()
    rt._cancel_tokens["ses_busy"] = CancelToken()  # simulate an active drain
    with pytest.raises(SessionBusyError):
        await rt.compact_session("ses_busy")


async def test_compact_session_unknown_session_raises() -> None:
    rt = _runtime()
    with pytest.raises(RuntimeError):
        await rt.compact_session("ses_missing")


async def test_compact_session_folds_agent_layer() -> None:
    resolver = InMemoryTemplateResolver()
    # small keep_last so a handful of dispatch pairs is over budget
    tmpl = dataclasses.replace(make_echo_template(), loop_config=LoopConfig(compact_keep_last=2))
    resolver.register(tmpl)
    llm = MockLLMAdapter(responses=[MockResponse(text="SUMMARY")])
    rt = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)

    sid, aid = "ses_c", "agt_root"
    ts = datetime(2026, 6, 16, tzinfo=timezone.utc)
    await rt.event_store.append(Event(
        id="evt_0001", run_id="run_1", sequence=1, session_id=sid,
        type=EventType.SESSION_CREATED, timestamp=ts,
        payload={"template_id": tmpl.id, "user_prompt": "x", "root_agent_id": aid,
                 "llm_model": "mock", "context_limit": 180000},
    ))

    # Seed the agent layer (dispatch log) — scope key ignores task_id, uses agent_id.
    scope = MemoryScope(session_id=sid, task_id="t_seed", agent_id=aid)
    pctx = ProviderContext(session_id=sid, tenant_id="default", task_id="t_seed", agent_id=aid)
    for i in range(5):
        await mem.ingest(MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH, scope=scope, content=f"dispatch {i}",
            timestamp=ts, role="assistant", metadata={"tool_call_id": f"tc{i}"}), pctx)
        await mem.ingest(MemoryEvent(
            type=MemoryEventType.TASK_DISPATCH_RESULT, scope=scope, content=f"result {i}",
            timestamp=ts, role="tool", metadata={"tool_call_id": f"tc{i}"}), pctx)

    result = await rt.compact_session(sid)

    assert result["session_id"] == sid
    assert result["agent_id"] == aid
    # agent-layer scope key ignores task_id (spec/06 §2), so task_id="" matches the seeded layer
    summaries = await mem.recall_recent(
        scope=MemoryScope(session_id=sid, task_id="", agent_id=aid),
        types=[MemoryEventType.AGENT_COMPACT_SUMMARY], limit=10, ctx=pctx,
    )
    assert len(summaries) >= 1
    assert sid not in rt._cancel_tokens
