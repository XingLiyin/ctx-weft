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
