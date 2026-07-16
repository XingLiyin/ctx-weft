"""OpenAI adapter streaming finalize behavior (fake SSE)."""

import json

import pytest

from ctx_weft.protocols import LLMCallError, LLMMessage, LLMRequest, LLMTool
from ctx_weft.providers.llm.openai import OpenAIAdapter, _serialize_messages


# ── Fake httpx streaming client ────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b"err"


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines, status=200):
        self._lines = lines
        self._status = status

    def stream(self, *a, **k):
        return _FakeStreamCtx(_FakeResp(self._lines, self._status))


def _data(obj) -> str:
    return "data: " + json.dumps(obj)


def _delta(delta, finish_reason=None):
    return _data({"choices": [{"delta": delta, "finish_reason": finish_reason}]})


def _adapter(lines):
    a = OpenAIAdapter(api_key="k")
    a._client = _FakeClient(lines)
    return a


async def _collect(adapter):
    req = LLMRequest(model="m", system="", messages=[LLMMessage(role="user", content="hi")])
    return [c async for c in adapter.complete(req, stream=True)]


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_serialize_malformed_raw_arguments_stays_valid_json():
    # 复现 OpenAI API 400 "Expecting ',' delimiter"：真畸形 native 参数被兜底成
    # {"_raw": "<非法JSON>"} 回灌历史后，序列化出的 arguments 若原样回吐畸形串，
    # 严格 OpenAI 兼容端会对其 json.loads(arguments) → JSONDecodeError → 400。
    # 不变式：发出去的 arguments 必须永远是合法 JSON，服务端二次解析不得抛错。
    msg = LLMMessage(
        role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "write_file",
                     "arguments": {"_raw": '{"path": "/a" "content": "x"}'}}],  # 缺逗号
    )
    out = _serialize_messages("", [msg])
    arguments = out[0]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(arguments) == {}  # 服务端会 json.loads(arguments)；畸形 → 发合法空对象


async def test_native_tool_call_normal():
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "t1",
                                "function": {"name": "read", "arguments": "{\"p\": \"/a\"}"}}]}),
        _delta({}, finish_reason="tool_calls"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "read"
    assert tcs[0].tool_call.arguments == {"p": "/a"}


async def test_usage_after_finish_reason_is_captured():
    # OpenAI sends usage in a trailing choices=[] chunk AFTER finish_reason.
    lines = [
        _delta({"content": "hello"}, finish_reason="stop"),
        _data({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                        "total_tokens": 13}}),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    usage = [c for c in chunks if c.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].usage.prompt_tokens == 10


async def test_truncated_tool_call_raises_retriable():
    # tool_calls delta started, then stream ends with no finish_reason / no [DONE].
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "t1",
                                "function": {"name": "read", "arguments": "{\"p\":"}}]}),
    ]
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter(lines))
    assert exc.value.retriable is True


async def test_tool_call_streaming_emits_partial_heartbeat():
    # 工具调用参数流式累积期间发 tool_call_partial 心跳，让 act 流式循环顶部的
    # 暂停/取消检查点有机会触发；最终完整 tool_call 仍在收尾一次性产出。
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "t1",
                                "function": {"name": "read", "arguments": "{\"p\":"}}]}),
        _delta({"tool_calls": [{"index": 0, "function": {"arguments": " \"/a\"}"}}]}),
        _delta({}, finish_reason="tool_calls"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    assert any(c.kind == "tool_call_partial" for c in chunks)
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.arguments == {"p": "/a"}


async def test_text_embedded_tool_call_recovered():
    payload = '<tool_call>{"name": "write", "arguments": {"p": "/a"}}</tool_call>'
    lines = [
        _delta({"content": "ok" + payload}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    tcs = [c for c in chunks if c.kind == "tool_call"]
    assert len(tcs) == 1
    assert tcs[0].tool_call.name == "write"


async def test_malformed_tool_call_delta_is_filtered():
    # null id + empty name → dropped; terminal seen so no raise; no tool_call emitted.
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": None,
                                "function": {"name": "", "arguments": ""}}]}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    assert [c for c in chunks if c.kind == "tool_call"] == []
    assert any(c.kind == "done" for c in chunks)


async def test_inline_think_emits_reasoning():
    lines = [
        _delta({"content": "<think>reasoning</think>answer"}),
        _delta({}, finish_reason="stop"),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    reasoning = [c for c in chunks if c.kind == "reasoning"]
    assert any("reasoning" in c.text for c in reasoning)


async def test_empty_stream_raises_retriable():
    with pytest.raises(LLMCallError) as exc:
        await _collect(_adapter([]))
    assert exc.value.retriable is True


def test_payload_omits_tool_choice_auto_by_default():
    a = OpenAIAdapter(api_key="k")
    req = LLMRequest(
        model="m", system="", messages=[LLMMessage(role="user", content="hi")],
        tools=[LLMTool(name="read", description="", input_schema={"type": "object"})],
    )
    payload = a._build_payload(req)
    assert "tools" in payload
    assert "tool_choice" not in payload


def test_serialize_does_not_echo_raw_sentinel_back_to_model():
    # 回灌历史里的 assistant tool_call 若带兜底 {"_raw": <原文>}（真畸形 JSON），序列化时既
    # 不能把 `_raw` 当参数名喂回去（模型会照抄、死循环），也不能逐字回吐畸形串（严格服务端
    # json.loads(arguments) → 400）。→ 发合法空对象 "{}"；畸形原文另由 gateway error 文案带给模型。
    msg = LLMMessage(
        role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "control__ask_user",
                     "arguments": {"_raw": "{broken json"}}],
    )
    out = _serialize_messages("", [msg])
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "_raw" not in out[0]["tool_calls"][0]["function"]["arguments"]


def test_serialize_valid_raw_still_echoed():
    # 罕见但合法的 _raw（流式拼接抖动救回、finalize 未解包的非 dict 合法 JSON）→ 照发，
    # 因为它本就是合法 JSON，服务端二次解析不会 400。
    msg = LLMMessage(
        role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "read", "arguments": {"_raw": '{"path": "/a"}'}}],
    )
    out = _serialize_messages("", [msg])
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "/a"}


def test_serialize_normal_arguments_still_json_dumped():
    msg = LLMMessage(
        role="assistant", content="",
        tool_calls=[{"id": "c1", "name": "read", "arguments": {"path": "/a"}}],
    )
    out = _serialize_messages("", [msg])
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"path": "/a"}
