"""act._run_llm_turn 的 token 自校准回喂：行为级测试锁定 I1 修复。

回喂必须用 request.metadata[PROMPT_EST_SEG_KEY]（floor 前的真实估算段），而非
prompt_token_estimate − base——整份路径的返回值在 context_tokens 远大于整份估算时会被
max(full, ctx_tokens) floor 成上一轮真实值；若拿 floor 后的值回喂，ratio≈1 的假样本会把
伺服系统性拖向 1（正是 I1 要修的污染场景，也是 request_prompt_estimate 存在的校准动机）。

脚手架自包含复制自 tests/unit/test_llm_request_identity.py 的 _make_state/_make_ctx 模式，
不跨文件 import。fake llm 的 tokenizer 用实例属性（不用类属性——避免多个 fake 实例间共享
可变校准状态）。
"""
from __future__ import annotations

from types import SimpleNamespace

import ctx_weft.core.loop.steps.act as _act_mod
from ctx_weft.core.loop.llm_gateway import PROMPT_EST_SEG_KEY, _estimate_request_tokens
from ctx_weft.protocols import LLMUsage, MemoryAddress
from ctx_weft.core.loop.steps.act import _run_llm_turn
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


# ── minimal fakes（与 test_llm_request_identity._make_state/_make_ctx 同构）───────


class _RecordingBus:
    def __init__(self):
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _FakeGateway:
    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id=None):
        from ctx_weft.core.capabilities.control_tools import ControlResult
        return ControlResult(content="DONE")


class _SpyTokenizer(HeuristicTokenizer):
    """真实 HeuristicTokenizer 行为 + 记录 observe 调用参数，供断言回喂用了哪个值。"""

    def __init__(self) -> None:
        super().__init__()
        self.observe_calls: list[tuple[int, int]] = []

    def observe(self, estimated: int, actual: int) -> None:
        self.observe_calls.append((estimated, actual))
        super().observe(estimated, actual)


def _make_usage_chunk(prompt_tokens: int, completion_tokens: int = 5):
    return SimpleNamespace(
        kind="usage",
        usage=LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
        tool_call=None, text="",
    )


def _make_state(*, context_tokens: int = 0):
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(max_turns_per_observe=3, compact_keep_last=2),
        runtime={},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=context_tokens),
    )
    session = SimpleNamespace(
        id="s1", tenant_id="default", token_used=0,
        llm_provider="deepseek-rj", llm_model="deepseek-v4-pro",
    )
    task = SimpleNamespace(
        id="t1", parent_task_id="p1", status="RUNNING",
        observer_outcome=None, process_report=None,
        # act 的提交点（spec 2026-09-09）会调 persist_user_prompt——这两个字段是它的判据。
        # 本文件只测一次 LLM turn，没有待落库的 user_prompt：标成「已落库」即 no-op。
        user_prompt=None, user_prompt_in_memory=True,
    )
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
    from ctx_weft.core.loop.driver import LoopState
    return LoopState(
        run_id="run-test", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None, "bound_capabilities": []},
        resolved_model=SimpleNamespace(model="deepseek-v4-pro", account="deepseek-rj"),
    )


def _make_ctx(event_bus, tokenizer: HeuristicTokenizer):
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.core.loop.driver import LoopContext

    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
        def __init__(self, tok: HeuristicTokenizer) -> None:
            self.tokenizer = tok  # 实例属性，非类属性——各测试各自的 fake 各学各的

        async def complete(self, req, stream=True):
            return
            yield  # unreachable; stream_llm_resilient is monkeypatched

    return LoopContext(
        assembler=_FakeAssembler(),
        llm=_FakeLLM(tokenizer),
        memory=InMemoryMemoryProvider(),
        event_bus=event_bus,
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="t1", agent_id="a1"),
        capability_gateway=_FakeGateway(),
    )


_PROMPT = SimpleNamespace(system="SYS", tools=[])


# ── 1) 整份路径 + 无 floor：observe 收到 (估算段, usage.prompt_tokens) ────────────


async def test_feedback_uses_estimate_segment_when_not_floored(monkeypatch):
    captured_request = {}

    async def _fake_stream(ctx, state, request):
        captured_request["req"] = request
        yield _make_usage_chunk(prompt_tokens=10)

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    tok = _SpyTokenizer()
    bus = _RecordingBus()
    state = _make_state(context_tokens=0)  # 无真实基线 → 整份路径，且 0 不会 floor
    ctx = _make_ctx(bus, tok)

    await _run_llm_turn(state, ctx, _PROMPT, [], 1)

    req = captured_request["req"]
    full = _estimate_request_tokens(req, tok.count)
    assert req.metadata[PROMPT_EST_SEG_KEY] == full
    assert tok.observe_calls == [(full, 10)]  # base=0 → actual段 = 10-0


# ── 2) 整份路径 + floor：observe 的 est 参数 = 真实估算段（不是被 floor 的值）────────


async def test_feedback_uses_real_segment_not_floored_value(monkeypatch):
    captured_request = {}

    async def _fake_stream(ctx, state, request):
        captured_request["req"] = request
        yield _make_usage_chunk(prompt_tokens=10)

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    tok = _SpyTokenizer()
    bus = _RecordingBus()
    # loop_guard.context_tokens 远大于整份估算(仅 "SYS" 量级) → 触发 floor
    state = _make_state(context_tokens=1_000_000)
    ctx = _make_ctx(bus, tok)

    await _run_llm_turn(state, ctx, _PROMPT, [], 1)

    req = captured_request["req"]
    full = _estimate_request_tokens(req, tok.count)
    assert req.prompt_token_estimate == 1_000_000  # 返回值确被 floor
    assert full != 1_000_000                        # floor 确实生效（真实段远小于 floor 值）
    assert req.metadata[PROMPT_EST_SEG_KEY] == full
    # 回喂用的是真实估算段，不是被 floor 的 1_000_000
    assert tok.observe_calls == [(full, 10)]


# ── 3) 无 usage（prompt_tokens=0）：不回喂 ─────────────────────────────────────


async def test_no_feedback_when_no_usage(monkeypatch):
    async def _fake_stream(ctx, state, request):
        yield _make_usage_chunk(prompt_tokens=0)

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    tok = _SpyTokenizer()
    bus = _RecordingBus()
    state = _make_state(context_tokens=0)
    ctx = _make_ctx(bus, tok)

    await _run_llm_turn(state, ctx, _PROMPT, [], 1)

    assert tok.observe_calls == []
