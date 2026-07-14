"""estimate_tokens：CJK/其他分开、刻意往大了估（避免 len//4 对中文系统性低估）。

口径：CJK 每字 ceil(1.5*n) token；其余每 ceil(len/3) token。两段相加，非空至少 1。
"""
from __future__ import annotations

from math import ceil

from ctx_weft.core.utils import estimate_tokens


def test_empty_is_zero():
    assert estimate_tokens("") == 0


def test_english_over_estimated_ceil_len_over_3():
    # 11 ASCII → ceil(11/3) = 4（旧 //4 只有 2）
    assert estimate_tokens("hello world") == 4


def test_cjk_one_and_half_per_char():
    # 4 CJK → ceil(1.5*4) = 6（旧 len//4 = 1）
    assert estimate_tokens("你好世界") == 6


def test_cjk_odd_count_rounds_up():
    # 1 CJK → ceil(1.5) = 2；3 CJK → ceil(4.5) = 5
    assert estimate_tokens("你") == 2
    assert estimate_tokens("你好啊") == 5


def test_mixed_cjk_and_ascii_added():
    # 2 CJK + 3 ASCII → ceil(1.5*2)=3 + ceil(3/3)=1 = 4
    assert estimate_tokens("你好abc") == 4


def test_japanese_kana_and_hangul_are_cjk():
    assert estimate_tokens("あ") == 2   # 平假名
    assert estimate_tokens("ア") == 2   # 片假名
    assert estimate_tokens("한") == 2   # 谚文


def test_cjk_punctuation_and_fullwidth_are_cjk():
    # 全角标点/形式区按 CJK 计（每字 1.5）
    assert estimate_tokens("。") == 2
    assert estimate_tokens("！") == 2


def test_supplementary_cjk_ext_b():
    # 扩展 B（U+20000+，如 𠀀）也算 CJK
    assert estimate_tokens("\U00020000") == 2


def test_always_over_estimates_vs_old_len_div_4():
    # 对任意含 CJK/普通文本，新估算 >= 旧 len//4（刻意偏大）
    for txt in ["hello world", "你好世界", "这是一段中文说明", "mixed 混合 text 文本"]:
        assert estimate_tokens(txt) >= max(1, len(txt) // 4)


def test_matches_documented_formula():
    # 白盒：与 CJK ceil(1.5n) + 其他 ceil(len/3) 一致
    txt = "混合abc123，测试！"
    cjk = sum(1 for c in txt if c in "混合测试，！")
    other = len(txt) - cjk
    assert estimate_tokens(txt) == ceil(1.5 * cjk) + ceil(other / 3)
