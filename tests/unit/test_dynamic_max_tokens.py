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
