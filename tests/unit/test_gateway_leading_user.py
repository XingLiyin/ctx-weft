"""gateway ensure_leading_user：首条必须 user（§2.5）。"""
from __future__ import annotations

from ctx_weft.core.loop.llm_gateway import ensure_leading_user
from ctx_weft.protocols import LLMMessage


def _u(c): return LLMMessage(role="user", content=c)
def _a(c, tcs=None): return LLMMessage(role="assistant", content=c, tool_calls=tcs or [])
def _t(c, tcid): return LLMMessage(role="tool", content=c, tool_call_id=tcid)


def test_drops_leading_assistant():
    out = ensure_leading_user([_a("hi"), _u("q"), _a("a")])
    assert [m.role for m in out] == ["user", "assistant"]
    assert out[0].content == "q"


def test_drops_leading_tool_and_assistant_run():
    out = ensure_leading_user([_a("x", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1"), _u("q")])
    assert [m.role for m in out] == ["user"]


def test_already_leading_user_unchanged():
    msgs = [_u("q"), _a("a"), _t("r", "1")]
    assert ensure_leading_user(msgs) == msgs


def test_empty_unchanged():
    assert ensure_leading_user([]) == []


def test_no_user_at_all_returns_empty():
    out = ensure_leading_user([_a("x"), _t("r", "1")])
    assert out == []


def test_mid_conversation_tool_result_not_touched():
    """正常工具循环：user → assistant(tool_call) → tool。首条已是 user，整体不动。"""
    msgs = [_u("q"), _a("", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1")]
    assert ensure_leading_user(msgs) == msgs


from ctx_weft.core.loop.llm_gateway import drop_orphan_tool_results, merge_consecutive_messages


def test_pipeline_leading_assistant_then_orphan_cleaned():
    # ensure_leading_user 丢前导 assistant → 暴露的 tool 成孤儿 → drop_orphan 清掉
    msgs = [_a("x", [{"id": "1", "name": "f", "input": {}}]), _t("r", "1"), _u("q")]
    out = merge_consecutive_messages(drop_orphan_tool_results(ensure_leading_user(msgs)))
    assert [m.role for m in out] == ["user"]
    assert out[0].content == "q"
