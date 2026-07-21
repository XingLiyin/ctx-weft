"""prepare 的 compact 触发估算：增量记录 / 首份装配估算补齐 tool_calls 参数、图片、tools schema。

优化前 prepare 只 join memory 记录的 content 文本（丢 tool_calls 参数、非 str content），
且首份回退到 composer 的纯文本 token_count（连 tools schema 都没数）。
"""
from types import SimpleNamespace

from ctx_weft.core.loop.steps.prepare import _estimate_assembled_tokens, _estimate_record_tokens
from ctx_weft.protocols import LLMMessage


def _rec(content, **md):
    return SimpleNamespace(content=content, metadata=md or None)


def test_record_counts_tool_calls_in_metadata():
    # LLM_RESPONSE：content 空、体量全在 metadata.tool_calls 里——必须计入
    r = _rec("", tool_calls=[{"name": "write_file", "input": {"content": "x" * 6000}}])
    assert _estimate_record_tokens(r) >= 2000


def test_record_plain_content_with_framing():
    assert _estimate_record_tokens(_rec("hello world")) == 4 + 4  # framing + ceil(11/3)


def test_record_no_metadata_ok():
    assert _estimate_record_tokens(SimpleNamespace(content="hi", metadata=None)) >= 4


def test_record_counts_reasoning():
    with_r = _estimate_record_tokens(_rec("hi", reasoning="R" * 3000))
    without = _estimate_record_tokens(_rec("hi"))
    assert with_r - without >= 900


def test_assembled_counts_tool_calls_and_tools_schema():
    # 首份估算：content 空但 tool_calls 大 + 有 tools schema → 远超纯文本 token_count
    prompt = SimpleNamespace(
        system="sys",
        messages=[LLMMessage(role="assistant", content="",
                             tool_calls=[{"id": "c", "name": "w", "input": {"c": "x" * 6000}}])],
        tools=[SimpleNamespace(name="tool_a", description="does a thing",
                               input_schema={"type": "object", "properties": {"a": {"type": "string"}}})],
        token_count=0,
    )
    assert _estimate_assembled_tokens(prompt, "mock") >= 2000


def test_assembled_tools_schema_counted_even_without_messages():
    # 纯 tools（composer 的 token_count 完全没数 tools）——此处应计入 schema
    prompt = SimpleNamespace(
        system="",
        messages=[],
        tools=[SimpleNamespace(name="t", description="d",
                               input_schema={"type": "object",
                                             "properties": {p: {"type": "string"} for p in "abcdefgh"}})],
        token_count=0,
    )
    assert _estimate_assembled_tokens(prompt, "mock") > 0
