"""LLMUsage 七字段构造语义：input_tokens 哨兵自动派生 + 显式保留 + asdict 透传。"""
from __future__ import annotations

import dataclasses

from ctx_weft.protocols import LLMUsage


def test_legacy_construction_derives_input_tokens():
    # 旧写法（不传新字段）必须自洽：无缓存信息时实际输入 == 总输入
    u = LLMUsage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    assert u.input_tokens == 100
    assert u.cache_read_tokens == 0 and u.cache_write_tokens == 0
    assert u.reasoning_tokens == 0


def test_derivation_with_cache_split():
    u = LLMUsage(prompt_tokens=127, cache_read_tokens=100, cache_write_tokens=20)
    assert u.input_tokens == 7


def test_explicit_input_tokens_preserved():
    u = LLMUsage(prompt_tokens=127, cache_read_tokens=100, cache_write_tokens=20,
                 input_tokens=7)
    assert u.input_tokens == 7


def test_abnormal_ledger_clamps_derivation_to_zero():
    # provider 异常账（cached > prompt）：派生路径钳 0，不产生负数
    u = LLMUsage(prompt_tokens=5, cache_read_tokens=10)
    assert u.input_tokens == 0


def test_asdict_carries_seven_keys():
    d = dataclasses.asdict(LLMUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13))
    assert set(d) == {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cache_read_tokens", "cache_write_tokens", "input_tokens", "reasoning_tokens",
    }
    assert d["input_tokens"] == 10
