"""dynamic_max_tokens + 请求携带 prompt 估算值：按窗口实时算输出上限。

架构：caller 侧 request_prompt_estimate 算 used（真实基线 + 本轮增量）→ 挂到
request.prompt_token_estimate → 网关 apply_dynamic_max_tokens 纯消费。网关不再自估。
"""
from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.utils import dynamic_max_tokens
from ctx_weft.protocols import LLMMessage, LLMRequest
from ctx_weft.core.loop.llm_gateway import (
    apply_dynamic_max_tokens,
    request_prompt_estimate,
    _estimate_request_tokens,
    _estimate_message_tokens,
)
from ctx_weft.protocols.context import ImagePart, TextPart


# ── 纯算术：dynamic_max_tokens(context_limit, used, ceiling) ────────────────────

def test_empty_window_releases_full_remaining():
    # used 小 + 默认 ceiling=context_limit → ≈ context_limit − margin
    got = dynamic_max_tokens(200_000, 100, ceiling=200_000, margin=4096, floor=1024)
    assert got == 200_000 - 100 - 4096


def test_used_directly_subtracted():
    got = dynamic_max_tokens(128_000, 3000, ceiling=128_000, margin=4096, floor=1024)
    assert got == 128_000 - 3000 - 4096


def test_ceiling_clamps_when_configured_smaller():
    # 配了 output_ceiling(< 剩余) → 被夹到 ceiling（Anthropic 硬输出上限场景）
    got = dynamic_max_tokens(200_000, 100, ceiling=8192, margin=4096, floor=1024)
    assert got == 8192


def test_floor_when_window_nearly_full():
    # 剩余 < floor → 被 floor 兜住
    got = dynamic_max_tokens(200_000, 199_500, ceiling=200_000, margin=4096, floor=1024)
    assert got == 1024


# ── helper: request_prompt_estimate（caller 侧 used 估算）────────────────────────

def _req(**kw):
    base = dict(model="m", system="sys", messages=[LLMMessage(role="user", content="hi")], tools=[])
    base.update(kw)
    return LLMRequest(**base)


def _guard(context_limit=200_000, context_tokens=0):
    return SimpleNamespace(context_limit=context_limit, context_tokens=context_tokens)


def test_estimate_first_call_uses_full_when_larger():
    # baseline=None → max(整份估算, context_tokens)；整份更大时用整份
    req = _req(messages=[LLMMessage(role="user", content="X" * 40_000)])
    est = request_prompt_estimate(req, _guard(context_tokens=0), None)
    assert est == _estimate_request_tokens(req)
    assert est >= 10_000


def test_estimate_first_call_floors_at_context_tokens():
    # baseline=None、上步真实 context_tokens 更大 → 不低于它（更保守，永不 400）
    req = _req(messages=[LLMMessage(role="user", content="hi")])
    est = request_prompt_estimate(req, _guard(context_tokens=90_000), None)
    assert est == 90_000


def test_estimate_incremental_baseline_plus_delta():
    # 循环 2+：真实基线 + 仅新增尾段（本轮 tool result）；基线前的历史不重估
    req = _req(messages=[
        LLMMessage(role="user", content="old" * 10_000),                 # 基线内，不计
        LLMMessage(role="assistant", content="prev"),                    # 基线内，不计
        LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1"),  # 新增，计
    ])
    est = request_prompt_estimate(req, _guard(context_tokens=50_000), 2)
    # 50_000（真实基线）+ 每条消息估算：framing(4) + ceil(4000/3)=1334；历史大 user 不被重估
    assert est == 50_000 + 4 + 1334


def test_estimate_incremental_falls_back_without_real_baseline():
    # context_tokens=0（无真实基线）即使给了 baseline_msg_count 也退回首次分支
    req = _req(messages=[LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1")])
    est = request_prompt_estimate(req, _guard(context_tokens=0), 0)
    assert est == max(_estimate_request_tokens(req), 0)


def test_estimate_includes_tool_result_messages():
    # role="tool" 消息必须计入（整份估算分支）
    req = _req(messages=[
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="tool", content="X" * 4000, tool_call_id="t1"),
    ])
    est = _estimate_request_tokens(req)
    assert est >= 1000


# ── 此前数不到的几类：tool_calls 参数 / reasoning / 图片 / framing ─────────────────

def test_message_counts_tool_call_arguments():
    # 纯工具调用回合：content 空，体量全在 tool_calls 的 arguments 里——必须计入（旧口径 ≈0 → 400 洞）
    big = "x" * 6000
    m = LLMMessage(role="assistant", content="",
                   tool_calls=[{"id": "c1", "name": "write_file", "input": {"content": big}}])
    assert _estimate_message_tokens(m) >= 2000  # ceil(~6000/3) 量级，远超旧的 ~0


def test_message_counts_reasoning_content():
    with_r = _estimate_message_tokens(
        LLMMessage(role="assistant", content="hi", reasoning_content="R" * 3000))
    without = _estimate_message_tokens(LLMMessage(role="assistant", content="hi"))
    assert with_r - without >= 900  # ceil(3000/3)=1000


def test_message_counts_image_parts_by_fixed_constant():
    # 图片按固定保守常数（~1600），不是 base64 的 10 万字符（否则反向严重高估）
    m = LLMMessage(role="user", content=[
        TextPart(text="见图"),
        ImagePart(data="A" * 100_000, media_type="image/png"),
    ])
    assert 1500 <= _estimate_message_tokens(m) <= 2200


def test_message_framing_overhead_added():
    # 空 content 消息也有固定 framing 开销（provider 每条消息都要计）
    assert _estimate_message_tokens(LLMMessage(role="user", content="")) >= 4


def test_tool_call_args_counted_in_request_estimate():
    # 整份估算里也要含 tool_calls 参数
    req = _req(messages=[
        LLMMessage(role="assistant", content="",
                   tool_calls=[{"id": "c1", "name": "w", "input": {"c": "x" * 9000}}]),
    ])
    assert _estimate_request_tokens(req) >= 3000  # ceil(~9000/3)


# ── 网关消费者：apply_dynamic_max_tokens 读 request.prompt_token_estimate ─────────

def _ctx(llm, config=None):
    return SimpleNamespace(llm=llm, config=config)


def _llm(context_limit=200_000, output_ceiling=None):
    return SimpleNamespace(context_limit=context_limit, output_ceiling=output_ceiling)


def test_apply_uses_request_estimate():
    req = _req(prompt_token_estimate=100)
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard(context_tokens=0))
    assert req.max_tokens == 200_000 - 100 - 8192  # 默认 margin


def test_apply_noop_when_estimate_missing():
    # caller 未挂估算 → 网关不设 max_tokens（feature off，无旧回退自估）
    req = _req()  # prompt_token_estimate=None
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard(context_tokens=0))
    assert req.max_tokens is None


def test_apply_respects_explicit_max_tokens():
    req = _req(max_tokens=16, prompt_token_estimate=100)
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard())
    assert req.max_tokens == 16  # 不覆盖显式值


def test_apply_noop_when_loop_guard_none():
    req = _req(prompt_token_estimate=100)
    apply_dynamic_max_tokens(_ctx(_llm()), req, None)
    assert req.max_tokens is None


def test_apply_ceiling_fallback_to_context_limit():
    # output_ceiling 缺省(None) → 回退 context_limit，不被夹到小值
    req = _req(prompt_token_estimate=100)
    apply_dynamic_max_tokens(
        _ctx(_llm(context_limit=50_000, output_ceiling=None)), req, _guard(context_limit=50_000))
    assert req.max_tokens == 50_000 - 100 - 8192


def test_apply_ceiling_clamps_when_configured():
    req = _req(prompt_token_estimate=100)
    apply_dynamic_max_tokens(_ctx(_llm(output_ceiling=8192)), req, _guard(context_tokens=0))
    assert req.max_tokens == 8192


# ── 瞬态字段不进线上 payload ─────────────────────────────────────────────────────
from ctx_weft.providers.llm.openai import OpenAIAdapter


def test_prompt_token_estimate_not_in_payload():
    adapter = OpenAIAdapter(api_key="k", model="m")
    req = _req(prompt_token_estimate=12345, max_tokens=100)
    payload = adapter._build_payload(req)
    assert "prompt_token_estimate" not in payload
    assert payload["max_tokens"] == 100


# ── output_ceiling 配置透传（不变）───────────────────────────────────────────────
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
