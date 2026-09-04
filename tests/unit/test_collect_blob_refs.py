"""blob_refs 字段 + collect_blob_refs（mark 判据）。

缺陷记录：docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.core.utils.content import collect_blob_refs
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEvent, MemoryKind, MemoryRecord,
    MemoryScope, TextPart,
)


def _addr() -> MemoryAddress:
    return MemoryAddress(session_id="ses_1", agent_id="agt_1", task_id="tsk_1")


def _event(**kw) -> MemoryEvent:
    base = dict(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc), content="hi",
    )
    base.update(kw)
    return MemoryEvent(**base)


def test_event_blob_refs_defaults_empty() -> None:
    assert _event().blob_refs == []


def test_record_blob_refs_defaults_empty() -> None:
    rec = MemoryRecord(id="rec_1", type=None, content="hi", timestamp=datetime.now(timezone.utc))
    assert rec.blob_refs == []


def test_blob_refs_is_not_shared_between_instances() -> None:
    """default_factory 而非可变默认值——共享 list 会让一条记录的 ref 污染另一条。"""
    a, b = _event(), _event()
    a.blob_refs.append("blob:aaa")
    assert b.blob_refs == []


_REF_A = "blob:aaaaaaaa"
_REF_B = "blob:bbbbbbbb"


def _img(ref: str) -> ImagePart:
    return ImagePart(data=ref, media_type="image/png", source_type="ref")


def test_collects_structural_ref_parts() -> None:
    ev = _event(content=[TextPart(text="看图"), _img(_REF_A)])
    assert collect_blob_refs(ev) == [_REF_A]


def test_collects_declared_blob_refs() -> None:
    """L0.5 之后的形态：content 里只剩文本占位，ref 靠 blob_refs 声明。"""
    ev = _event(content=[TextPart(text="[image blob:aaaaaaaa media_type=image/png]")],
                blob_refs=[_REF_A])
    assert collect_blob_refs(ev) == [_REF_A]


def test_unions_both_sources_without_duplicates() -> None:
    ev = _event(content=[_img(_REF_A)], blob_refs=[_REF_A, _REF_B])
    assert collect_blob_refs(ev) == [_REF_A, _REF_B]


def test_plain_text_yields_nothing() -> None:
    assert collect_blob_refs(_event(content="纯文本")) == []
    # 测试没有 content 属性的对象
    class NoContent:
        pass
    assert collect_blob_refs(NoContent()) == []


def test_does_not_parse_placeholder_text() -> None:
    """占位文案不是判据——没有 blob_refs 声明就采不到，这是刻意的。

    解析文案会把占位格式知识泄进归一层，而 core/media/refs.py 是本仓唯一
    知道占位长什么样的地方。声明式采集正是为了避免那种耦合。
    """
    ev = _event(content=[TextPart(text=f"[image {_REF_A} media_type=image/png]")])
    assert collect_blob_refs(ev) == []


def test_ignores_non_ref_image_parts() -> None:
    """inline base64 不是 blob 引用。"""
    ev = _event(content=[ImagePart(data="iVBORw0KGgo=", media_type="image/png")])
    assert collect_blob_refs(ev) == []
