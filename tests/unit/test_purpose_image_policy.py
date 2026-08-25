"""per-purpose 图片策略：只有 act 携带真图，其余四个 purpose 降级成文本占位。

背景（spec §13 的 I3）：Phase 2 探针实测五个 compose purpose **全部**携带 inline
base64。最要命的一条——compaction 恰在上下文超预算时触发，却会把触发它的那些图一起
重发，即在最贵的时刻多打一发最大的请求。

本文件钉三件事：
1. 降级发生在 ``token_count`` 计算**之前**（否则报出的数含图、发出的 prompt 无图，
   budget 与 compact 会基于错误的数字判断）；
2. 占位文本**逐字节确定性**（含随机 id / sha / 时间戳会砸掉该 purpose 自己的 prompt
   前缀缓存，把本任务的收益反转成净损失）；
3. 纯文本会话**零影响**。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler import composer as composer_mod
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.content import downgrade_images_to_text
from ctx_weft.core.utils import _IMAGE_PART_TOKENS, content_to_text, estimate_tokens, image_tokens
from ctx_weft.protocols import ImagePart, TextPart

ALL_PURPOSES = ("act", "compact", "observe", "recognize_intent", "background_observe")
DOWNGRADED_PURPOSES = ("compact", "observe", "recognize_intent", "background_observe")


def _identity(text: str) -> ContextBlock:
    return ContextBlock(id="id", source="identity", kind="identity", target="system",
                        content=text, priority=0, token_estimate=1, metadata={})


def _hist(content) -> ContextBlock:
    """一条 USER_PROMPT 历史记录——图片就是经这条路进 prompt 的（memory 保 parts）。"""
    return ContextBlock(id="h1", source="task_conversation", kind="history", target="messages",
                        content=content, priority=1, token_estimate=1,
                        metadata={"role": "user", "type": "user_prompt", "task_id": "t1",
                                  "timestamp": "2026-01-01T00:00:00", "seq_no": 1})


def _req(purpose: str):
    task = SimpleNamespace(user_prompt_in_memory=True, process_report=None,
                           title="T", description="D", user_prompt="look", id="t1")
    template = SimpleNamespace(identity={"act": SimpleNamespace(text="ACT-SOUL", style=None)})
    return SimpleNamespace(purpose=purpose, task=task, template=template, extra={},
                           token_counter=estimate_tokens)


def _image_content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


async def _compose(purpose: str, content):
    return await DefaultComposer().compose([_identity("P"), _hist(content)], _req(purpose))


def _image_parts(content):
    """内容里的图片 part。守卫 isinstance：``not hasattr(p,"text")`` 在 ``str`` 上恒为
    True，不加守卫时"内容含图"的断言在被拍扁成字符串的内容上是重言式。"""
    assert isinstance(content, list), f"内容已被拍扁成 {type(content).__name__}，断言无意义"
    return [p for p in content if not hasattr(p, "text")]


# ── 1. act 保留真图 ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_act_keeps_real_image():
    prompt = await _compose("act", _image_content())
    user = [m for m in prompt.messages if m.role == "user"][0]
    imgs = _image_parts(user.content)
    assert len(imgs) == 1, "act 必须把真图发给模型"
    assert imgs[0].data == "ZGF0YQ==", "act 路径上 base64 不得被替换"
    assert imgs[0].media_type == "image/png"
    assert image_tokens(user.content) == _IMAGE_PART_TOKENS, "act 的 token 数应含图"


# ── 2/3. 其余四个 purpose 降级 ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", DOWNGRADED_PURPOSES)
async def test_non_act_purposes_downgrade_image_to_placeholder(purpose):
    prompt = await _compose(purpose, _image_content())
    for m in prompt.messages:
        if isinstance(m.content, list):
            assert _image_parts(m.content) == [], f"{purpose} 不得携带任何图片 part"
    flat = "\n".join(content_to_text(m.content) for m in prompt.messages)
    assert "[image image/png]" in flat, "降级须留下确定性占位，而不是直接删掉"
    assert "ZGF0YQ==" not in flat, "base64 不得以任何形式残留"


# ── (1) 要害：降级必须发生在 token_count 之前 ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", DOWNGRADED_PURPOSES)
async def test_token_count_excludes_image_tokens_for_downgraded_purposes(purpose):
    """报出的 token 数必须与**实际发出**的 prompt 一致。

    降级若发生在 token_count 之后，报出的数含 1600/张、发出的 prompt 已无图——
    budget 裁剪与 compact 触发都会基于错误的数字判断。
    """
    prompt = await _compose(purpose, _image_content())
    assert all(image_tokens(m.content) == 0 for m in prompt.messages), \
        "降级后不该还有 part 被计成图片"
    expected = _req(purpose).token_counter(prompt.system) + sum(
        estimate_tokens(content_to_text(m.content)) for m in prompt.messages
    )
    assert prompt.token_count == expected, "token_count 必须由降级**后**的消息算出"

    with_image = await _compose(purpose, _image_content())
    text_only = await _compose(purpose, [TextPart(text="看这张图")])
    assert with_image.token_count - text_only.token_count < _IMAGE_PART_TOKENS, \
        "含图与纯文本的差额不应包含整份图片 token"


@pytest.mark.asyncio
async def test_act_token_count_still_includes_image_tokens():
    """对照组：act 不降级，其 token_count 仍须含图——否则上面那条可能只是图整体没进来。"""
    prompt = await _compose("act", _image_content())
    assert prompt.token_count > _IMAGE_PART_TOKENS


# ── (2) 占位确定性 ────────────────────────────────────────────────────────────


def test_placeholder_is_byte_identical_across_calls():
    """同一份含图内容连续降级两次，产出完全相等（裁定 D2 的硬约束）。"""
    first = downgrade_images_to_text(_image_content())
    second = downgrade_images_to_text(_image_content())
    assert first == second
    assert [p.text for p in first] == ["看这张图", "[image image/png]"]


def test_placeholder_carries_no_entropy():
    """占位不得含 blob sha / 随机 id / 时间戳 / 计数器——它们会砸掉该 purpose 自己的
    prompt 前缀缓存，而 compact 恰在最需要命中缓存时触发。"""
    out = downgrade_images_to_text(
        [ImagePart(data="c2hhLXNoYS1zaGE=", media_type="image/jpeg", source_type="ref")]
    )
    assert [p.text for p in out] == ["[image image/jpeg]"]


def test_multiple_images_share_one_placeholder():
    """同一条消息内多张同类型图共用相同占位——不加序号（无需区分它们）。"""
    out = downgrade_images_to_text([
        ImagePart(data="YQ==", media_type="image/png"),
        ImagePart(data="Yg==", media_type="image/png"),
    ])
    assert [p.text for p in out] == ["[image image/png]", "[image image/png]"]


def test_missing_media_type_falls_back_to_generic_word():
    out = downgrade_images_to_text([ImagePart(data="YQ==", media_type="")])
    assert [p.text for p in out] == ["[image image]"]


# ── (4) 纯文本零影响 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("content", ["hello", "", None, [], [TextPart(text="hi")]])
def test_text_only_content_returned_as_same_object(content):
    """无图内容原样返回**同一对象**——降级逻辑对纯文本零开销、零改写。"""
    assert downgrade_images_to_text(content) is content


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ALL_PURPOSES)
async def test_text_only_compose_is_byte_identical(purpose, monkeypatch):
    """纯文本会话：五个 purpose 的产出与「根本没有降级逻辑」时逐字节相同。

    基线由把 ``downgrade_images_to_text`` 换成恒等函数得到——AssembledPrompt 是
    dataclass，相等即 system / messages / tools / token_count 全部逐字节相等。
    """
    text_only = [TextPart(text="看这段文字")]
    monkeypatch.setattr(composer_mod, "downgrade_images_to_text", lambda c: c)
    baseline = await _compose(purpose, text_only)
    monkeypatch.undo()
    actual = await _compose(purpose, text_only)
    assert actual == baseline
    assert all(isinstance(m.content, (str, list)) for m in actual.messages)
    assert "[image" not in "\n".join(content_to_text(m.content) for m in actual.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ALL_PURPOSES)
async def test_str_content_message_untouched(purpose):
    """history 里的 str 形态 content 必须保持 str（降级不得把它变成 list）。"""
    prompt = await _compose(purpose, "纯文本历史")
    assert all(isinstance(m.content, str) for m in prompt.messages)


# ── (5) 降级产物仍是合法 LLMMessage ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", DOWNGRADED_PURPOSES)
async def test_downgraded_messages_stay_valid(purpose):
    """不产生空 content、role 与 tool_call 元信息原样保留。"""
    prompt = await _compose(purpose, [ImagePart(data="ZGF0YQ==", media_type="image/png")])
    for m in prompt.messages:
        assert m.role in ("user", "assistant", "system", "tool")
        assert m.content, "降级不得把消息掏空"
        if isinstance(m.content, list):
            assert _image_parts(m.content) == []
    flat = "\n".join(content_to_text(m.content) for m in prompt.messages)
    assert "[image image/png]" in flat, "纯图消息降级后仍须留下可见文本"


# ── dict 形态 part ────────────────────────────────────────────────────────────


def test_dict_shaped_image_is_downgraded():
    """dict 形态的图片同样降级。产出刻意是 dataclass TextPart 而非 dict——
    ``core/utils`` 的 content_to_text / image_part_count 是 dict-blind 的（判据被
    spec §13 冻结），回吐 dict 文本会让占位对摘要器不可见、且仍被计成一张图的 token。"""
    out = downgrade_images_to_text([{"type": "image", "data": "YQ==", "media_type": "image/webp"}])
    assert [p.text for p in out] == ["[image image/webp]"]
    assert image_tokens(out) == 0, "降级后不得还被计成图片 token"


def test_dict_shaped_text_part_is_not_mistaken_for_an_image():
    """dict 上永远取不到 ``.text`` 属性——直接套冻结判据会把 dict 纯文本当图片、
    换成 ``[image image]``，是实打实的内容损坏。判 ``type`` 才对。"""
    content = [{"type": "text", "text": "hello"}]
    assert downgrade_images_to_text(content) is content
