"""M3（评审 2026-08-23 fix wave）：composer._is_blank_content 与 llm_gateway._is_empty_content
的空白语义并存但不同义——前者不 strip，后者 strip。今日安全仅因为 legalize_messages 链路
最终会再经 _is_empty_content 把纯空白消息滤掉一道；这是个没人写下来的非局部不变式。

在此改动前，目前没有任何测试覆盖纯空白内容（"   "）——这条测试钉住这对语义分歧，防止
未来有人「复用兄弟函数」把两者合并时静默改变行为。
"""

from ctx_weft.core.assembler.composer import _is_blank_content
from ctx_weft.core.loop.llm_gateway import _is_empty_content


def test_composer_keeps_whitespace_only_string_as_non_blank():
    assert _is_blank_content("   ") is False


def test_gateway_treats_whitespace_only_string_as_empty():
    assert _is_empty_content("   ") is True
