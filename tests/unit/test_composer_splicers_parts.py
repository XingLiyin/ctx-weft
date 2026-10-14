from ctx_weft.core.assembler.prompt_document import PromptDocument
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


# ── 纯文本：与改造前逐字节相同 ────────────────────────────────────────────

def test_prepend_str_unchanged():
    document = PromptDocument([LLMMessage(role="user", content="body")])
    document.wrap(document.refs[0], before="head\n\n---\n\n")
    out = document.messages
    assert out[0].content == "head\n\n---\n\nbody"


def test_append_at_str_unchanged():
    document = PromptDocument([LLMMessage(role="user", content="body")])
    document.wrap(document.refs[0], after="\n\ntail")
    out = document.messages
    assert out[0].content == "body\n\ntail"


def test_append_last_str_unchanged():
    document = PromptDocument([LLMMessage(role="user", content="body")])
    document.append(LLMMessage(role="user", content="tail"), merge_with_last=True)
    out = document.messages
    assert out[-1].content == "body\n\ntail"


def test_empty_text_is_noop():
    msgs = [LLMMessage(role="user", content="body")]
    document = PromptDocument(msgs)
    refs = document.refs
    assert document.append(LLMMessage(role="user", content=""), merge_with_last=True) is None
    assert document.messages == msgs and document.refs == refs
    assert document.trace == []


# ── 多模态：图片必须存活 ──────────────────────────────────────────────────

def _parts_msg():
    return LLMMessage(role="user", content=[TextPart(text="body"), _img()])


def test_prepend_keeps_image():
    document = PromptDocument([_parts_msg()])
    document.wrap(document.refs[0], before="head\n\n---\n\n")
    out = document.messages
    assert any(not hasattr(p, "text") for p in out[0].content), "图片不得丢失"
    assert out[0].content[0].text == "head\n\n---\n\nbody"


def test_append_at_keeps_image():
    document = PromptDocument([_parts_msg()])
    document.wrap(document.refs[0], after="\n\ntail")
    out = document.messages
    assert any(not hasattr(p, "text") for p in out[0].content)
    # content_with_suffix（Phase 1）新插 TextPart 时保留分隔符前缀，与 str 路径的
    # f"{base}\n\ntail" 语义一致；brief 里的 == "tail" 断言与该既有契约不符，按源码调整。
    assert out[0].content[-1].text == "\n\ntail", "尾部为图片时应新插一个 TextPart"


def test_append_last_keeps_image():
    document = PromptDocument([_parts_msg()])
    document.append(LLMMessage(role="user", content="tail"), merge_with_last=True)
    out = document.messages
    # 先守住类型：拍扁成 str 会让下面的逐 part 断言对字符做假阳性判定。
    assert isinstance(out[-1].content, list), "必须仍是 part 列表，不能被拍扁成 str"
    assert any(not hasattr(p, "text") for p in out[-1].content)


def test_append_last_creates_user_when_tail_not_user():
    """末条非 user 时新建一条 user 回合——既有行为，不得改变。"""
    document = PromptDocument([LLMMessage(role="assistant", content="x")])
    document.append(LLMMessage(role="user", content="tail"), merge_with_last=True)
    out = document.messages
    assert out[-1].role == "user" and out[-1].content == "tail"
