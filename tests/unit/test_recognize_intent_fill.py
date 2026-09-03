"""RecognizeIntentStep fill path: reuses state.extra['bound_capabilities']; routes update_task_metadata."""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.recognize_intent import RecognizeIntentStep
from ctx_weft.protocols import LLMChunk, LLMUsage, ToolCall
from ctx_weft.protocols.capability import ToolCapability
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


class _FakeAssembler:
    def __init__(self):
        self.calls = []

    async def assemble(self, request):
        self.calls.append(request)
        return SimpleNamespace(system="SYS", messages=[], tools=[])


class _FakeLLM:
    def __init__(self, args):
        self._args = args
        self.tokenizer = HeuristicTokenizer()

    async def complete(self, request, stream=True):
        yield LLMChunk(
            kind="tool_call",
            tool_call=ToolCall(id="tc1", name="update_task_metadata", arguments=self._args),
        )
        yield LLMChunk(
            kind="usage",
            usage=LLMUsage(prompt_tokens=50, completion_tokens=8, total_tokens=58),
        )


class _FakeGateway:
    def __init__(self):
        self.invocations = []

    async def invoke(self, *, tool_name, arguments, state, ctx):
        self.invocations.append({"tool_name": tool_name, "arguments": arguments})


def _state(bound):
    return SimpleNamespace(
        run_id="r1",
        sequence_counter=0,
        agent=SimpleNamespace(id="a1", runtime={"llm_model": "mock"}),
        session=SimpleNamespace(id="s1", tenant_id="te1"),
        task=SimpleNamespace(id="t1", title="", settings=SimpleNamespace()),
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        extra={"template": SimpleNamespace(), "bound_capabilities": bound},
        resolved_model=SimpleNamespace(model="mock", account=""),
    )


async def test_fills_metadata_via_tool_call():
    args = {"title": "Build the thing", "description": "A clear description", "session_goal": "Ship it"}
    cap = ToolCapability(id="cap1", name="update_task_metadata", purposes=["recognize_intent"])

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    gateway = _FakeGateway()
    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(args),
        capability_gateway=gateway,
        event_bus=SimpleNamespace(emit=_emit),
    )

    state = _state([cap])
    outcome = await RecognizeIntentStep().execute(state, ctx)

    assert outcome.next_step is None
    assert len(gateway.invocations) == 1
    assert gateway.invocations[0]["tool_name"] == "update_task_metadata"
    assert gateway.invocations[0]["arguments"] == args

    types = [ev.type for ev in emitted]
    assert "RecognizeIntentStarted" in types
    assert "RecognizeIntentToolCall" in types
    assert "RecognizeIntentCompleted" in types
    assert "RecognizeIntentSkipped" not in types

    # The assemble request carried the full bound set with the recognize_intent purpose.
    req = ctx.assembler.calls[0]
    assert req.purpose == "recognize_intent"
    assert req.bound_capabilities == [cap]


async def test_skips_when_no_metadata_tool_in_bound_set():
    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM({}),
        capability_gateway=_FakeGateway(),
        event_bus=SimpleNamespace(emit=_emit),
    )
    state = _state([])  # no caps at all
    outcome = await RecognizeIntentStep().execute(state, ctx)

    assert outcome.next_step is None
    assert "RecognizeIntentSkipped" in [ev.type for ev in emitted]
    assert ctx.assembler.calls == []  # never assembled


async def test_completed_payload_carries_usage():
    """意图识别的 LLM 开销此前无账可查——usage 透进 COMPLETED payload（只透出不记账）。"""
    args = {"title": "T", "description": "D", "session_goal": "G"}
    cap = ToolCapability(id="cap1", name="update_task_metadata", purposes=["recognize_intent"])

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(args),
        capability_gateway=_FakeGateway(),
        event_bus=SimpleNamespace(emit=_emit),
    )
    await RecognizeIntentStep().execute(_state([cap]), ctx)

    completed = [ev for ev in emitted if ev.type == "RecognizeIntentCompleted"]
    assert completed
    u = completed[0].payload["usage"]
    assert u["prompt_tokens"] == 50
    assert u["input_tokens"] == 50   # 无缓存信息 → 派生 = prompt
    assert u["completion_tokens"] == 8
