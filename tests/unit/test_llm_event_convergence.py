"""Task 4：4 种「流式侧」LLM_* 事件收敛到 llm_gateway.stream_llm_resilient。

范围裁定（task-4-brief.md 全量 6 种 → 收窄为 4 种）：
  - 搬进 gateway：LLM_REQUEST_STARTED / LLM_PROMPT_SENT / LLM_TOKEN_STREAMED /
    LLM_REASONING_STREAMED。
  - 留在 act.py：LLM_RESPONSE_FINISHED（payload 依赖软打断决策，见 brief）。
  - 本就在 gateway：LLM_RETRY_TRIGGERED（_emit_retry，Task 3 已加 origin）。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop import llm_gateway
from ctx_weft.core.loop.steps import act
from ctx_weft.core.loop.steps.act import _run_llm_turn
from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.protocols import LLMUsage, MemoryAddress
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer

_MOVED_TYPES = {
    EventType.LLM_REQUEST_STARTED,
    EventType.LLM_PROMPT_SENT,
    EventType.LLM_TOKEN_STREAMED,
    EventType.LLM_REASONING_STREAMED,
}
_GATEWAY_TYPES = _MOVED_TYPES | {EventType.LLM_RETRY_TRIGGERED}


def test_act_no_longer_emits_llm_events():
    """act.py 源码里不应再出现搬走的 4 种 LLM_* 类型；LLM_RESPONSE_FINISHED 仍应保留。"""
    src = inspect.getsource(act)
    leaked = sorted(t.name for t in _MOVED_TYPES if f"EventType.{t.name}" in src)
    assert leaked == [], f"act.py 仍在发射：{leaked}"
    assert "EventType.LLM_RESPONSE_FINISHED" in src, "LLM_RESPONSE_FINISHED 不应被搬走"


def test_gateway_emits_moved_and_retry_events():
    """5 种事件（搬来的 4 个 + 原有的 LLM_RETRY_TRIGGERED）全部由 gateway 发射。"""
    src = inspect.getsource(llm_gateway)
    missing = sorted(t.name for t in _GATEWAY_TYPES if f"EventType.{t.name}" not in src)
    assert missing == [], f"gateway 未接管：{missing}"


# ── request_id 一致性回归（task-4 最容易出错的地方）─────────────────────────────
#
# LLM_REQUEST_STARTED / LLM_PROMPT_SENT 现由 gateway 发射，LLM_RESPONSE_FINISHED 仍由
# act.py 发射——两侧各自用同一个确定性公式 f"req_{agent.id}_{state.sequence_counter}"
# 独立算出 request_id，都在「本次 LLM 调用的任何事件被发射之前」求值，因此理应相等。
# 本测试直接跑一次真实 _run_llm_turn（不 monkeypatch stream_llm_resilient），验证
# 三者的 payload["request_id"] 确实一致——防止两处公式后续被改得不一致而不被察觉。


class _RecordingBus:
    def __init__(self):
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _FakeLLM:
    """脚本化 LLMClient：先吐两个 token chunk，再吐一个 usage chunk。"""
    tokenizer = HeuristicTokenizer()
    context_limit = 100_000

    async def complete(self, req, stream=True):
        yield SimpleNamespace(kind="token", text="he", tool_call=None, usage=None)
        yield SimpleNamespace(kind="token", text="llo", tool_call=None, usage=None)
        yield SimpleNamespace(
            kind="usage", text="", tool_call=None,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
        )


def _make_state():
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(max_turns_per_observe=3, compact_keep_last=2),
        runtime={},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
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
        resolved_model=SimpleNamespace(model="mock-model", account="mock-acct"),
        origin=EventOrigin.LOOP_ACT,
    )


def _make_ctx(event_bus):
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.core.loop.driver import LoopContext

    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeGateway:
        async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id=None):
            raise AssertionError("no tool call expected in this test")

    return LoopContext(
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        memory=InMemoryMemoryProvider(),
        event_bus=event_bus,
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="t1", agent_id="a1"),
        capability_gateway=_FakeGateway(),
    )


@pytest.mark.asyncio
async def test_request_started_prompt_sent_response_finished_share_request_id():
    bus = _RecordingBus()
    state = _make_state()
    ctx = _make_ctx(bus)
    prompt = SimpleNamespace(system="SYS", tools=[])

    await _run_llm_turn(state, ctx, prompt, [], 1)

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED]
    prompt_sent = [e for e in bus.events if e.type == EventType.LLM_PROMPT_SENT]
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED]
    assert len(started) == 1 and len(prompt_sent) == 1 and len(finished) == 1

    rid = started[0].payload["request_id"]
    assert rid  # 非空
    assert prompt_sent[0].payload["request_id"] == rid
    assert finished[0].payload["request_id"] == rid

    # 顺带确认流式事件也确实搬到了 gateway 侧（TOKEN_STREAMED 携带同一 request_id）。
    tokens = [e for e in bus.events if e.type == EventType.LLM_TOKEN_STREAMED]
    assert len(tokens) == 2
    assert all(e.payload["request_id"] == rid for e in tokens)
