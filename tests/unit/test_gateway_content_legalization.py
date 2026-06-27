"""Gateway 发送前合法化：空 content 防御 + 悬挂 tool_call 防御。

两条都是发送前最后兜底——reconcile/act 正常已补齐 dangling、composer 正常不产空
content；命中即说明上游漏了，故**只删不补**且打一条 ERROR 日志。
"""

from __future__ import annotations

import logging

import pytest

from ctx_weft.core.loop.llm_gateway import (
    drop_dangling_tool_calls,
    remove_empty_messages,
)
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart

_LOGGER = "ctx_weft.core.loop.llm_gateway"


def _assistant_call(cid: str, *, content: str = "") -> LLMMessage:
    return LLMMessage(
        role="assistant", content=content,
        tool_calls=[{"id": cid, "name": "t", "arguments": {}}],
    )


def _tool_result(cid: str, content: str = "ok") -> LLMMessage:
    return LLMMessage(role="tool", content=content, tool_call_id=cid)


# ── drop_dangling_tool_calls ──────────────────────────────────────────────────


def test_keeps_tool_call_with_matching_result() -> None:
    msgs = [LLMMessage(role="user", content="hi"), _assistant_call("c1"), _tool_result("c1")]
    out = drop_dangling_tool_calls(msgs)
    asst = [m for m in out if m.role == "assistant"][0]
    assert [tc["id"] for tc in asst.tool_calls] == ["c1"]


def test_strips_dangling_tool_call_without_result() -> None:
    msgs = [LLMMessage(role="user", content="hi"), _assistant_call("c1", content="working")]
    out = drop_dangling_tool_calls(msgs)
    asst = [m for m in out if m.role == "assistant"][0]
    assert asst.tool_calls == []          # 悬挂调用被剥离
    assert asst.content == "working"      # 文本保留


def test_strips_only_dangling_keeps_paired() -> None:
    msgs = [
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": "c1", "name": "t", "arguments": {}},
            {"id": "c2", "name": "t", "arguments": {}},
        ]),
        _tool_result("c1"),
    ]
    out = drop_dangling_tool_calls(msgs)
    asst = [m for m in out if m.role == "assistant"][0]
    assert [tc["id"] for tc in asst.tool_calls] == ["c1"]   # c2 悬挂剥离，c1 保留


def test_dangling_drop_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    msgs = [LLMMessage(role="user", content="hi"), _assistant_call("c1")]
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        drop_dangling_tool_calls(msgs)
    assert any(r.levelno == logging.ERROR and "c1" in r.getMessage() for r in caplog.records)


def test_no_dangling_no_log(caplog: pytest.LogCaptureFixture) -> None:
    msgs = [LLMMessage(role="user", content="hi"), _assistant_call("c1"), _tool_result("c1")]
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        drop_dangling_tool_calls(msgs)
    assert not caplog.records


# ── remove_empty_messages ─────────────────────────────────────────────────────


def test_drops_empty_user() -> None:
    msgs = [
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="assistant", content="  "),
        LLMMessage(role="user", content="next"),
    ]
    out = remove_empty_messages(msgs)
    assert [m.role for m in out] == ["user", "user"]


def test_keeps_assistant_with_tool_calls_even_if_empty_content() -> None:
    msgs = [_assistant_call("c1", content="")]    # 纯 tool_use，无文本——合法
    out = remove_empty_messages(msgs)
    assert out == msgs


def test_keeps_multimodal_image_content() -> None:
    img = LLMMessage(role="user", content=[ImagePart(data="x", media_type="image/png")])
    out = remove_empty_messages([img])
    assert out == [img]


def test_drops_empty_textpart_list() -> None:
    msgs = [LLMMessage(role="assistant", content=[TextPart(text="   ")])]
    out = remove_empty_messages(msgs)
    assert out == []


def test_empty_drop_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    msgs = [LLMMessage(role="user", content="")]
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        remove_empty_messages(msgs)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_does_not_touch_empty_tool_message() -> None:
    # tool result 即便空也不在此删（删了会制造悬挂）；只管 user/assistant。
    msgs = [_tool_result("c1", content="")]
    out = remove_empty_messages(msgs)
    assert out == msgs
