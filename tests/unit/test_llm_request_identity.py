"""LLM 请求事件携带实际使用的 account/model：以 session 为真值（运行时据
session.llm_provider/llm_model 解析 ctx.llm，切换模型同步更新），回退 agent.runtime。

修复背景：LLMRequestStarted 此前恒报 agent.runtime 的 "mock"（runtime 从不填
llm_model），host 云端上报又在响应处理时读"当前会话账号"，与"该次调用实际账号"
存在切换竞态——事件自带真值后两处都有账可对。
"""
from __future__ import annotations

from types import SimpleNamespace

import ctx_weft.core.loop.steps.act as _act_mod
import ctx_weft.core.loop.steps.observe as _obs_mod
from ctx_weft.protocols.events import EventType
from ctx_weft.core.loop.llm_gateway import resolve_llm_identity
from ctx_weft.core.loop.steps.act import _run_llm_turn
from ctx_weft.core.loop.steps.observe import run_observe_react
from ctx_weft.protocols import LLMMessage, LLMUsage, MemoryAddress
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


# ── minimal fakes（与 test_observe_react_helper 同构，自包含避免跨测试文件 import）──


class _RecordingBus:
    def __init__(self):
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _FakeGateway:
    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id=None):
        from ctx_weft.core.orchestrator.control_capability import ControlResult
        return ControlResult(content="DONE")


def _make_usage_chunk():
    return SimpleNamespace(
        kind="usage", usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        tool_call=None, text="",
    )


def _make_tool_call_chunk(name: str):
    return SimpleNamespace(
        kind="tool_call",
        tool_call=SimpleNamespace(id="tc1", name=name, arguments={}),
        text="", usage=None,
    )


def _make_state(*, session_llm: bool):
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(max_turns_per_observe=3, compact_keep_last=2),
        runtime={},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
    if session_llm:
        session = SimpleNamespace(
            id="s1", tenant_id="default", token_used=0,
            llm_provider="deepseek-rj", llm_model="deepseek-v4-pro",
        )
    else:
        # 不带 llm 字段的 session（旧测试夹具形态）→ 走 agent.runtime 回退
        session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    task = SimpleNamespace(
        id="t1", parent_task_id="p1", status="RUNNING",
        observer_outcome=None, process_report=None,
    )
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
    from ctx_weft.core.loop.driver import LoopState
    return LoopState(
        run_id="run-test", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None, "bound_capabilities": []},
    )


def _make_ctx(event_bus):
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.core.loop.driver import LoopContext

    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        tokenizer = HeuristicTokenizer()

        async def complete(self, req, stream=True):
            return
            yield  # unreachable; stream_llm_resilient is monkeypatched

    return LoopContext(
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        memory=InMemoryMemoryProvider(),
        event_bus=event_bus,
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="t1", agent_id="a1"),
        capability_gateway=_FakeGateway(),
    )


# ── resolve_llm_identity 单元语义 ─────────────────────────────────────────────


def test_resolve_prefers_session_truth():
    state = _make_state(session_llm=True)
    model, account = resolve_llm_identity(state)
    assert model == "deepseek-v4-pro"
    assert account == "deepseek-rj"


def test_resolve_falls_back_to_runtime_then_mock():
    state = _make_state(session_llm=False)
    model, account = resolve_llm_identity(state)
    assert model == "mock"          # runtime 也没配 → 兜底哨兵
    assert account == ""
    state.agent.runtime["llm_model"] = "rt-model"
    model, _ = resolve_llm_identity(state)
    assert model == "rt-model"


def test_resolve_ignores_empty_session_values():
    state = _make_state(session_llm=True)
    state.session.llm_model = ""     # 空串视同未设置（session 默认值形态）
    state.session.llm_provider = None
    state.agent.runtime["llm_model"] = "rt-model"
    model, account = resolve_llm_identity(state)
    assert model == "rt-model"
    assert account == ""


# ── act：_run_llm_turn 的请求/响应事件 ────────────────────────────────────────


async def test_act_request_events_carry_session_llm_identity(monkeypatch):
    captured = {}

    async def _fake_stream(ctx, state, request):
        captured["model"] = request.model
        yield _make_usage_chunk()

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    bus = _RecordingBus()
    state = _make_state(session_llm=True)
    ctx = _make_ctx(bus)
    prompt = SimpleNamespace(system="SYS", tools=[])

    await _run_llm_turn(state, ctx, prompt, [], 1)

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "deepseek-v4-pro"
    assert started.payload["llm_account"] == "deepseek-rj"
    assert captured["model"] == "deepseek-v4-pro"   # LLMRequest.model 同源
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED][0]
    assert finished.payload["llm_model"] == "deepseek-v4-pro"
    assert finished.payload["llm_account"] == "deepseek-rj"


async def test_act_request_events_fall_back_without_session_llm(monkeypatch):
    async def _fake_stream(ctx, state, request):
        yield _make_usage_chunk()

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    bus = _RecordingBus()
    state = _make_state(session_llm=False)
    ctx = _make_ctx(bus)

    await _run_llm_turn(state, ctx, SimpleNamespace(system="SYS", tools=[]), [], 1)

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "mock"       # 旧行为兜底
    assert started.payload["llm_account"] == ""


# ── observe：run_observe_react 的请求/响应事件（前台/后台共用路径）───────────────


async def test_observe_request_events_carry_session_llm_identity(monkeypatch):
    captured = {}

    async def _fake_stream(ctx, state, request):
        captured["model"] = request.model
        yield _make_tool_call_chunk("report_task_outcome")
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)
    bus = _RecordingBus()
    state = _make_state(session_llm=True)
    ctx = _make_ctx(bus)

    await run_observe_react(
        state, ctx, system="SYS",
        messages=[LLMMessage(role="user", content="observe")],
        tools=[], request_id_prefix="t", max_rounds=1,
        terminal_tool_name="report_task_outcome",
    )

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "deepseek-v4-pro"
    assert started.payload["llm_account"] == "deepseek-rj"
    assert captured["model"] == "deepseek-v4-pro"
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED][0]
    assert finished.payload["llm_model"] == "deepseek-v4-pro"
    assert finished.payload["llm_account"] == "deepseek-rj"
