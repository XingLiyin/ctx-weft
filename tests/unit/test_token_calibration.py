"""Token 估算运行时自校准：按模型维护 实际/估算 比值的 EMA。

设计：每轮 usage 到达后拿 (估算增量, 真实增量) 喂 EMA；request_prompt_estimate 把
学到的 factor 乘到估算段上（真实基线不乘）——自适配任意 tokenizer 的费率偏差。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.llm_gateway import _estimate_request_tokens, request_prompt_estimate
from ctx_weft.core.loop.token_calibration import (
    PROMPT_EST_BASE_KEY,
    PROMPT_EST_RAW_KEY,
    TokenCalibration,
    calibration_factor,
    observe_estimate,
    observe_request_outcome,
    reset_calibration,
)
from ctx_weft.protocols import LLMMessage, LLMRequest, LLMUsage

# ── TokenCalibration 本体 ──────────────────────────────────────────────────────

def test_factor_defaults_to_one():
    assert TokenCalibration().factor("m") == 1.0


def test_first_sample_seeds_ema_directly():
    c = TokenCalibration()
    c.observe("m", estimated=10_000, actual=15_000)
    assert c.factor("m") == pytest.approx(1.5)


def test_ema_blends_subsequent_samples():
    c = TokenCalibration(alpha=0.5)
    c.observe("m", 10_000, 20_000)  # 首样本直接种 EMA=2.0
    c.observe("m", 10_000, 10_000)  # ratio=1.0 → 0.5*1.0 + 0.5*2.0 = 1.5
    assert c.factor("m") == pytest.approx(1.5)


def test_small_samples_ignored():
    # 估算段太小（< min_sample_tokens）时噪声占主导，不更新
    c = TokenCalibration(min_sample_tokens=512)
    c.observe("m", 100, 10_000)
    assert c.factor("m") == 1.0


def test_nonpositive_actual_ignored():
    c = TokenCalibration()
    c.observe("m", 10_000, 0)
    c.observe("m", 10_000, -5)
    assert c.factor("m") == 1.0


def test_factor_clamped_both_directions():
    c = TokenCalibration()
    c.observe("hi", 1_000, 100_000)   # ratio=100 → 夹到 3.0
    c.observe("lo", 100_000, 1_000)   # ratio=0.01 → 夹到 0.5
    assert c.factor("hi") == 3.0
    assert c.factor("lo") == 0.5


def test_models_independent():
    c = TokenCalibration()
    c.observe("a", 10_000, 20_000)
    assert c.factor("a") == pytest.approx(2.0)
    assert c.factor("b") == 1.0


def test_module_level_default_and_reset():
    reset_calibration()
    observe_estimate("m", 10_000, 20_000)
    assert calibration_factor("m") == pytest.approx(2.0)
    reset_calibration()
    assert calibration_factor("m") == 1.0


# ── request_prompt_estimate 应用 factor（估算段乘、真实基线不乘）────────────────

def _req(**kw):
    base = dict(model="m", system="sys", messages=[LLMMessage(role="user", content="hi")], tools=[])
    base.update(kw)
    return LLMRequest(**base)


def _guard(context_limit=200_000, context_tokens=0):
    return SimpleNamespace(context_limit=context_limit, context_tokens=context_tokens)


def test_estimate_applies_factor_to_incremental_delta_only():
    reset_calibration()
    observe_estimate("m", 10_000, 20_000)  # 学到 2x
    req = _req(messages=[
        LLMMessage(role="user", content="old"),
        LLMMessage(role="assistant", content="prev"),
        LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1"),  # 新增尾段
    ])
    est = request_prompt_estimate(req, _guard(context_tokens=50_000), 2)
    # 真实基线 50_000 不乘；raw delta = framing(4) + ceil(4000/2)=2000 → ×2 = 4008
    assert est == 50_000 + 2 * (4 + 2000)


def test_estimate_applies_factor_to_full_estimate():
    reset_calibration()
    observe_estimate("m", 10_000, 15_000)  # 1.5x
    req = _req(messages=[LLMMessage(role="user", content="X" * 40_000)])
    est = request_prompt_estimate(req, _guard(context_tokens=0), None)
    assert est == int(_estimate_request_tokens(req) * 1.5)


def test_estimate_records_base_and_raw_in_metadata():
    reset_calibration()
    req = _req(messages=[
        LLMMessage(role="user", content="old"),
        LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1"),
    ])
    request_prompt_estimate(req, _guard(context_tokens=50_000), 1)
    assert req.metadata[PROMPT_EST_BASE_KEY] == 50_000
    assert req.metadata[PROMPT_EST_RAW_KEY] == 4 + 2000  # 未乘 factor 的原始增量估算


def test_estimate_unchanged_when_factor_is_one():
    reset_calibration()
    req = _req(messages=[LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1")])
    est = request_prompt_estimate(req, _guard(context_tokens=50_000), 0)
    assert est == 50_000 + 4 + 2000


# ── observe_request_outcome：usage 到达后按 metadata 回喂 EMA ──────────────────

def test_observe_request_outcome_updates_factor():
    reset_calibration()
    req = _req()
    req.metadata[PROMPT_EST_BASE_KEY] = 50_000
    req.metadata[PROMPT_EST_RAW_KEY] = 2_000
    observe_request_outcome(req, LLMUsage(prompt_tokens=54_000))
    # 真实增量 = 54_000 − 50_000 = 4_000；ratio = 4000/2000 = 2.0
    assert calibration_factor("m") == pytest.approx(2.0)


def test_observe_request_outcome_noop_without_metadata():
    reset_calibration()
    observe_request_outcome(_req(), LLMUsage(prompt_tokens=54_000))
    assert calibration_factor("m") == 1.0


def test_observe_request_outcome_noop_without_usage():
    reset_calibration()
    req = _req()
    req.metadata[PROMPT_EST_BASE_KEY] = 50_000
    req.metadata[PROMPT_EST_RAW_KEY] = 2_000
    observe_request_outcome(req, LLMUsage(prompt_tokens=0))
    assert calibration_factor("m") == 1.0


# ── act 循环接线：_run_llm_turn 后 usage 自动回喂 EMA ──────────────────────────
# fakes 与 test_llm_request_identity 同构，自包含避免跨测试文件 import。

from ctx_weft.protocols import MemoryScope


class _RecordingBus:
    def __init__(self):
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _make_state():
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(max_turns_per_act=3, compact_keep_last=2),
        runtime={},
        loop_guard=SimpleNamespace(context_limit=100_000, context_tokens=0),
    )
    session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    task = SimpleNamespace(id="t1", parent_task_id="p1", status="RUNNING")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="a1")
    from ctx_weft.core.loop.driver import LoopState
    return LoopState(
        run_id="run-test", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None, "bound_capabilities": []},
    )


def _make_ctx(event_bus):
    from ctx_weft.core.loop.driver import LoopContext
    from ctx_weft.protocols import ProviderContext
    from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

    class _FakeAssembler:
        async def assemble(self, req):
            return SimpleNamespace(system="SYS", messages=[], tools=[])

    class _FakeLLM:
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
    )


async def test_act_turn_feeds_calibration(monkeypatch):
    import ctx_weft.core.loop.steps.act as _act_mod
    from ctx_weft.core.loop.steps.act import _run_llm_turn

    reset_calibration()

    async def _fake_stream(ctx, state, request):
        # raw 整份估算 = est("SYS")=1 + framing(4)+ceil(4000/2)=2004 → 2005；
        # 真实 prompt 报 2 倍 → ratio ≈ 2.0
        yield SimpleNamespace(
            kind="usage", usage=LLMUsage(prompt_tokens=4010, completion_tokens=5),
            tool_call=None, text="")

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    state = _make_state()
    ctx = _make_ctx(_RecordingBus())
    messages = [LLMMessage(role="user", content="R" * 4000)]

    await _run_llm_turn(state, ctx, SimpleNamespace(system="SYS", tools=[]), messages, 1)

    assert calibration_factor("mock") == pytest.approx(4010 / 2005)


async def test_act_turn_no_feed_without_usage(monkeypatch):
    import ctx_weft.core.loop.steps.act as _act_mod
    from ctx_weft.core.loop.steps.act import _run_llm_turn

    reset_calibration()

    async def _fake_stream(ctx, state, request):
        yield SimpleNamespace(kind="token", text="hi", usage=None, tool_call=None)

    monkeypatch.setattr(_act_mod, "stream_llm_resilient", _fake_stream)
    state = _make_state()
    ctx = _make_ctx(_RecordingBus())

    await _run_llm_turn(
        state, ctx, SimpleNamespace(system="SYS", tools=[]),
        [LLMMessage(role="user", content="R" * 4000)], 1)

    assert calibration_factor("mock") == 1.0


# ── prepare 接线：增量/整份估算应用 factor ─────────────────────────────────────

async def test_prepare_incremental_estimate_applies_factor(fake_state_ctx):
    from ctx_weft.core.loop.steps.prepare import PrepareStep

    state, ctx = fake_state_ctx
    reset_calibration()
    observe_estimate("mock", 10_000, 20_000)  # 学到 2x
    # conftest guard: context_tokens=1000；补 context_message_count=1 →
    # 增量 = 最新 1 条（LLM_RESPONSE "hello llm"：framing 4 + ceil(9/3)=3 = 7）
    state.agent.loop_guard.context_message_count = 1

    est, has_baseline = await PrepareStep()._estimate_tokens(state, ctx)

    assert has_baseline is True
    assert est == 1000 + 2 * 7  # 基线不乘、增量乘 factor


def test_prepare_assembled_estimate_applies_factor():
    from ctx_weft.core.loop.steps.prepare import _estimate_assembled_tokens

    reset_calibration()
    observe_estimate("m2", 10_000, 20_000)
    prompt = SimpleNamespace(
        system="SYS", messages=[LLMMessage(role="user", content="R" * 4000)], tools=[])
    # raw = est("SYS")=1 + framing(4)+ceil(4000/2)=2004 → 2005；×2
    assert _estimate_assembled_tokens(prompt, "m2") == 2 * 2005
