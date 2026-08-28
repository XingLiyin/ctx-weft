"""Phase 3c Task B：OpenAI tool-result 图片重定位 + gateway 视觉门控。

改前 `openai.py` 的 ``role == "tool"`` 分支走 ``_parts_to_text``，图片被 ``continue``
**静默丢弃且无占位符**（Anthropic 侧 tool_result 原生支持图片块，是 OpenAI 单边缺口）。
本任务拆两处：

- **策略在 gateway**：``supports_vision`` 是 duck-typed、不在 ``LLMClient`` 协议上，
  ``OpenAIAdapter`` 自己不声明它（声明的是包在外面的 ``_FixedModelClient``），故
  adapter 内 ``getattr(self, "supports_vision", False)`` 恒为 False——门控写在 adapter
  会把**所有**工具图降级掉，包括视觉模型。门控落 ``stream_llm``，与 Phase 3b 的
  rehydrate 同一处、同一理由，且一处覆盖两家 adapter。
- **格式在 adapter**：批处理连续 tool 消息，图片攒到**整段之后**合并成一条 user 消息。
  「段末 flush」是硬约束而非风格选择：OpenAI 要求 assistant 的每个 ``tool_calls`` 由
  紧随其后的 ``tool`` 消息应答，中间插 user 消息会打断配对 → 400。

base64 的验证一律**解码回原始字节**，不用「不以 blob: 开头」这种弱判据——
Phase 3b 实测 ``base64.b64decode('blob:...')`` 默认不抛、静默解出垃圾字节。
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from ctx_weft.core.loop.llm_gateway import stream_llm
from ctx_weft.protocols import (
    BLOB_REF_PREFIX,
    MemoryBlobStore,
    ImagePart,
    LLMChunk,
    LLMMessage,
    LLMRequest,
    ProviderContext,
    TextPart,
)
from ctx_weft.providers.llm.anthropic import _serialize_messages as anth
from ctx_weft.providers.llm.openai import _serialize_messages as oai

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"tool-shot" * 7
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()
_PNG_REF = f"{BLOB_REF_PREFIX}{hashlib.sha256(_PNG_BYTES).hexdigest()}"

_JPG_BYTES = b"\xff\xd8\xff" + b"second" * 9
_JPG_B64 = base64.b64encode(_JPG_BYTES).decode()


def _img(b64: str = _PNG_B64, media_type: str = "image/png") -> ImagePart:
    return ImagePart(data=b64, media_type=media_type)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="ses-B", tenant_id="default")


def _data_url_payload(block: dict) -> bytes:
    """从 image_url block 取出 base64 并**解码回字节**（强判据，见模块 docstring）。"""
    url = block["image_url"]["url"]
    head, _, b64 = url.partition(";base64,")
    assert head.startswith("data:"), f"非 data URL: {url[:40]!r}"
    assert b64, "data URL 缺 base64 段"
    return base64.b64decode(b64, validate=True)


def _parts(content) -> list:
    """断言 content 是 parts 列表再返回。

    **先断言 list**：``not hasattr(p, "text")`` 在 ``str`` 上恒为 True，
    少了这道守卫的「含图」断言是重言式。
    """
    assert isinstance(content, list), f"expected parts list, got {type(content).__name__}"
    return content


def _images(content) -> list:
    return [p for p in _parts(content) if not hasattr(p, "text")]


# ── 1. tool 结果含图 → 文本 + 其后恰有一条 user 消息载图 ──────────────────────


def test_tool_image_relocated_to_one_following_user_message() -> None:
    out = oai("", [LLMMessage(
        role="tool", content=[TextPart(text="截图如下"), _img()], tool_call_id="tc1")])

    assert len(out) == 2, f"应为 tool 文本 + 一条载图 user 消息，实得 {out}"
    tool_msg, user_msg = out
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "tc1"
    assert isinstance(tool_msg["content"], str)
    assert "截图如下" in tool_msg["content"]
    assert _PNG_B64 not in tool_msg["content"], "不得把 base64 拍进 tool 文本"
    assert "ImagePart" not in tool_msg["content"], "不得把 dataclass repr 拍进 tool 文本"

    assert user_msg["role"] == "user"
    blocks = user_msg["content"]
    assert isinstance(blocks, list) and len(blocks) == 1
    assert blocks[0]["type"] == "image_url"
    assert _data_url_payload(blocks[0]) == _PNG_BYTES, "wire 上的 base64 必须解回原始字节"


def test_tool_text_carries_deterministic_marker() -> None:
    """含图时 tool 文本尾部追加**逐字节确定性**标记（无 sha / 随机 id / 时间戳 / 计数器）。"""
    msgs = [LLMMessage(role="tool", content=[TextPart(text="截图如下"), _img()],
                       tool_call_id="tc1")]
    first = oai("", list(msgs))[0]["content"]
    second = oai("", list(msgs))[0]["content"]
    assert first == second, "标记必须逐字节确定，否则砸掉自动前缀缓存"
    assert first != "截图如下", "含图的 tool 文本必须带标记，指向后一条消息"
    assert not any(ch.isdigit() for ch in first.replace("截图如下", "")), \
        f"标记不得含数字（sha / 计数器 / 时间戳的征兆）：{first!r}"
    # Phase 4 Task 1（L6 收口）：文案统一成英文。逐字钉住 wire 输出——这条标记进的是
    # 缓存前缀的中段，改一个字都要在这里显式过一遍。清单见 core/media/refs.py。
    assert first == "截图如下\n\n[image see the following message]", \
        f"标记文案变了（会改变 wire 输出、砸掉既有前缀缓存）：{first!r}"


def test_tool_message_with_only_image_has_non_empty_content() -> None:
    """纯图片 tool 结果不得产出空 content——空 content 会被 provider 拒（同 Anthropic 侧
    ``_EMPTY_TOOL_RESULT_CONTENT`` 的理由）。"""
    out = oai("", [LLMMessage(role="tool", content=[_img()], tool_call_id="tc1")])
    assert out[0]["content"].strip(), f"纯图 tool 结果的 content 不得为空：{out[0]!r}"
    assert len(out) == 2 and out[1]["role"] == "user"


# ── 2. 段末 flush（硬约束的守卫）─────────────────────────────────────────────


def test_consecutive_tool_messages_flush_once_after_whole_segment() -> None:
    """多条连续 tool 消息各含图 → **只产出一条**合并的 user 消息，且位置在**整段之后**。

    这是「段末 flush」硬约束的守卫：OpenAI 要求 assistant 的每个 ``tool_calls`` 由
    紧随其后的 ``tool`` 消息应答，在 tool 消息之间插 user 消息会打断配对 → 400。
    """
    out = oai("", [
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": "tc1", "name": "shot", "arguments": {}},
            {"id": "tc2", "name": "shot", "arguments": {}},
        ]),
        LLMMessage(role="tool", content=[TextPart(text="a"), _img()], tool_call_id="tc1"),
        LLMMessage(role="tool", content=[TextPart(text="b"),
                                         _img(_JPG_B64, "image/jpeg")], tool_call_id="tc2"),
        LLMMessage(role="user", content="继续"),
    ])

    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "tool", "user", "user"], \
        f"两条 tool 之间不得插入任何消息，实得 {roles}"

    relocated = out[3]
    assert relocated["role"] == "user"
    blocks = relocated["content"]
    assert isinstance(blocks, list), "载图 user 消息的 content 必须是 block 列表"
    assert len(blocks) == 2, f"整段的图必须合并进**一条**消息，实得 {len(blocks)} 块"
    assert [_data_url_payload(b) for b in blocks] == [_PNG_BYTES, _JPG_BYTES], \
        "两张图须按 tool 消息顺序合并，且能解回各自原始字节"

    # 原样的尾随 user 消息不得被吞掉、也不得被合并进载图消息。
    assert out[4] == {"role": "user", "content": "继续"}


def test_two_tool_segments_flush_independently() -> None:
    """被 assistant 隔开的两段 tool 各自在**自己**段末 flush，图片不得跨段合并。"""
    out = oai("", [
        LLMMessage(role="assistant", content="", tool_calls=[{"id": "t1", "name": "s",
                                                              "arguments": {}}]),
        LLMMessage(role="tool", content=[_img()], tool_call_id="t1"),
        LLMMessage(role="assistant", content="", tool_calls=[{"id": "t2", "name": "s",
                                                              "arguments": {}}]),
        LLMMessage(role="tool", content=[_img(_JPG_B64, "image/jpeg")], tool_call_id="t2"),
    ])
    roles = [m["role"] for m in out]
    assert roles == ["assistant", "tool", "user", "assistant", "tool", "user"], \
        f"每段各自 flush，实得 {roles}"
    assert _data_url_payload(out[2]["content"][0]) == _PNG_BYTES
    assert _data_url_payload(out[5]["content"][0]) == _JPG_BYTES


def test_text_only_tool_segment_emits_no_extra_user_message() -> None:
    """纯文本 tool 段**不得**凭空多出一条 user 消息。"""
    out = oai("", [
        LLMMessage(role="tool", content=[TextPart(text="a")], tool_call_id="t1"),
        LLMMessage(role="tool", content="b", tool_call_id="t2"),
    ])
    assert [m["role"] for m in out] == ["tool", "tool"]


# ── 3. 纯文本 wire 逐字节不变 ────────────────────────────────────────────────


def test_plain_text_wire_unchanged() -> None:
    """本任务最重要的兼容性约束：纯文本 tool 结果的 wire 逐字节不变（基准值直接写死）。"""
    assert oai("sys", [
        LLMMessage(role="user", content="hello"),
        LLMMessage(role="assistant", content="hi", tool_calls=[]),
        LLMMessage(role="assistant", content="call", tool_calls=[
            {"id": "tc1", "name": "noop", "arguments": {"a": 1}}]),
        LLMMessage(role="tool", content="result", tool_call_id="tc1"),
        LLMMessage(role="tool", content=[TextPart(text="p1"), TextPart(text="p2")],
                   tool_call_id="tc2"),
        LLMMessage(role="tool", content="", tool_call_id=""),
    ]) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "assistant", "content": "call", "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "noop", "arguments": '{"a": 1}'}}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
        {"role": "tool", "tool_call_id": "tc2", "content": "p1 p2"},
        {"role": "tool", "tool_call_id": "", "content": ""},
    ]


# ── 4. Anthropic 侧 tool_result 行为不变（回归）───────────────────────────────


def test_anthropic_tool_result_image_behaviour_unchanged() -> None:
    """Anthropic 原生支持 tool_result 图片块——本任务不得把 OpenAI 的重定位波及过去。"""
    out = anth([
        LLMMessage(role="tool", content=[TextPart(text="r"), _img()], tool_call_id="tc1"),
        LLMMessage(role="tool", content=[_img(_JPG_B64, "image/jpeg")], tool_call_id="tc2"),
    ])
    assert len(out) == 1 and out[0]["role"] == "user", "Anthropic 侧仍是单条 user 载 tool_result"
    results = out[0]["content"]
    assert [r["tool_use_id"] for r in results] == ["tc1", "tc2"]
    assert results[0]["content"] == [
        {"type": "text", "text": "r"},
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": _PNG_B64}},
    ]
    assert results[1]["content"][0]["source"]["media_type"] == "image/jpeg"


# ── 5. gateway 视觉门控 ──────────────────────────────────────────────────────


class _CapturingLLM:
    """记录 complete() 实际收到的 messages——即 adapter 将要序列化的东西。"""

    def __init__(self, *, supports_vision: bool | None = None) -> None:
        self.seen: list[LLMMessage] = []
        if supports_vision is not None:
            self.supports_vision = supports_vision

    async def complete(self, request: LLMRequest, stream: bool = True):
        self.seen = list(request.messages)
        yield LLMChunk(kind="done")


class _CountingStore(MemoryBlobStore):
    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        return f"{BLOB_REF_PREFIX}{hashlib.sha256(data).hexdigest()}"

    async def get(self, ref: str, ctx: ProviderContext) -> "tuple[bytes, str] | None":
        self.get_calls.append(ref)
        return (_PNG_BYTES, "image/png") if ref == _PNG_REF else None


def _request(*messages: LLMMessage) -> LLMRequest:
    return LLMRequest(model="m", system="s", messages=list(messages))


def _legal(*tool_msgs: LLMMessage) -> list[LLMMessage]:
    """把 tool 消息包成 ``legalize_messages`` 不会删改的合法序列。

    ``stream_llm`` 首先跑 ``legalize_messages``：孤儿 tool result（前面没有配对的
    assistant tool_calls）会被 ``drop_orphan_tool_results`` 丢掉，首条非 user 会被
    ``ensure_leading_user`` 砍掉。故门控的测试必须给出 user → assistant(tool_calls)
    → tool… 的完整回合，否则测的是「消息被 legalize 丢了」而不是门控。
    """
    return [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content="", tool_calls=[
            {"id": tm.tool_call_id, "name": "shot", "arguments": {}} for tm in tool_msgs]),
        *tool_msgs,
    ]


def _by_role(messages: list[LLMMessage], role: str) -> list[LLMMessage]:
    return [m for m in messages if m.role == role]


async def _drain(llm, request, **kw) -> None:
    async for _ in stream_llm(llm, request, **kw):
        pass


@pytest.mark.asyncio
async def test_no_vision_downgrades_tool_images_and_adapter_adds_no_user_message() -> None:
    """无视觉模型：gateway 把 tool 图收成文本占位 → adapter 不再产生额外 user 消息。"""
    llm = _CapturingLLM(supports_vision=False)
    await _drain(llm, _request(*_legal(
        LLMMessage(role="tool", content=[TextPart(text="截图"), _img()], tool_call_id="tc1"))))

    tool_msg = _by_role(llm.seen, "tool")[0]
    assert _images(tool_msg.content) == [], "无视觉模型的 tool 消息不得留下图片 part"
    out = oai("", llm.seen)
    assert [m["role"] for m in out] == ["user", "assistant", "tool"], \
        f"不得产生额外 user 消息：{out}"
    assert "[image image/png]" in out[-1]["content"], \
        f"降级后 tool 文本必须留占位：{out[-1]['content']!r}"


@pytest.mark.asyncio
async def test_undeclared_vision_is_treated_as_no_vision() -> None:
    """严格默认：未声明 ``supports_vision`` 的 client 一律按无视觉处理。"""
    llm = _CapturingLLM()  # 不设属性
    await _drain(llm, _request(*_legal(
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"))))
    assert _images(_by_role(llm.seen, "tool")[0].content) == []


@pytest.mark.asyncio
async def test_vision_model_keeps_tool_images() -> None:
    """视觉模型不得被门控误伤——否则门控就成了「一律降级」。"""
    llm = _CapturingLLM(supports_vision=True)
    await _drain(llm, _request(*_legal(
        LLMMessage(role="tool", content=[TextPart(text="截图"), _img()], tool_call_id="tc1"))))

    imgs = _images(_by_role(llm.seen, "tool")[0].content)
    assert len(imgs) == 1 and imgs[0].data == _PNG_B64
    out = oai("", llm.seen)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
    assert _data_url_payload(out[-1]["content"][0]) == _PNG_BYTES


@pytest.mark.asyncio
async def test_gate_only_touches_tool_role() -> None:
    """门控**只降 ``role == "tool"``**：用户递的图在入口已被 ``validate_content`` 门控，
    工具产出的图从未经过入口，是唯一的漏网路径。降别的角色属越权。"""
    llm = _CapturingLLM(supports_vision=False)
    await _drain(llm, _request(
        LLMMessage(role="user", content=[TextPart(text="看图"), _img()]),
        LLMMessage(role="assistant", content=[TextPart(text="收到"), _img()],
                   tool_calls=[{"id": "tc1", "name": "shot", "arguments": {}}]),
        LLMMessage(role="tool", content=[TextPart(text="截图"), _img()], tool_call_id="tc1"),
    ))
    user_msg = _by_role(llm.seen, "user")[0]
    assistant_msg = _by_role(llm.seen, "assistant")[0]
    tool_msg = _by_role(llm.seen, "tool")[0]
    assert len(_images(user_msg.content)) == 1, "user 消息的图不得被门控波及"
    assert len(_images(assistant_msg.content)) == 1, "assistant 消息的图不得被门控波及"
    assert _images(tool_msg.content) == []


@pytest.mark.asyncio
async def test_plain_text_messages_untouched_by_gate() -> None:
    """纯文本消息经门控后**原样是同一对象**（不接 MemoryBlobStore 时行为逐字节不变）。"""
    llm = _CapturingLLM(supports_vision=False)
    msgs = _legal(LLMMessage(role="tool", content="result", tool_call_id="tc1"))
    await _drain(llm, _request(*msgs))
    assert llm.seen == msgs
    assert all(seen is orig for seen, orig in zip(llm.seen, msgs, strict=True)), \
        "纯文本路径不得重建消息对象"


@pytest.mark.asyncio
async def test_no_vision_skips_blob_fetch_for_tool_images() -> None:
    """无视觉时不该为一张注定被降级的图去 MemoryBlobStore 取一趟——门控在 rehydrate 之前。"""
    llm = _CapturingLLM(supports_vision=False)
    store = _CountingStore()
    await _drain(llm, _request(*_legal(LLMMessage(
        role="tool",
        content=[ImagePart(data=_PNG_REF, media_type="image/png", source_type="ref")],
        tool_call_id="tc1",
    ))), blob_store=store, provider_ctx=_ctx())

    assert store.get_calls == [], "被门控降级掉的图不该再触发 blob 取回"
    assert _images(_by_role(llm.seen, "tool")[0].content) == []
    assert "[image image/png]" in oai("", llm.seen)[-1]["content"]


@pytest.mark.asyncio
async def test_vision_model_tool_ref_rehydrated_then_relocated() -> None:
    """视觉模型 + blob ref 的全链路：rehydrate → 重定位，wire 上的 base64 解回原始字节。"""
    llm = _CapturingLLM(supports_vision=True)
    store = _CountingStore()
    await _drain(llm, _request(*_legal(LLMMessage(
        role="tool",
        content=[TextPart(text="截图"),
                 ImagePart(data=_PNG_REF, media_type="image/png", source_type="ref")],
        tool_call_id="tc1",
    ))), blob_store=store, provider_ctx=_ctx())

    assert store.get_calls == [_PNG_REF]
    out = oai("", llm.seen)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "user"]
    assert _data_url_payload(out[-1]["content"][0]) == _PNG_BYTES
