"""LLM 请求事件携带实际使用的 account/model：真值是 ``state.resolved_model``——
派发时由 ``AgentLifecycleManager.resolve_model`` 解出的那一个（批次 B）。

修复背景：LLMRequestStarted 此前以 session.llm_model/llm_provider 为真值，两级
兜底到 agent.runtime 再到 "mock"（runtime 从不填 llm_model → 恒报 "mock"）。三样
东西（host 的选择 / 解析出的 client / 实际身份）曾挤在 session 一个对象上，选择
可空这件事因此没法表达。现在 resolved_model 由派发方在构造 LoopState 之前算好、
塞入，不再有回退链——没有解析过的值不存在，也就报不出假数据。
"""
from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.protocols.events import EventOrigin, EventType
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
        from ctx_weft.core.capabilities.control_tools import ControlResult
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


def _make_state(
    *, model: str = "deepseek-v4-pro", account: str = "deepseek-rj",
    origin: str = EventOrigin.LOOP_ACT,
):
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
        resolved_model=SimpleNamespace(model=model, account=account),
        # act 走真实 llm_gateway.stream_llm_resilient（task 4 后 LLM_REQUEST_STARTED/
        # PROMPT_SENT 由 gateway 发射，且只在 origin==LOOP_ACT 时发射，见该函数文档字符串）。
        origin=origin,
    )


class _FakeLLM:
    """最小 LLMClient：按脚本 yield chunk。context_limit 是
    llm_gateway.apply_dynamic_max_tokens（stream_llm_resilient 入口即调用）的硬需求——
    此前这些测试整个 monkeypatch 掉 stream_llm_resilient，从未真正跑到这段，现在
    改走真实 gateway 后必须补上。last_request：记录实际发给 LLM client 的 request，
    供测试断言 request.model（与事件 payload 里报的 model 是两回事，见
    test_llm_event_convergence.py 的同名夹具注释）。
    """
    tokenizer = HeuristicTokenizer()
    context_limit = 100_000

    def __init__(self, chunks=()):
        self._chunks = list(chunks)
        self.last_request = None

    async def complete(self, req, stream=True):
        self.last_request = req
        for c in self._chunks:
            yield c


def _make_ctx(event_bus, *, llm=None):
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.core.loop.driver import LoopContext

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


# ── resolve_llm_identity 单元语义 ─────────────────────────────────────────────


def test_resolve_reads_resolved_model():
    state = _make_state(model="deepseek-v4-pro", account="deepseek-rj")
    model, account = resolve_llm_identity(state)
    assert model == "deepseek-v4-pro"
    assert account == "deepseek-rj"


def test_resolve_reports_the_mock_adapters_own_model():
    """未配置真实 provider 时，身份来自 mock adapter 自己公开的 model——不是恒定哨兵。"""
    state = _make_state(model="mock-adapter-model", account="")
    model, account = resolve_llm_identity(state)
    assert model == "mock-adapter-model"
    assert account == ""


# ── act：_run_llm_turn 的请求/响应事件 ────────────────────────────────────────


async def test_act_request_events_carry_resolved_model_identity():
    """act 走真实 llm_gateway.stream_llm_resilient（task 4 后 REQUEST_STARTED 由 gateway
    发射），因此这里不再 monkeypatch stream_llm_resilient——用 _FakeLLM 脚本化 chunk。"""
    bus = _RecordingBus()
    state = _make_state(model="deepseek-v4-pro", account="deepseek-rj")
    ctx = _make_ctx(bus, llm=_FakeLLM(chunks=[_make_usage_chunk()]))
    prompt = SimpleNamespace(system="SYS", tools=[])

    await _run_llm_turn(state, ctx, prompt, [], 1)

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "deepseek-v4-pro"
    assert started.payload["llm_account"] == "deepseek-rj"
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED][0]
    assert finished.payload["llm_model"] == "deepseek-v4-pro"
    assert finished.payload["llm_account"] == "deepseek-rj"


async def test_act_request_events_carry_mock_adapters_model():
    bus = _RecordingBus()
    state = _make_state(model="mock-adapter-model", account="")
    ctx = _make_ctx(bus, llm=_FakeLLM(chunks=[_make_usage_chunk()]))

    await _run_llm_turn(state, ctx, SimpleNamespace(system="SYS", tools=[]), [], 1)

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "mock-adapter-model"
    assert started.payload["llm_account"] == ""


# ── observe：run_observe_react 的请求/响应事件（前台/后台共用路径）───────────────


async def test_observe_request_events_carry_resolved_model_identity():
    """observe 走真实 llm_gateway.stream_llm_resilient（Task 5 后 REQUEST_STARTED 由 gateway
    无条件发射），因此这里不再 monkeypatch stream_llm_resilient——用 _FakeLLM 脚本化 chunk。"""
    bus = _RecordingBus()
    state = _make_state(
        model="deepseek-v4-pro", account="deepseek-rj", origin=EventOrigin.LOOP_OBSERVE)
    llm = _FakeLLM(chunks=[_make_tool_call_chunk("report_task_outcome"), _make_usage_chunk()])
    ctx = _make_ctx(bus, llm=llm)

    await run_observe_react(
        state, ctx, system="SYS",
        messages=[LLMMessage(role="user", content="observe")],
        tools=[], max_rounds=1,
        terminal_tool_name="report_task_outcome",
    )

    started = [e for e in bus.events if e.type == EventType.LLM_REQUEST_STARTED][0]
    assert started.payload["model"] == "deepseek-v4-pro"
    assert started.payload["llm_account"] == "deepseek-rj"
    assert llm.last_request.model == "deepseek-v4-pro"
    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED][0]
    assert finished.payload["llm_model"] == "deepseek-v4-pro"
    assert finished.payload["llm_account"] == "deepseek-rj"


def test_resolve_llm_identity_raises_readable_error_when_unset():
    """resolved_model 未设置时给出可读报错，而不是裸 AttributeError。

    调用方若在 asyncio.create_task 里跑且用无超时的 event.wait() 等待，
    一个不可读的早期异常会表现为「挂起 30 秒无输出」——诊断成本极高。
    """
    import pytest

    state = SimpleNamespace(resolved_model=None)
    with pytest.raises(RuntimeError, match="resolved_model must be set"):
        resolve_llm_identity(state)
