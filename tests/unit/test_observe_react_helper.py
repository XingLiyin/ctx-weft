"""run_observe_react：跑 ReAct，返回最后一个控制工具的 content；不写 task 状态。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.observe as _obs_mod
from ctx_weft.core.loop.steps.observe import run_observe_react
from ctx_weft.core.orchestrator.control_capability import ControlResult
from ctx_weft.protocols import LLMMessage, LLMUsage, MemoryScope

pytestmark = pytest.mark.asyncio


# ── minimal fakes ─────────────────────────────────────────────────────────────


class _FakeEventBus:
    async def emit(self, event) -> None:
        pass


class _FakeGateway:
    """Returns ControlResult(content=tool_content) for any tool invocation."""

    def __init__(self, tool_content: str):
        self._tool_content = tool_content

    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id):
        return ControlResult(content=self._tool_content)


def _make_tool_call_chunk(name: str, call_id: str = "tc1"):
    """Chunk that looks like a tool_call kind chunk from stream_llm_resilient."""
    return SimpleNamespace(
        kind="tool_call",
        tool_call=SimpleNamespace(id=call_id, name=name, arguments={}),
        text="",
        usage=None,
    )


def _make_token_chunk(text: str):
    return SimpleNamespace(kind="token", text=text, tool_call=None, usage=None)


def _make_usage_chunk(prompt_tokens: int = 10, completion_tokens: int = 5):
    return SimpleNamespace(
        kind="usage",
        usage=LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        tool_call=None,
        text="",
    )


# ── state / ctx factories ─────────────────────────────────────────────────────


def _make_state():
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(max_turns_per_observe=3, compact_keep_last=2),
        runtime={"llm_model": "mock"},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    task = SimpleNamespace(
        id="t1",
        parent_task_id="p1",
        status="RUNNING",
        observer_outcome=None,
        process_report=None,
    )
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="a1")

    from ctx_weft.core.loop.driver import LoopState
    state = LoopState(
        run_id="run-test",
        session=session,
        task=task,
        agent=agent,
        scope=scope,
        extra={"template": None, "bound_capabilities": []},
    )
    return state


def _make_ctx(tool_content: str):
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="a1")

    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        async def complete(self, req, stream=True):
            return
            yield  # unreachable; stream_llm_resilient is monkeypatched in tests

    from ctx_weft.core.loop.driver import LoopContext
    ctx = LoopContext(
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        memory=mem,
        event_bus=_FakeEventBus(),
        provider_ctx=pctx,
        capability_gateway=_FakeGateway(tool_content),
    )
    return ctx


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_helper_returns_tool_content_when_control_tool_called(monkeypatch):
    """Round 1 produces a tool_call; gateway returns ControlResult('REPORT').
    Helper must return ('REPORT', last_text)."""

    async def _fake_stream(ctx, state, request):
        yield _make_token_chunk("thinking...")
        yield _make_tool_call_chunk("report_task_outcome")
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    state = _make_state()
    ctx = _make_ctx(tool_content="REPORT")

    tool_content, last_text = await run_observe_react(
        state, ctx,
        system="SYS",
        messages=[LLMMessage(role="user", content="observe this")],
        tools=[],
        request_id_prefix="test",
        max_rounds=3,
    )

    assert tool_content == "REPORT"


async def test_helper_returns_none_when_no_tool_called(monkeypatch):
    """LLM returns only text (no tool_calls); helper returns (None, last_text)."""

    async def _fake_stream(ctx, state, request):
        yield _make_token_chunk("just text")
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    state = _make_state()
    ctx = _make_ctx(tool_content="IGNORED")

    tool_content, last_text = await run_observe_react(
        state, ctx,
        system="SYS",
        messages=[LLMMessage(role="user", content="observe this")],
        tools=[],
        request_id_prefix="test",
        max_rounds=3,
    )

    assert tool_content is None
    assert last_text == "just text"


async def test_helper_last_text_from_final_round(monkeypatch):
    """When the control tool is called, last_text is the text from that round."""

    async def _fake_stream(ctx, state, request):
        yield _make_token_chunk("final thinking")
        yield _make_tool_call_chunk("report_task_outcome")
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    state = _make_state()
    ctx = _make_ctx(tool_content="DONE")

    tool_content, last_text = await run_observe_react(
        state, ctx,
        system="SYS",
        messages=[LLMMessage(role="user", content="observe this")],
        tools=[],
        request_id_prefix="test",
        max_rounds=3,
    )

    assert tool_content == "DONE"
    assert last_text == "final thinking"


async def test_helper_token_accounting(monkeypatch):
    """session.token_used is accumulated from usage chunks."""

    async def _fake_stream(ctx, state, request):
        yield _make_usage_chunk(prompt_tokens=20, completion_tokens=8)

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    state = _make_state()
    state.session.token_used = 0
    ctx = _make_ctx(tool_content="X")

    await run_observe_react(
        state, ctx,
        system="SYS",
        messages=[],
        tools=[],
        request_id_prefix="test",
        max_rounds=1,
    )

    assert state.session.token_used == 28  # 20 + 8
