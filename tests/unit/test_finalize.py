"""Shared stream-finalization logic for LLM adapters."""

import pytest

from ctx_weft.protocols import LLMCallError, LLMUsage, ToolCall
from ctx_weft.providers.llm._finalize import build_finalize_chunks


def _kinds(chunks):
    return [c.kind for c in chunks]


def test_native_tool_calls_emit_usage_then_calls_then_done():
    tc = ToolCall(id="t1", name="read", arguments={"p": "/a"})
    chunks = build_finalize_chunks(
        content_text="",
        native_tool_calls=[tc],
        had_native_buffer=True,
        saw_terminal=True,
        usage=LLMUsage(prompt_tokens=5, completion_tokens=2),
        finish_reason="tool_use",
    )
    assert _kinds(chunks) == ["usage", "tool_call", "done"]
    assert chunks[1].tool_call is tc


def test_truncated_tool_call_raises_retriable():
    with pytest.raises(LLMCallError) as exc:
        build_finalize_chunks(
            content_text="",
            native_tool_calls=[],
            had_native_buffer=True,      # buffer started but never finalized
            saw_terminal=False,          # no finish_reason / message_delta
            usage=None,
            finish_reason=None,
        )
    assert exc.value.retriable is True


def test_empty_stream_without_terminal_raises_retriable():
    with pytest.raises(LLMCallError) as exc:
        build_finalize_chunks(
            content_text="   ",
            native_tool_calls=[],
            had_native_buffer=False,
            saw_terminal=False,
            usage=None,
            finish_reason=None,
        )
    assert exc.value.retriable is True


def test_plain_text_with_terminal_does_not_raise():
    # Legit plain-text reply (interactive → pause). Must NOT raise.
    # Nothing was streamed (emitted_visible_len=0) → finalize flushes the visible tail.
    chunks = build_finalize_chunks(
        content_text="here is my answer",
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
    )
    assert _kinds(chunks) == ["token", "done"]
    assert chunks[0].text == "here is my answer"


def test_already_streamed_text_is_not_re_emitted():
    # Adapter streamed the whole visible text already → no tail token.
    chunks = build_finalize_chunks(
        content_text="here is my answer",
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
        emitted_visible_len=len("here is my answer"),
    )
    assert _kinds(chunks) == ["done"]


def test_text_with_no_terminal_but_content_is_kept_not_raised():
    # Non-compliant provider that ends a plain-text reply without finish_reason.
    chunks = build_finalize_chunks(
        content_text="some answer",
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=False,
        usage=None,
        finish_reason=None,
        emitted_visible_len=len("some answer"),
    )
    assert _kinds(chunks) == ["done"]


def test_text_embedded_tool_call_is_recovered():
    text = 'ok<tool_call>{"name": "write", "arguments": {"p": "/a"}}</tool_call>'
    chunks = build_finalize_chunks(
        content_text=text,
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
        emitted_visible_len=len("ok"),  # "ok" already streamed
    )
    assert _kinds(chunks) == ["tool_call", "done"]
    assert chunks[0].tool_call.name == "write"
    assert chunks[0].tool_call.arguments == {"p": "/a"}
    assert chunks[0].tool_call.id  # non-empty generated id


def test_minimax_text_tool_call_is_recovered():
    # MiniMax 把工具调用写成 <minimax:tool_call> 文本 → 收尾还原成 tool_call，
    # 且可见正文尾部（"好的\n"）补吐时不带那坨 XML。
    text = (
        "好的\n<minimax:tool_call>"
        '<invoke name="delegate">'
        '<parameter name="title">迁移</parameter>'
        '<parameter name="task_prompt">line1\nline2</parameter>'
        "</invoke></minimax:tool_call>"
    )
    chunks = build_finalize_chunks(
        content_text=text,
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
        emitted_visible_len=0,   # nothing streamed yet → tail flushed here
    )
    assert _kinds(chunks) == ["token", "tool_call", "done"]
    assert chunks[0].text == "好的\n"          # 可见尾部补吐
    assert "<minimax" not in chunks[0].text     # XML 未泄露进正文
    tc = chunks[1].tool_call
    assert tc.name == "delegate"
    assert tc.arguments == {"title": "迁移", "task_prompt": "line1\nline2"}
    assert tc.id                                # 生成了非空 id
def test_truncated_text_tool_call_raises_retriable():
    # Text-mode tool call cut off mid-tag (has_open_tag): no native buffer, so D1
    # doesn't fire and content is non-empty so N4 doesn't fire. Must NOT be dropped
    # to plain text (which would strand the UI in llm_pending) — retry instead.
    text = 'ok<tool_call>{"name": "write", "argum'
    with pytest.raises(LLMCallError) as exc:
        build_finalize_chunks(
            content_text=text,
            native_tool_calls=[],
            had_native_buffer=False,
            saw_terminal=True,
            usage=None,
            finish_reason="stop",
            emitted_visible_len=len("ok"),
        )
    assert exc.value.retriable is True
    assert exc.value.outage is True  # routed through self-heal backoff, not task-level retry


def test_malformed_text_tool_call_raises_retriable():
    # Tool-call tag present and closed but unparseable (bad JSON, no name/XML fallback)
    # → parse yields zero calls. Same trap: must retry, not fall through to plain text.
    text = "<tool_call>not json and not xml</tool_call>"
    with pytest.raises(LLMCallError) as exc:
        build_finalize_chunks(
            content_text=text,
            native_tool_calls=[],
            had_native_buffer=False,
            saw_terminal=True,
            usage=None,
            finish_reason="stop",
        )
    assert exc.value.retriable is True
    assert exc.value.outage is True  # routed through self-heal backoff, not task-level retry


def test_inline_think_extracted_as_reasoning():
    chunks = build_finalize_chunks(
        content_text="<think>thinking</think>answer",
        native_tool_calls=[],
        had_native_buffer=False,
        saw_terminal=True,
        usage=None,
        finish_reason="stop",
        emitted_visible_len=len("answer"),  # "answer" already streamed via gate
    )
    assert _kinds(chunks) == ["reasoning", "done"]
    assert chunks[0].text == "thinking"


def test_native_tool_calls_take_precedence_over_text():
    tc = ToolCall(id="t1", name="native", arguments={})
    text = '<tool_call>{"name": "text_one", "arguments": {}}</tool_call>'
    chunks = build_finalize_chunks(
        content_text=text,
        native_tool_calls=[tc],
        had_native_buffer=True,
        saw_terminal=True,
        usage=None,
        finish_reason="tool_use",
    )
    names = [c.tool_call.name for c in chunks if c.kind == "tool_call"]
    assert names == ["native"]
