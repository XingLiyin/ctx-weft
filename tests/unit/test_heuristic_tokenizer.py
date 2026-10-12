"""HeuristicTokenizer：启发式费率 × 对数空间伺服校准。

count 返回已校准值；observe 收 (已校准估算段, 真实段)。首样本直接种入
factor=act/est（此时 factor=1，act/est 即原始比值）；此后 factor *= (act/est)^α
（均衡点 = 校准后估算贴住真实值）；恒 clamp [0.5, 3.0]；小样本/非正真实值跳过。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


def test_count_equals_heuristic_when_uncalibrated():
    t = HeuristicTokenizer()
    assert t.count("hello world") == estimate_tokens("hello world")
    assert t.count("你好世界") == estimate_tokens("你好世界")


def test_count_empty_is_zero():
    assert HeuristicTokenizer().count("") == 0


def test_first_sample_seeds_factor_directly():
    t = HeuristicTokenizer()
    t.observe(1000, 2000)
    assert t.factor == pytest.approx(2.0)


def test_count_applies_learned_factor():
    t = HeuristicTokenizer()
    t.observe(1000, 2000)  # factor=2.0
    # "R"*4000 → 启发式(高熵段 0.6/字符) 2400 → ×2
    assert t.count("R" * 4000) == 4800


def test_subsequent_samples_damped_by_alpha():
    t = HeuristicTokenizer(alpha=0.3)
    t.observe(1000, 2000)          # 种入 2.0
    t.observe(1000, 1500)          # factor *= 1.5^0.3
    assert t.factor == pytest.approx(2.0 * 1.5 ** 0.3)


def test_equilibrium_no_drift():
    # 校准后估算 == 真实 → factor 不动（伺服均衡点）
    t = HeuristicTokenizer()
    t.observe(1000, 2000)
    t.observe(1000, 1000)
    assert t.factor == pytest.approx(2.0)


def test_factor_clamped_both_directions():
    hi = HeuristicTokenizer()
    hi.observe(1000, 999_000)
    assert hi.factor == 3.0
    lo = HeuristicTokenizer()
    lo.observe(999_000, 1000)
    assert lo.factor == 0.5


def test_small_or_nonpositive_samples_ignored():
    t = HeuristicTokenizer(min_sample_tokens=512)
    t.observe(100, 10_000)   # est < 512
    t.observe(1000, 0)       # act <= 0
    t.observe(1000, -5)
    assert t.factor == 1.0


def test_nonempty_count_at_least_one():
    t = HeuristicTokenizer()
    t.observe(1000, 500)  # factor=0.5
    assert t.count("a") >= 1
