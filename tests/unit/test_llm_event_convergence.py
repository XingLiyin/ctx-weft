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


# ── 复审修复轮：compact 也要放行 + 补收尾事件；_maybe_predispatch_compact 的 origin ──
#
# 控制方复审发现：
#   1. gateway 的 origin 门禁原来只放行 LOOP_ACT，但 compact（summarize_for_compact）此前
#      一个 LLM_* 事件都不发，放行它是纯增量，不存在 observe/background_observe 那种重复
#      发射/串号风险——理应一并放行。
#   2. 放行 compact 后，summarize_for_compact 必须补发 LLM_RESPONSE_FINISHED，否则
#      REQUEST_STARTED 有了收尾却永远不来，host SSE 看到一条挂死请求。
#   3. act.py::_maybe_predispatch_compact 在 Act 步骤执行期间调用 compact，driver 只在步骤
#      边界切 origin，此刻仍是 LOOP_ACT——必须临时切到 LOOP_COMPACT，调用结束（含异常）后
#      还原，否则这次「实际是 compact 摘要」的调用会被门禁错当成 act 的一次 LLM turn。


class _FakeCompactAssembler:
    async def assemble(self, req):
        return SimpleNamespace(system="SYS-COMPACT", messages=[], tools=[])


def _make_compact_state(*, origin: str = EventOrigin.LOOP_COMPACT):
    agent = SimpleNamespace(
        id="a1", runtime={"llm_model": "mock-model"},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    task = SimpleNamespace(id="t1", parent_task_id="p1", status="RUNNING")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
    from ctx_weft.core.loop.driver import LoopState
    return LoopState(
        run_id="run-test", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None, "bound_capabilities": []},
        resolved_model=SimpleNamespace(model="mock-model", account="mock-acct"),
        origin=origin,
    )


def _make_compact_ctx(event_bus):
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.core.loop.driver import LoopContext

    return LoopContext(
        assembler=_FakeCompactAssembler(),
        llm=_FakeLLM(),
        memory=InMemoryMemoryProvider(),
        event_bus=event_bus,
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="t1", agent_id="a1"),
    )


@pytest.mark.asyncio
async def test_gateway_emits_stream_events_for_compact_origin_and_pairs_with_response_finished():
    """origin==LOOP_COMPACT 放行 4 个流式事件；summarize_for_compact 补发的
    LLM_RESPONSE_FINISHED 与它们共享同一个 request_id（同 act.py 那条回归的镜像用例）。"""
    from ctx_weft.core.loop.steps.compact import summarize_for_compact

    bus = _RecordingBus()
    state = _make_compact_state()
    ctx = _make_compact_ctx(bus)

    out = await summarize_for_compact(state, ctx, scope="task")

    assert out == "hello"
    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED]
    prompt_sent = [e for e in bus.events if e.type == EventType.LLM_PROMPT_SENT]
    tokens = [e for e in bus.events if e.type == EventType.LLM_TOKEN_STREAMED]
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED]
    assert len(started) == 1 and len(prompt_sent) == 1 and len(finished) == 1
    assert len(tokens) == 2

    rid = started[0].payload["request_id"]
    assert rid
    assert prompt_sent[0].payload["request_id"] == rid
    assert finished[0].payload["request_id"] == rid
    assert all(e.payload["request_id"] == rid for e in tokens)
    assert finished[0].payload["content"] == "hello"
    assert finished[0].payload["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_compact_events_carry_resolved_model_not_mock_sentinel():
    """task-4 复审第二轮：summarize_for_compact 的 llm_request.model 曾用
    agent.runtime.get("llm_model", "mock")——inline compact 路径下 agent.runtime 从不会被
    写入 llm_model，恒回落 "mock"。改用 resolve_llm_identity(state) 后，llm_request.model
    与 LLM_REQUEST_STARTED/LLM_RESPONSE_FINISHED 里的 model/llm_account 都必须是
    state.resolved_model 解出的真实值，不是 "mock" 字面量。这里刻意让 agent.runtime 为空
    （模拟正常任务执行 materialize() 出来的 Agent，没有人给它填过 llm_model），
    resolved_model 给一个与 "mock" 明显不同的真实模型名，钉住两者不再混用。"""
    from ctx_weft.core.loop.steps.compact import summarize_for_compact

    bus = _RecordingBus()
    state = _make_compact_state()
    state.agent.runtime = {}  # 正常任务执行下 agent.runtime 就是这样——不含 llm_model
    state.resolved_model = SimpleNamespace(model="claude-real-model", account="acct-real")
    ctx = _make_compact_ctx(bus)

    await summarize_for_compact(state, ctx, scope="task")

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED][0]
    assert started.payload["model"] == "claude-real-model"
    assert started.payload["llm_account"] == "acct-real"
    assert finished.payload["llm_model"] == "claude-real-model"
    assert finished.payload["llm_account"] == "acct-real"
    assert "mock" not in started.payload["model"]
    assert "mock" not in finished.payload["llm_model"]


@pytest.mark.asyncio
async def test_gateway_still_gates_out_observe_origin_and_compact_stays_silent_too():
    """门禁维持不放行 observe/background_observe（Task 5 前的临时脚手架，不在本轮改动范围）；
    summarize_for_compact 万一在这种 origin 下被调用，也不该发孤儿 LLM_RESPONSE_FINISHED。"""
    from ctx_weft.core.loop.steps.compact import summarize_for_compact

    bus = _RecordingBus()
    state = _make_compact_state(origin=EventOrigin.LOOP_OBSERVE)
    ctx = _make_compact_ctx(bus)

    await summarize_for_compact(state, ctx, scope="task")

    assert [e for e in bus.events if e.type in _GATEWAY_TYPES] == []


@pytest.mark.asyncio
async def test_predispatch_compact_swaps_origin_to_compact_and_restores_on_success(monkeypatch):
    from ctx_weft.core.loop.steps.act import _maybe_predispatch_compact
    from ctx_weft.core.loop import capability_gateway as _cap_mod
    from ctx_weft.core.loop.steps import compact as _compact_mod

    dispatch_tool_name = next(iter(_cap_mod.DISPATCH_TOOLS))
    tool_calls = [SimpleNamespace(name=dispatch_tool_name, id="tc1", arguments={})]
    seen_origin = {}

    async def _fake_maybe_compact(state, ctx, *, prompt_tokens):
        seen_origin["value"] = state.origin
        return []

    monkeypatch.setattr(_compact_mod, "maybe_compact_before_dispatch", _fake_maybe_compact)
    bus = _RecordingBus()
    state = _make_state()  # origin=LOOP_ACT，同 _run_llm_turn 测试用的那份夹具
    ctx = _make_ctx(bus)

    await _maybe_predispatch_compact(state, ctx, tool_calls, LLMUsage(prompt_tokens=1))

    assert seen_origin["value"] == EventOrigin.LOOP_COMPACT
    assert state.origin == EventOrigin.LOOP_ACT  # 调用结束后已还原


@pytest.mark.asyncio
async def test_predispatch_compact_restores_origin_even_on_exception(monkeypatch):
    from ctx_weft.core.loop.steps.act import _maybe_predispatch_compact
    from ctx_weft.core.loop import capability_gateway as _cap_mod
    from ctx_weft.core.loop.steps import compact as _compact_mod

    dispatch_tool_name = next(iter(_cap_mod.DISPATCH_TOOLS))
    tool_calls = [SimpleNamespace(name=dispatch_tool_name, id="tc1", arguments={})]

    async def _boom(state, ctx, *, prompt_tokens):
        raise RuntimeError("boom")

    monkeypatch.setattr(_compact_mod, "maybe_compact_before_dispatch", _boom)
    bus = _RecordingBus()
    state = _make_state()
    ctx = _make_ctx(bus)

    with pytest.raises(RuntimeError, match="boom"):
        await _maybe_predispatch_compact(state, ctx, tool_calls, LLMUsage(prompt_tokens=1))

    assert state.origin == EventOrigin.LOOP_ACT  # finally 里还原，异常路径也不例外
