"""SESSION_CREATED / SESSION_RESUMED 的 user_prompt：保 ref、不落字节、不拍扁。

裁定（2026-08-27）：`Session.user_prompt` 与两条 session 事件的 payload
**不能拍扁**（图片结构必须留下）、也**不能内联 base64 字节**（事件库体积）——
一律走 ref 形态。宿主没接 MemoryBlobStore 时手里没有 ref，那些 inline 图降级成不含
字节的占位 part（复用 per-purpose 的 `[image {media_type}]` 文案，不新增第六种占位）。

对照组：`TASK_CREATED` 早已是 `content_to_jsonable`（无损），本文件钉的是 session 侧。
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from ctx_weft.core.content import content_to_jsonable_refs_only
from ctx_weft.core.control.reducers import deserialize_view, rebuild_view, serialize_view
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.state.event_store import InMemoryEventStore
from ctx_weft.protocols import ImagePart, TextPart

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode("ascii")


def _ref_part() -> ImagePart:
    return ImagePart(data="blob:deadbeef", media_type="image/png",
                     source_type="ref", byte_size=4096)


def _inline_part() -> ImagePart:
    return ImagePart(data=_PNG, media_type="image/png")


# ── content_to_jsonable_refs_only ────────────────────────────────────────────


def test_str_returns_same_object() -> None:
    """纯文本路径零成本、逐字节不变。"""
    s = "hello"
    assert content_to_jsonable_refs_only(s) is s
    assert content_to_jsonable_refs_only(None) is None


def test_ref_part_survives_intact() -> None:
    """ref 形态原样保留——data 是短标记，可回读、可 rehydrate。"""
    out = content_to_jsonable_refs_only([TextPart(text="看图"), _ref_part()])
    assert out == [
        {"type": "text", "text": "看图"},
        {"type": "image", "data": "blob:deadbeef", "media_type": "image/png",
         "source_type": "ref", "byte_size": 4096},
    ]


def test_inline_base64_never_reaches_payload() -> None:
    """inline base64 降级成占位——事件库里不得出现字节。"""
    out = content_to_jsonable_refs_only([TextPart(text="看图"), _inline_part()])
    assert out == [
        {"type": "text", "text": "看图"},
        {"type": "text", "text": "[image image/png]"},
    ]
    assert _PNG not in str(out)


def test_mixed_ref_and_inline() -> None:
    """混合形态逐 part 判定，ref 留、inline 降。"""
    out = content_to_jsonable_refs_only([_ref_part(), _inline_part()])
    assert out[0]["type"] == "image" and out[0]["source_type"] == "ref"
    assert out[1] == {"type": "text", "text": "[image image/png]"}


def test_url_part_is_downgraded_too() -> None:
    """url 形态同样不是 ref，一并降级（本仓尚不支持 url，防御性钉住）。"""
    part = ImagePart(data="https://example.com/a.png", media_type="image/png",
                     source_type="url")
    assert content_to_jsonable_refs_only([part]) == [
        {"type": "text", "text": "[image image/png]"}]


def test_structure_is_never_flattened() -> None:
    """恒为 parts 结构——拍扁会让「这里曾有一张图」的信息消失。"""
    out = content_to_jsonable_refs_only([TextPart(text="a"), _inline_part()])
    assert isinstance(out, list) and len(out) == 2


# ── 事件流往返 ────────────────────────────────────────────────────────────────


async def _store_with(payload_prompt: object) -> InMemoryEventStore:
    store = InMemoryEventStore()
    await store.append(Event(
        id="evt_0001", run_id="run_1", sequence=1, session_id="ses_1",
        tenant_id="default", type=EventType.SESSION_CREATED,
        timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
        payload={"user_prompt": payload_prompt, "template_id": "tpl",
                 "root_agent_id": "agt_1"},
    ))
    return store


async def test_rebuild_view_restores_parts_from_session_created() -> None:
    """重放 SESSION_CREATED 还原出 parts 结构与 ref，而不是一个 str。"""
    payload = content_to_jsonable_refs_only([TextPart(text="看图"), _ref_part()])
    view = await rebuild_view(await _store_with(payload), "ses_1")
    prompt = view.sessions["ses_1"].user_prompt
    assert isinstance(prompt, list)
    assert prompt[0].text == "看图"
    assert prompt[1].source_type == "ref"
    assert prompt[1].data == "blob:deadbeef"


async def test_legacy_str_payload_still_replays() -> None:
    """存量事件的 user_prompt 是裸 str——必须原样还原，零数据迁移。"""
    view = await rebuild_view(await _store_with("旧的纯文本"), "ses_1")
    assert view.sessions["ses_1"].user_prompt == "旧的纯文本"


async def test_view_snapshot_roundtrip_keeps_parts() -> None:
    """serialize_view / deserialize_view 往返不丢 parts。"""
    payload = content_to_jsonable_refs_only([TextPart(text="看图"), _ref_part()])
    view = await rebuild_view(await _store_with(payload), "ses_1")
    back = deserialize_view(serialize_view(view))
    prompt = back.sessions["ses_1"].user_prompt
    assert isinstance(prompt, list)
    assert prompt[1].data == "blob:deadbeef"
