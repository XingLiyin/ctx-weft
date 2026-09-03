from ctx_weft.core.content import content_to_text, redact_content_for_event
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart

_LONG_B64 = "QUJDRA==" * 500          # 模拟真实图片的体量


def _img():
    return ImagePart(data=_LONG_B64, media_type="image/png")


def _redact(messages):
    """复刻 act.py / observe.py 的 payload 构造表达式（改造后形态）。"""
    return [{"role": m.role, "content": redact_content_for_event(m.content)}
            for m in messages]


def test_plain_text_payload_unchanged():
    msgs = [LLMMessage(role="user", content="hello")]
    assert _redact(msgs) == [{"role": "user", "content": "hello"}]


def test_base64_never_reaches_event_payload():
    msgs = [LLMMessage(role="user", content=[TextPart(text="看图"), _img()])]
    blob = _redact(msgs)[0]["content"]
    assert _LONG_B64 not in blob, "完整 base64 不得进事件 payload"
    assert len(blob) < 200, "脱敏后应是短标记，不是几万字符"
    assert "看图" in blob and "image/png" in blob


def test_recognize_intent_gets_text_not_empty():
    """recognize_intent 的 else "" 会把整条多模态消息丢空。"""
    m = LLMMessage(role="user", content=[TextPart(text="看图"), _img()])
    assert content_to_text(m.content) == "看图"


# ── 真实驱动：从实际的 LLM_PROMPT_SENT / RECOGNIZE_INTENT_LLM_PROMPT 事件里断言 ──
#
# 复用 tests/unit/test_llm_request_identity.py（act/observe）与
# tests/unit/test_recognize_intent_fill.py（recognize_intent）已有的最小 fake 夹具，
# 直接驱动到 make_event(..., payload={"messages": [...]}) 那一行，而不是只测
# redact_content_for_event 本身。


from types import SimpleNamespace

import ctx_weft.core.loop.steps.observe as _obs_mod
from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.steps.act import _run_llm_turn
from ctx_weft.core.loop.steps.observe import run_observe_react
from ctx_weft.core.loop.steps.recognize_intent import RecognizeIntentStep
from ctx_weft.protocols import LLMUsage, MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import ToolCapability
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


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


def _make_state(*, origin: str = EventOrigin.LOOP_ACT):
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
    return LoopState(
        run_id="run-test", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None, "bound_capabilities": []},
        resolved_model=SimpleNamespace(model="mock", account=""),
        # act 走真实 llm_gateway.stream_llm_resilient（task 4 后 LLM_PROMPT_SENT 由 gateway
        # 发射，且只在 origin==LOOP_ACT 时发射）。
        origin=origin,
    )


class _FakeLLM:
    """脚本化 LLMClient；context_limit 是 stream_llm_resilient 入口
    apply_dynamic_max_tokens 的硬需求（task 4 后 act 不再 monkeypatch 掉整个
    stream_llm_resilient，会真正跑到这段）。"""
    tokenizer = HeuristicTokenizer()
    context_limit = 100_000

    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    async def complete(self, req, stream=True):
        for c in self._chunks:
            yield c


def _make_ctx(event_bus, *, llm=None):
    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    return LoopContext(
        assembler=_FakeAssembler(),
        llm=llm if llm is not None else _FakeLLM(),
        memory=InMemoryMemoryProvider(),
        event_bus=event_bus,
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="t1", agent_id="a1"),
        capability_gateway=_FakeGateway(),
    )


async def test_act_prompt_sent_event_has_no_base64():
    bus = _RecordingBus()
    state = _make_state()
    ctx = _make_ctx(bus, llm=_FakeLLM(chunks=[_make_usage_chunk()]))
    prompt = SimpleNamespace(system="SYS", tools=[])
    current_messages = [LLMMessage(role="user", content=[TextPart(text="看图"), _img()])]

    await _run_llm_turn(state, ctx, prompt, current_messages, 1)

    sent = [e for e in bus.events if e.type == EventType.LLM_PROMPT_SENT][0]
    blob = sent.payload["messages"][0]["content"]
    assert _LONG_B64 not in blob
    assert "看图" in blob and "image/png" in blob


async def test_observe_prompt_sent_event_has_no_base64(monkeypatch):
    async def _fake_stream(ctx, state, request):
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)
    bus = _RecordingBus()
    state = _make_state()
    ctx = _make_ctx(bus)

    await run_observe_react(
        state, ctx, system="SYS",
        messages=[LLMMessage(role="user", content=[TextPart(text="看图"), _img()])],
        tools=[], request_id_prefix="t", max_rounds=1,
        terminal_tool_name="report_task_outcome",
    )

    sent = [e for e in bus.events if e.type == EventType.LLM_PROMPT_SENT][0]
    blob = sent.payload["messages"][0]["content"]
    assert _LONG_B64 not in blob
    assert "看图" in blob and "image/png" in blob


async def test_recognize_intent_prompt_event_keeps_text_not_empty():
    """真实驱动到 RECOGNIZE_INTENT_LLM_PROMPT：多模态消息的文本不该被丢空。"""

    class _FakeAssembler:
        async def assemble(self, request):
            return SimpleNamespace(
                system="SYS",
                messages=[LLMMessage(role="user", content=[TextPart(text="看图"), _img()])],
                tools=[],
            )

    class _FakeLLM:
        tokenizer = HeuristicTokenizer()

        async def complete(self, request, stream=True):
            yield SimpleNamespace(kind="usage", usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
                                   tool_call=None, text="")

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    cap = ToolCapability(id="cap1", name="update_task_metadata", purposes=["recognize_intent"])
    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(),
        capability_gateway=_FakeGateway(),
        event_bus=SimpleNamespace(emit=_emit),
    )
    state = SimpleNamespace(
        run_id="r1",
        sequence_counter=0,
        agent=SimpleNamespace(id="a1", runtime={"llm_model": "mock"}),
        session=SimpleNamespace(id="s1", tenant_id="te1"),
        task=SimpleNamespace(id="t1", title="", settings=SimpleNamespace()),
        scope=SimpleNamespace(session_id="s1", task_id="t1", agent_id="a1"),
        extra={"template": SimpleNamespace(), "bound_capabilities": [cap]},
        resolved_model=SimpleNamespace(model="mock", account=""),
    )

    await RecognizeIntentStep().execute(state, ctx)

    prompt_ev = [e for e in emitted if e.type == EventType.RECOGNIZE_INTENT_LLM_PROMPT][0]
    content = prompt_ev.payload["messages"][0]["content"]
    assert content != ""
    assert "看图" in content
