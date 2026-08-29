"""HITL（Human-in-the-Loop）行为测试。

一个机制、两种 kind（approval / input）、两个触发点：
1. HitlManager 生命周期：request(kind) → approve / answer / reject / 超时；状态机 pending→accepted/rejected/cancelled。
2. 事件：request 发 HitlRequired + SessionPausedHitl；approve→HitlApproved、modify→HitlModified、
   answer→HitlAnswered、reject→HitlRejected。超时→热→冷驱逐（HitlPark），不再是终态。
3. 触发点 A：HumanConfirmationAuthorizer（approval 门控）端到端。
4. 触发点 B：ask_user 控制工具（input 提问）端到端。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ctx_weft.core.auth import HumanConfirmationAuthorizer
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.orchestrator.control_capability import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
)
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols import ProviderContext
from ctx_weft.protocols.capability import ToolCapability

# ── Helpers ─────────────────────────────────────────────────────────────────────


async def _request(mgr: HitlManager, form: str = "approval") -> str:
    return await mgr.request(
        form=form, session_id="s1", task_id="tsk_1", capability_id="fs:bash_exec",
        arguments={"command": "ls"}, question="Allow bash?", context="runs shell",
    )


def _collect(bus: InProcessEventBus) -> list[str]:
    seen: list[str] = []

    async def handler(ev):
        seen.append(ev.type)

    bus.subscribe(None, handler)
    return seen


# ── 1/2. 生命周期 + 状态 + 事件 ────────────────────────────────────────────────────


async def test_request_then_approve() -> None:
    mgr = HitlManager()
    rid = await _request(mgr)
    assert mgr.list_pending() and mgr.list_pending()[0].id == rid
    assert mgr.get(rid).form == "approval" and mgr.get(rid).status == "pending"

    await mgr.approve(rid)
    req = await mgr.wait(rid)
    assert req.status == "accepted" and req.accepted is True
    assert mgr.list_pending() == []


async def test_reject_carries_message() -> None:
    mgr = HitlManager()
    rid = await _request(mgr)
    await mgr.reject(rid, message="run ls first instead")
    req = await mgr.wait(rid)
    assert req.status == "rejected" and req.message == "run ls first instead"


async def test_approve_with_modified_arguments() -> None:
    mgr = HitlManager()
    rid = await _request(mgr)
    await mgr.approve(rid, modified_arguments={"command": "ls -la"})
    req = await mgr.wait(rid)
    assert req.status == "accepted"
    assert req.modified_arguments == {"command": "ls -la"}


async def test_answer_input_kind() -> None:
    mgr = HitlManager()
    rid = await _request(mgr, form="question")
    await mgr.answer(rid, "use postgres")
    req = await mgr.wait(rid)
    assert req.status == "accepted" and req.message == "use postgres"


async def test_wait_timeout_raises_park_and_keeps_pending() -> None:
    from ctx_weft.core.loop.park import HitlPark
    mgr = HitlManager(timeout_sec=0)
    rid = await _request(mgr)
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    assert mgr.get(rid).status == "pending"


async def test_answer_after_timeout_resolves_cold() -> None:
    from ctx_weft.core.loop.park import HitlPark
    mgr = HitlManager(timeout_sec=0)
    rid = await _request(mgr, form="question")
    with pytest.raises(HitlPark):
        await mgr.wait(rid)
    resolved, was_hot = await mgr.resolve_answer(rid, "late")
    assert resolved.status == "accepted" and was_hot is False


async def test_wait_unknown_id_raises() -> None:
    mgr = HitlManager()
    with pytest.raises(KeyError):
        await mgr.wait("hit_does_not_exist")


async def test_list_pending_filters_by_session() -> None:
    mgr = HitlManager()
    a1 = await mgr.request(form="question", session_id="s1", task_id="", question="q1")
    await mgr.request(form="question", session_id="s2", task_id="", question="q2")
    assert [p.id for p in mgr.list_pending(session_id="s1")] == [a1]


async def test_request_emits_required_and_paused() -> None:
    bus = InProcessEventBus()
    seen = _collect(bus)
    await _request(HitlManager(event_bus=bus))
    assert "HitlRequired" in seen and "SessionPausedHitl" in seen


async def test_resolution_events_per_kind() -> None:
    bus = InProcessEventBus()
    seen = _collect(bus)
    mgr = HitlManager(event_bus=bus)

    await mgr.approve(await _request(mgr))                                  # HitlApproved
    await mgr.approve(await _request(mgr), modified_arguments={"x": 1})     # HitlModified
    await mgr.answer(await _request(mgr, form="question"), "ans")             # HitlAnswered
    await mgr.reject(await _request(mgr))                                   # HitlRejected

    for t in ("HitlApproved", "HitlModified", "HitlAnswered", "HitlRejected"):
        assert t in seen, t


# ── 3. 触发点 A：HumanConfirmationAuthorizer（approval 门控）端到端 ───────────────────


def _cap():
    return ToolCapability(id="fs:bash_exec", name="bash_exec", description="run shell")


async def _filter_with_response(mgr: HitlManager, respond) -> list:
    authorizer = HumanConfirmationAuthorizer(hitl_manager=mgr)
    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1")
    ftask = asyncio.create_task(
        authorizer.filter([_cap()], ctx, {"command": "ls"})
    )
    req = await _await_pending(mgr)
    assert req.form == "approval"
    await respond(req.id)
    return await ftask


async def _await_pending(mgr: HitlManager):
    for _ in range(200):
        pend = mgr.list_pending()
        if pend:
            return pend[0]
        await asyncio.sleep(0)
    raise AssertionError("no pending HITL request appeared")


async def test_authorizer_approve_passes() -> None:
    mgr = HitlManager()
    out = await _filter_with_response(mgr, lambda rid: mgr.approve(rid))
    assert [c.id for c in out] == ["fs:bash_exec"]


async def test_authorizer_reject_blocks() -> None:
    mgr = HitlManager()
    out = await _filter_with_response(mgr, lambda rid: mgr.reject(rid))
    assert out == []


async def _authorize_with_response(mgr: HitlManager, respond):
    authorizer = HumanConfirmationAuthorizer(hitl_manager=mgr)
    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1")
    dtask = asyncio.create_task(
        authorizer.authorize(_cap(), ctx, {"command": "ls"})
    )
    req = await _await_pending(mgr)
    await respond(req.id)
    return await dtask


async def test_authorize_approve_with_modify_flows_through() -> None:
    mgr = HitlManager()
    d = await _authorize_with_response(
        mgr, lambda rid: mgr.approve(rid, modified_arguments={"command": "ls -la"})
    )
    assert d.allowed is True
    assert d.modified_arguments == {"command": "ls -la"}


async def test_authorize_reject_with_message_flows_through() -> None:
    mgr = HitlManager()
    d = await _authorize_with_response(
        mgr, lambda rid: mgr.reject(rid, message="use the read-only tool instead")
    )
    assert d.allowed is False
    assert d.message == "use the read-only tool instead"


# ── 4/5. 触发点 B/C：控制工具的 input 提问端到端 ────────────────────────────────────


def _control_provider(mgr: HitlManager):
    provider = ControlCapabilityProvider(hitl_manager=mgr)
    session = Session(id="s1", tenant_id="default", user_prompt="do it", status="RUNNING")
    task = Task(id="tsk_1", session_id="s1", status="ACTIVE", title="T1")
    tm = SimpleNamespace(get_task=lambda tid: task, reopen_chain=None)
    provider.register_session("s1", tm, session)
    return provider, session, task


async def _invoke_control(provider, name: str, args: dict):
    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1")
    parts: list[str] = []

    async def drain():
        async for ev in provider.invoke(f"{PROVIDER_NAME}:{name}", args, ctx):
            if ev.kind == "result":
                parts.append(ev.payload.get("content", ""))

    return asyncio.create_task(drain()), parts


async def test_ask_user_returns_answer() -> None:
    mgr = HitlManager()
    provider, session, _ = _control_provider(mgr)
    task_h, parts = await _invoke_control(
        provider, "ask_user", {"questions": [{"question": "Which DB?"}]}
    )
    req = await _await_pending(mgr)
    assert req.form == "question"
    assert session.status == "PAUSED_HITL"      # park 期间会话挂起
    await mgr.answer(req.id, "use postgres")
    await task_h
    assert parts and "use postgres" in parts[0]
    assert session.status == "RUNNING"          # 应答后恢复


async def test_ask_user_propagates_structured_questions() -> None:
    """ask_user 的 questions(含 options/multi_select)透传到 HitlRequest,供 host 投影渲染。"""
    mgr = HitlManager()
    provider, _, _ = _control_provider(mgr)
    questions = [
        {"question": "Which DB?", "options": [
            {"label": "pgvector", "description": "reuse PG", "recommended": True},
            {"label": "Qdrant"},
        ], "multi_select": False},
        {"question": "Enable which features?", "options": [{"label": "cache"}], "multi_select": True},
    ]
    task_h, _ = await _invoke_control(provider, "ask_user", {"questions": questions})
    req = await _await_pending(mgr)
    assert req.questions == questions
    await mgr.answer(req.id, "1. Which DB? → pgvector\n2. Enable which features? → cache")
    await task_h


async def test_ask_user_rejected() -> None:
    mgr = HitlManager()
    provider, _, _ = _control_provider(mgr)
    task_h, parts = await _invoke_control(
        provider, "ask_user", {"questions": [{"question": "ok?"}]}
    )
    req = await _await_pending(mgr)
    await mgr.reject(req.id)
    await task_h
    assert parts and "rejected" in parts[0].lower()


async def test_ask_user_reject_feeds_message_back() -> None:
    """拒绝时人类的指导反馈应回灌为工具结果，驱动 agent 改方向。"""
    mgr = HitlManager()
    provider, _, _ = _control_provider(mgr)
    task_h, parts = await _invoke_control(
        provider, "ask_user", {"questions": [{"question": "delete prod DB?"}]}
    )
    req = await _await_pending(mgr)
    await mgr.reject(req.id, message="never touch prod; use the staging copy")
    await task_h
    assert parts and "staging copy" in parts[0]


def test_hitl_cancelled_is_registered_event() -> None:
    from ctx_weft.protocols.events import EVENT_TYPES, EventType
    assert EventType.HITL_CANCELLED == "HitlCancelled"
    assert "HitlCancelled" in EVENT_TYPES


async def test_request_stores_tool_call_id() -> None:
    mgr = HitlManager()
    rid = await mgr.request(
        form="approval", session_id="s1", task_id="t1",
        capability_id="fs:bash_exec", tool_call_id="tc_42", question="ok?",
    )
    assert mgr.get(rid).tool_call_id == "tc_42"


async def test_cancel_moves_to_cancelled_and_emits() -> None:
    bus = InProcessEventBus()
    seen = _collect(bus)
    mgr = HitlManager(event_bus=bus)
    rid = await _request(mgr, form="question")
    await mgr.cancel(rid)
    assert mgr.get(rid).status == "cancelled"
    assert mgr.list_pending() == []
    assert "HitlCancelled" in seen


async def test_cancel_is_idempotent_after_resolve() -> None:
    mgr = HitlManager()
    rid = await _request(mgr, form="question")
    await mgr.answer(rid, "done")
    await mgr.cancel(rid)                 # 已解决 → no-op
    assert mgr.get(rid).status == "accepted"


async def test_request_idempotent_by_tool_call_id_pending() -> None:
    mgr = HitlManager()
    rid1 = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcX")
    rid2 = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcX")
    assert rid1 == rid2                      # 同一请求，不新建
    assert len(mgr.list_pending()) == 1


async def test_request_idempotent_by_tool_call_id_resolved_no_future() -> None:
    mgr = HitlManager()
    rid = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcY")
    await mgr.answer(rid, "answered")
    # 重新请求同一 tool_call_id（cold reconcile 再入）：返回已解决记录、不重置状态
    rid2 = await mgr.request(form="question", session_id="s1", task_id="t1", tool_call_id="tcY")
    assert rid2 == rid
    assert mgr.get(rid2).status == "accepted" and mgr.get(rid2).message == "answered"


def test_find_for_tool_call_returns_latest() -> None:
    mgr = HitlManager()
    assert mgr.find_for_tool_call("nope") is None


async def test_request_no_tool_call_id_not_deduped() -> None:
    mgr = HitlManager()
    r1 = await mgr.request(form="question", session_id="s1", task_id="t1")  # 空 tool_call_id
    r2 = await mgr.request(form="question", session_id="s1", task_id="t1")
    assert r1 != r2                          # 空 id 不去重


async def _invoke_control_with_tcid(provider, name, args, tool_call_id):
    ctx = ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1",
                          agent_id="agt_1", extra={"tool_call_id": tool_call_id})
    parts: list[str] = []
    async def drain():
        async for ev in provider.invoke(f"{PROVIDER_NAME}:{name}", args, ctx):
            if ev.kind == "result":
                parts.append(ev.payload.get("content", ""))
    return asyncio.create_task(drain()), parts


async def test_ask_user_short_circuits_resolved_hitl() -> None:
    """cold reconcile 再入：tool_call_id 已有 answered HITL → 直接用答复、不再 park。"""
    mgr = HitlManager()
    provider, session, _ = _control_provider(mgr)
    rid = await mgr.request(form="question", session_id="s1", task_id="tsk_1", tool_call_id="tc_re")
    await mgr.answer(rid, "use postgres")
    task_h, parts = await _invoke_control_with_tcid(
        provider, "ask_user", {"questions": [{"question": "Which DB?"}]}, "tc_re",
    )
    await asyncio.wait_for(task_h, timeout=1.0)   # 不应阻塞
    assert parts and "use postgres" in parts[0]
    assert len(mgr.list_pending()) == 0           # 未新建 pending


# ── resolved 请求 GC（防 _requests 无界增长）+ 驱逐超时配置接线（spec/07 §2/§9）────────


async def test_resolved_requests_are_gc_pruned() -> None:
    mgr = HitlManager(max_resolved=2)
    r1 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="a")
    await mgr.answer(r1, "x")
    r2 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="b")
    await mgr.answer(r2, "x")
    r3 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="c")
    await mgr.answer(r3, "x")
    # max_resolved=2 → 最旧的已解决（r1）被裁剪，近两条保留
    assert mgr.get(r1) is None
    assert mgr.get(r2) is not None and mgr.get(r3) is not None


async def test_gc_never_prunes_pending() -> None:
    mgr = HitlManager(max_resolved=1)
    p1 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="p1")
    p2 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="p2")
    r1 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="r1")
    await mgr.answer(r1, "x")
    r2 = await mgr.request(form="question", session_id="s", task_id="t", tool_call_id="r2")
    await mgr.answer(r2, "x")
    # pending 永不裁剪；已解决裁到 1
    assert mgr.get(p1) is not None and mgr.get(p2) is not None
    assert len([r for r in (r1, r2) if mgr.get(r) is not None]) == 1


def test_hitl_config_defaults() -> None:
    """RuntimeConfig default leaves hitl_timeout_sec as None (no eviction)."""
    from ctx_weft.core.config import RuntimeConfig
    cfg = RuntimeConfig()
    assert cfg.hitl_timeout_sec is None
    assert cfg.hitl_max_resolved == 1000


def test_runtime_wires_hitl_timeout() -> None:
    """CtxWeftRuntime injects hitl knobs from RuntimeConfig."""
    from ctx_weft.core.config import RuntimeConfig
    from ctx_weft.core import CtxWeftRuntime
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
    cfg = RuntimeConfig(hitl_timeout_sec=45)
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider(), config=cfg)
    assert rt.hitl_manager._timeout_sec == 45


async def test_cancel_event_payload_carries_the_real_message() -> None:
    """`HitlCancelled` 的 payload 必须带真实 message，不得是 None。

    HITL_* 参与状态重建（reducers 折叠决定缓存），载荷丢成 None 就等于把「为什么被
    取消」从重放流里抹掉。生产调用方是熔断取消（`runtime.py` 传
    ``message="failure_threshold"``），所以这条不是理论缺口。

    形状上的坑：`_resolve` 的判据是 `if req.message:`（真值），载荷却是另一条参数链
    递进来的——两者一旦脱节，就会写出「message 为真、载荷为 None」这种自相矛盾的
    payload，而只断言事件类型的既有用例照样绿。
    """
    bus = InProcessEventBus()
    events: list = []

    async def handler(ev):
        events.append(ev)

    bus.subscribe(None, handler)
    mgr = HitlManager(event_bus=bus)
    rid = await _request(mgr, form="question")

    await mgr.cancel(rid, message="failure_threshold")

    cancelled = [e for e in events if e.type == "HitlCancelled"]
    assert len(cancelled) == 1
    assert cancelled[0].payload["message"] == "failure_threshold"
