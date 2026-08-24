from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.protocols import ImagePart, TextPart


def _blk(content, role="user", **md):
    return ContextBlock(
        id="blk_1", source="task_conversation", kind="history", target="messages",
        content=content, priority=5, token_estimate=1,
        metadata={"role": role, "type": "user_prompt", "timestamp": "2026-08-23T00:00:00",
                  "seq_no": 0, "task_id": "t1", **md},
    )


def test_plain_text_message_unchanged():
    out = DefaultComposer()._history_to_messages([_blk("hello")])
    assert len(out) == 1 and out[0].content == "hello"


def test_parts_preserved_into_message():
    content = [TextPart(text="看图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    out = DefaultComposer()._history_to_messages([_blk(content)])
    assert out[0].content == content


def test_image_only_message_is_not_dropped():
    """纯图片消息的 content_to_text 是空串——旧的判空会把它静默丢掉。"""
    out = DefaultComposer()._history_to_messages(
        [_blk([ImagePart(data="ZGF0YQ==", media_type="image/png")])])
    assert len(out) == 1, "纯图片消息不得被当成空消息丢弃"


def test_truly_empty_message_still_dropped():
    """空串仍应被丢弃——既有行为不得改变。"""
    assert DefaultComposer()._history_to_messages([_blk("")]) == []


def test_empty_parts_list_dropped():
    assert DefaultComposer()._history_to_messages([_blk([])]) == []
