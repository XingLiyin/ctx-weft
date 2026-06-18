"""Runtime: pause_session = soft (PauseToken), cancel_session = hard (CancelToken + cancel-all)."""

from types import SimpleNamespace

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.tokens import CancelToken, PauseToken
from ctx_weft.core.orchestrator.hitl_manager import HitlRequest
from ctx_weft.core.state.models import Session
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryScope, ProviderContext
from ctx_weft.protocols.memory import MemoryEvent, MemoryEventType
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio


def _runtime():
    return CtxWeftRuntime(llm=MockLLMAdapter(responses=[]), template_resolver=InMemoryTemplateResolver())


async def test_pause_session_pauses_pause_token():
    rt = _runtime()
    pause = PauseToken()
    rt._pause_tokens["s1"] = pause
    assert rt.pause_session("s1") is True
    assert pause.is_paused is True


async def test_cancel_session_cancels_token_and_drains_queue():
    rt = _runtime()
    tok = CancelToken()
    rt._cancel_tokens["s1"] = tok
    drained = {"called": False}

    class _TM:
        async def cancel_all(self, *, reason=""):
            drained["called"] = True

    rt._task_managers["s1"] = _TM()
    assert await rt.cancel_session("s1") is True
    assert tok.is_cancelled is True
    assert drained["called"] is True


async def test_unknown_session_returns_false():
    rt = _runtime()
    assert rt.pause_session("nope") is False
    assert await rt.cancel_session("nope") is False


@pytest.mark.asyncio
async def test_inject_user_reply_phase1_adds_edit_note():
    rt = _runtime()
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)
    session = Session(id="s1", tenant_id="default", user_prompt="X", status="PAUSED", token_budget=0)
    task = SimpleNamespace(id="t1", status="SUSPENDED", outputs=None, process_report=None)
    tm = SimpleNamespace(get_task=lambda tid: task)
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.USER_PROMPT, scope=scope, content="原始请求X",
        timestamp=now_utc(), role="user",
    ), pctx)

    req = HitlRequest(
        id="h1", kind="input", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", context="interrupt:edit",
        status="accepted", message="新请求Y",
    )
    await rt._inject_user_reply(req, session, tm)

    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert any(
        "原始请求X" in (r.content or "") and "新请求Y" in (r.content or "") and "取消" in (r.content or "")
        for r in recs
    )


@pytest.mark.asyncio
async def test_inject_user_reply_non_edit_has_no_note():
    rt = _runtime()
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)
    session = Session(id="s1", tenant_id="default", user_prompt="X", status="PAUSED", token_budget=0)
    task = SimpleNamespace(id="t1", status="SUSPENDED", outputs=None, process_report=None)
    tm = SimpleNamespace(get_task=lambda tid: task)
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")

    req = HitlRequest(
        id="h1", kind="input", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", context="interrupt",  # ② not edit
        status="accepted", message="just continue",
    )
    await rt._inject_user_reply(req, session, tm)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert any(r.content == "just continue" for r in recs)
    assert all("取消" not in (r.content or "") for r in recs)
