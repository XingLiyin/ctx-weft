"""dynamic_max_tokens：按窗口实时算请求输出上限（纯算术）。"""
from __future__ import annotations

from ctx_weft.core.utils import dynamic_max_tokens


def test_empty_window_releases_full_remaining():
    # 窗口几乎空 + 默认 ceiling=context_limit → ≈ context_limit − margin
    got = dynamic_max_tokens(200_000, 0, 100, ceiling=200_000, margin=4096, floor=1024)
    assert got == 200_000 - 100 - 4096


def test_used_takes_max_of_real_and_estimate():
    # context_tokens(真实,上轮) 与 prompt_estimate(本次含 tool result) 取大
    # 本次估算更大（本轮新加大 tool result）→ 用它算剩余
    got = dynamic_max_tokens(200_000, 50_000, 120_000, ceiling=200_000, margin=4096, floor=1024)
    assert got == 200_000 - 120_000 - 4096
    # 反向：上轮真实更大 → 用真实
    got2 = dynamic_max_tokens(200_000, 120_000, 50_000, ceiling=200_000, margin=4096, floor=1024)
    assert got2 == 200_000 - 120_000 - 4096


def test_ceiling_clamps_when_configured_smaller():
    # 配了 output_ceiling(< 剩余) → 被夹到 ceiling（Anthropic 硬输出上限场景）
    got = dynamic_max_tokens(200_000, 0, 100, ceiling=8192, margin=4096, floor=1024)
    assert got == 8192


def test_floor_when_window_nearly_full():
    # 剩余 < floor → 被 floor 兜住
    got = dynamic_max_tokens(200_000, 199_000, 199_500, ceiling=200_000, margin=4096, floor=1024)
    assert got == 1024


def test_first_turn_context_tokens_zero():
    got = dynamic_max_tokens(128_000, 0, 3000, ceiling=128_000, margin=4096, floor=1024)
    assert got == 128_000 - 3000 - 4096


# ── 网关 helper ────────────────────────────────────────────────────────────────
from types import SimpleNamespace

from ctx_weft.protocols import LLMMessage, LLMRequest
from ctx_weft.core.loop.llm_gateway import (
    apply_dynamic_max_tokens,
    _estimate_request_tokens,
)


def _req(**kw):
    base = dict(model="m", system="sys", messages=[LLMMessage(role="user", content="hi")], tools=[])
    base.update(kw)
    return LLMRequest(**base)


def _ctx(llm, config=None):
    return SimpleNamespace(llm=llm, config=config)


def _llm(context_limit=200_000, output_ceiling=None):
    return SimpleNamespace(context_limit=context_limit, output_ceiling=output_ceiling)


def _guard(context_limit=200_000, context_tokens=0):
    return SimpleNamespace(context_limit=context_limit, context_tokens=context_tokens)


def test_estimate_includes_tool_result_messages():
    # role="tool" 消息必须计入（context_tokens 漏掉的本轮增量靠它抓）
    req = _req(messages=[
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="tool", content="X" * 4000, tool_call_id="t1"),
    ])
    est = _estimate_request_tokens(req)
    assert est >= 1000  # 4000 字符 tool result ≈ 1000 token，被计入


def test_apply_sets_max_tokens_when_none():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard(context_tokens=0))
    # 空窗口 → 剩余窗口全给输出，接近 context_limit
    assert req.max_tokens is not None and req.max_tokens > 100_000


def test_apply_respects_explicit_max_tokens():
    req = _req(max_tokens=16)
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard())
    assert req.max_tokens == 16  # 不覆盖


def test_apply_noop_when_loop_guard_none():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm()), req, None)
    assert req.max_tokens is None


def test_apply_ceiling_fallback_to_context_limit():
    # output_ceiling 缺省(None) → 回退 context_limit，不被夹到小值
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm(context_limit=50_000, output_ceiling=None)), req, _guard(context_limit=50_000))
    assert req.max_tokens > 40_000


def test_apply_ceiling_clamps_when_configured():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm(output_ceiling=8192)), req, _guard(context_tokens=0))
    assert req.max_tokens == 8192


# ── output_ceiling 配置透传 ─────────────────────────────────────────────────────
from ctx_weft.providers.llm.provider import ModelConfig, _FixedModelClient
from ctx_weft.providers.llm.mock import MockLLMAdapter


def test_fixed_model_client_exposes_output_ceiling():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "m", context_limit=200_000, max_output_tokens=8192, output_ceiling=64_000)
    assert client.output_ceiling == 64_000


def test_fixed_model_client_output_ceiling_defaults_none():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "m", context_limit=200_000, max_output_tokens=8192)
    assert client.output_ceiling is None


def test_model_config_has_output_ceiling_field():
    cfg = ModelConfig(name="m", context_limit=200_000, output_ceiling=32_000)
    assert cfg.output_ceiling == 32_000
    assert ModelConfig(name="m2", context_limit=100_000).output_ceiling is None
