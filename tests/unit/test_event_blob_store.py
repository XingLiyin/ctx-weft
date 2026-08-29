"""EventBlobStore：事件流侧独立的 blob 协议。

spec: docs/superpowers/specs/2026-08-27-dual-blob-store-design.md

与 MemoryBlobStore 同形但**类型无关**——两侧语义会各自演进（最明显的是回收锚点不同：
memory 侧是记录 is_superseded，event 侧是事件保留策略）。host 要共用就一个类同时实现两者。
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from ctx_weft.core.content import normalize_content
from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import ImagePart, ProviderContext, TextPart
from ctx_weft.protocols.events import EventBlobStore, NullEventBlobStore
from ctx_weft.protocols.memory import MemoryBlobStore


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="ses_1", tenant_id="default")


class _Stub(EventBlobStore):
    """最小实现，验协议可被继承。"""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        ref = f"blob:{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


def test_null_store_cannot_externalize() -> None:
    assert NullEventBlobStore().can_externalize is False


def test_stub_can_externalize_by_default() -> None:
    """基类默认 True——既有实现无需改动就是「能存」。"""
    assert _Stub().can_externalize is True


async def test_null_store_get_returns_none_never_raises() -> None:
    """get 对不存在的 ref 恒返 None 不抛：取图失败绝不能中断 loop。"""
    assert await NullEventBlobStore().get("blob:nope", _ctx()) is None


async def test_null_store_put_raises_loudly() -> None:
    """put 刻意抛错：调用方应先探询 can_externalize，而不是调用后捕异常。

    把「响亮失败」降级成控制流，会让真正的接线错误也被静默吞掉。
    """
    with pytest.raises(NotImplementedError):
        await NullEventBlobStore().put(b"x", "image/png", _ctx())


def test_is_independent_of_memory_blob_store() -> None:
    """同形但**类型无关**——不是子类型，也不共用一个 ABC（spec §3）。"""
    assert not issubclass(EventBlobStore, MemoryBlobStore)
    assert not issubclass(MemoryBlobStore, EventBlobStore)


def test_one_class_can_implement_both() -> None:
    """host 要共用就一个类同时继承两者——这是「可分可合」的合的那一半。"""

    class Both(MemoryBlobStore, EventBlobStore):
        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    both = Both()
    assert isinstance(both, MemoryBlobStore)
    assert isinstance(both, EventBlobStore)


def test_registry_explicit_registration() -> None:
    reg = ProviderRegistry()
    assert reg.get_event_blob_store().can_externalize is False  # 默认 Null
    stub = _Stub()
    reg.register_event_blob_store(stub)
    assert reg.get_event_blob_store() is stub


def test_registry_does_not_fall_back_to_memory_provider() -> None:
    """**刻意不自动回落**（spec §4）：自动解析会让「共用」成为隐式默认，
    而本设计的出发点正是让两者可分。host 要共用就把同一个实例注册两次。
    """

    class BothProvider(MemoryBlobStore, EventBlobStore):
        name = "both"

        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    reg = ProviderRegistry()
    reg.register_memory_blob_store(BothProvider())
    # memory 侧拿得到，event 侧仍是 Null——不串门
    assert reg.get_memory_blob_store().can_externalize is True
    assert reg.get_event_blob_store().can_externalize is False


def test_registry_does_not_fall_back_from_memory_provider_to_event_blob_store() -> None:
    """真正要防的回落是 `get_event_blob_store()` 从 **memory provider** 解析
    （对称于 `get_memory_blob_store()` 中间那一级：
    ``isinstance(self._memory, MemoryBlobStore) and mem.can_externalize`` 回落）。

    上一条用例（`test_registry_does_not_fall_back_to_memory_provider`）走的是
    `register_memory_blob_store`，从不调 `register_memory`，故 `self._memory`
    恒为 None——即便后人给 `get_event_blob_store()` 加上完全对称的
    `isinstance(self._memory, EventBlobStore)` 回落，那条用例也测不出来。

    这里改用 `register_memory()`：provider 本身同时实现 `MemoryProvider`
    （借用仓内现成的 `InMemoryMemoryProvider` 满足完整协议，避免手搓一遍全部
    抽象方法）、`MemoryBlobStore`、`EventBlobStore` 三个协议，注册为 memory
    provider 后断言 `get_event_blob_store()` 仍是 Null。
    """
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    class BothProvider(InMemoryMemoryProvider, MemoryBlobStore, EventBlobStore):
        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    reg = ProviderRegistry()
    reg.register_memory(BothProvider())
    # memory provider 自身可外部化（对称于 get_memory_blob_store 的中间一级）……
    assert reg.get_memory_blob_store().can_externalize is True
    # ……但 event 侧绝不能从 memory provider 回落解析，必须仍是 Null。
    assert reg.get_event_blob_store().can_externalize is False


# ── normalize_content 只写 memory 侧（blob-store 解耦 Task 1）────────────────
# 入口双写已删除（原 Task 2 的双写逻辑）：normalize_content 不再接受
# event_blob_store 参数。event 侧的外部化改由 content_to_event_jsonable
# 在事件发射点独立完成，见下一节。

_RAW = b"\x89PNG\r\n\x1a\n" + b"payload" * 20
_B64 = base64.b64encode(_RAW).decode("ascii")


class _MemStub(_Stub):
    """与 _Stub 同实现，只为在测试里区分两个 store 实例。"""


async def test_plain_text_is_untouched() -> None:
    mem = _MemStub()
    s = "纯文本"
    assert await normalize_content(s, blob_store=mem, ctx=_ctx()) is s
    assert mem.blobs == {}


async def test_ref_parts_are_not_re_externalized() -> None:
    """已是 ref 的 part 原样保留，不重复 put。"""
    mem = _MemStub()
    part = ImagePart(data="blob:already", media_type="image/png", source_type="ref")
    out = await normalize_content([part], blob_store=mem, ctx=_ctx())
    assert out[0] is part
    assert mem.blobs == {}


# ── content_to_event_jsonable（Task 3）─────────────────────────────────────

from ctx_weft.core.content import content_to_event_jsonable


async def test_inline_base64_is_externalized_here() -> None:
    """memory 侧无 blob 时入口不外部化，content 里仍是 inline base64——
    只要 event blob 可用，事件侧仍能独立完成 ref 化。这是「所有 base64 变引用」
    在 memory 无 blob 时也成立的关键（spec §6）。
    """
    evt = _Stub()
    out = await content_to_event_jsonable(
        [ImagePart(data=_B64, media_type="image/png")],
        event_blob_store=evt, ctx=_ctx(),
    )
    assert out[0]["source_type"] == "ref"
    assert out[0]["data"].startswith("blob:")
    assert _B64 not in str(out), "事件载荷里绝不能出现字节"
    assert len(evt.blobs) == 1


async def test_plain_text_returns_same_object() -> None:
    evt = _Stub()
    s = "纯文本"
    assert await content_to_event_jsonable(
        s, event_blob_store=evt, ctx=_ctx()) is s
    assert await content_to_event_jsonable(
        None, event_blob_store=evt, ctx=_ctx()) is None


async def test_transitional_helper_is_gone() -> None:
    """`content_to_jsonable_refs_only` 是本设计落地前的过渡实现，应已删除。"""
    import ctx_weft.core.content as c
    assert not hasattr(c, "content_to_jsonable_refs_only")


async def test_raises_loudly_when_event_store_cannot_externalize() -> None:
    """Task 4 收口：不再有「无 EventBlobStore 时退回 content_to_jsonable」的过渡短路。

    携图内容能不能到这里，由 `validate_content` 入口的第三道门控把关——真的绕过
    入口跑到这里（本用例就是这么干的：直接调本函数，不经入口），event blob 又不可用，
    必须撞上 `NullEventBlobStore.put` 的 `NotImplementedError`，响亮且可诊断，而不是
    静默把 inline base64 原样塞回 payload。
    """
    with pytest.raises(NotImplementedError):
        await content_to_event_jsonable(
            [TextPart(text="看图"), ImagePart(data=_B64, media_type="image/png")],
            event_blob_store=NullEventBlobStore(), ctx=_ctx(),
        )


async def test_url_part_is_downgraded_not_silently_passed_through() -> None:
    """既非 ref 也非 base64 的图（如 url）不得静默透传（Task 3 review Finding 3）。

    `url` 形态可以携带 ``data:`` URI——静默 `content_to_jsonable` 序列化会把字节
    原样写进事件 payload，直接击穿「事件库恒不含字节」。同
    `content_to_jsonable_refs_only`（本函数取代的过渡实现）对非 ref 图的处理口径
    一致：降级成 `[image {media_type}]` 占位。
    """
    evt = _Stub()
    part = ImagePart(data="https://example.com/a.png", media_type="image/png",
                     source_type="url")
    out = await content_to_event_jsonable([part], event_blob_store=evt, ctx=_ctx())
    assert out == [{"type": "text", "text": "[image image/png]"}]
    assert "https://example.com" not in str(out)
    assert evt.blobs == {}, "url 形态不 put，直接降级"
