"""Runtime: pause_session = 弃子（只留 root agent 那一轮继续跑，其余在途 run 与排队子任务全部弃）；
cancel_session = 硬取消（cancel 全部在途 run + cancel-all，会话终态 CANCELED）。"""

from types import SimpleNamespace

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.orchestrator.hitl_manager import HitlRequest
from ctx_weft.core.state.models import Session
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.protocols.memory import MemoryEvent, MemoryEventType
from ctx_weft.providers.llm.mock import MockLLMAdapter
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime

pytestmark = pytest.mark.asyncio


def _runtime():
    return make_runtime(llm=MockLLMAdapter(responses=[]), agent_provider=InlineAgentTemplateProvider())


async def test_pause_session_without_tm_cancels_all_runs():
    # 无 TM（纯 registry 残留）：无法辨认 root agent → 全部按"其余"cancel，返回 True
    rt = _runtime()
    a = rt._register_run_tokens("s1", "t1")
    b = rt._register_run_tokens("s1", "t2")
    assert await rt.pause_session("s1") is True
    assert a.cancel.is_cancelled and b.cancel.is_cancelled


async def test_cancel_session_cancels_all_run_tokens_and_drains_queue():
    rt = _runtime()
    tokens = rt._register_run_tokens("s1", "t1")
    drained = {"called": False}

    class _TM:
        def is_done(self):
            return False          # active drain in flight → _on_done reclaims after cancel completes

        async def cancel_all(self, *, reason=""):
            drained["called"] = True

    rt._task_managers["s1"] = _TM()
    assert await rt.cancel_session("s1") is True
    assert tokens.cancel.is_cancelled is True
    assert drained["called"] is True


async def test_unknown_session_returns_false():
    rt = _runtime()
    assert await rt.pause_session("nope") is False
    assert await rt.cancel_session("nope") is False


@pytest.mark.asyncio
async def test_inject_user_reply_phase1_adds_edit_note():
    rt = _runtime()
    mem = InMemoryMemoryProvider()
    rt.providers.register_memory(mem)
    session = Session(id="s1", tenant_id="default", user_prompt="X", status="PAUSED", token_budget=0)
    task = SimpleNamespace(id="t1", status="SUSPENDED", outputs=None, process_report=None)
    tm = SimpleNamespace(get_task=lambda tid: task)
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")
    await mem.ingest(MemoryEvent(
        type=MemoryEventType.USER_PROMPT, address=scope, content="原始请求X",
        timestamp=now_utc(), role="user",
    ), pctx)

    req = HitlRequest(
        id="h1", form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", context="interrupt:edit",
        outcome="accepted", message="新请求Y",
    )
    await rt._inject_user_reply(req, session, tm)

    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert any(
        "原始请求X" in (r.content or "") and "新请求Y" in (r.content or "") and "cancelled" in (r.content or "")
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
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1")

    req = HitlRequest(
        id="h1", form="wait", session_id="s1", task_id="t1", agent_id="ag1",
        capability_id="control:wait_for_user", context="interrupt",  # ② not edit
        outcome="accepted", message="just continue",
    )
    await rt._inject_user_reply(req, session, tm)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    assert any(r.content == "just continue" for r in recs)
    assert all("cancelled" not in (r.content or "") for r in recs)
