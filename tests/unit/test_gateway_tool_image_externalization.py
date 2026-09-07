"""gateway 端到端：provider 交 inline base64 → 落库时已是 blob ref。

改造前只有 `media:get_image` 一个生产者，它交出来的本就是 ``source_type="ref"``，于是
gateway 这条路一直没有「把字节存起来」这一步。第三方 provider（MCP 的 ImageContent、
将来的截图工具）交 inline base64 之后，缺的那一步就是：裸 base64 直接落进 memory 记录，
宿主明明注册了 blob store 也用不上。

对照面同样钉死：**没注册 blob store 的宿主行为逐字节不变**（inline 原样跑）。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryScope, ProviderContext, TextPart,
)
from ctx_weft.protocols.capability import (
    CapabilityEvent, CapabilityProviderInfo, ToolCapability, ToolCapabilityProvider,
)
from ctx_weft.protocols.events import EventType
from ctx_weft.protocols.memory import MemoryBlobStore
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 8
PNG_B64 = base64.b64encode(PNG).decode()


class _Store(MemoryBlobStore):
    def __init__(self) -> None:
        self.puts: list[bytes] = []

    async def put(self, data: bytes, media_type: str, ctx) -> str:
        self.puts.append(data)
        return "blob:deadbeef"

    async def get(self, ref: str, ctx):
        return PNG if ref == "blob:deadbeef" else None


class _Prov(ToolCapabilityProvider):
    """一次 result：content 里是 `[TextPart, ImagePart]`（MCP 那条路的形状）。"""

    name = "mcp:shot"

    def __init__(self, parts: list) -> None:
        self._parts = parts

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:shot:go", name="go", description="d")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)
    async def cancel(self, invocation_id, ctx) -> None: return None

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            yield CapabilityEvent(kind="result", payload={
                "content": [TextPart(text="here"), *self._parts]})
        return _run()


async def _run(parts: list, *, store: MemoryBlobStore | None):
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope, resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctxp = ProviderContext(session_id="s1", tenant_id="default",
                           task_id="tsk_1", agent_id="agt_1")
    bus = InProcessEventBus()
    seen: list = []

    async def _sink(ev):
        if ev.type == EventType.CAPABILITY_FINISHED:
            seen.append(ev.payload)

    bus.subscribe(None, _sink)
    ctx = LoopContext(assembler=None, llm=None, memory=mem, event_bus=bus,
                      provider_ctx=ctxp)
    prov = _Prov(parts)
    cache = CapabilityCache()
    cache.put("agt_1", [prov._cap()])
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[prov], memory=mem,
        event_bus=bus, memory_blob_store=store,
    )
    res = await gw.invoke("mcp__shot__go", {}, state, ctx)
    view = await mem.load_view(scope, MemoryScope.TASK, ctxp)
    stored = [r for r in view if r.role == "tool"][-1].content
    return res, stored, (seen[0] if seen else {})


async def test_inline_image_from_a_tool_lands_as_a_ref():
    store = _Store()
    res, stored, payload = await _run(
        [ImagePart(data=PNG_B64, media_type="image/png")], store=store)

    assert store.puts == [PNG], "字节没进 blob store"
    img = res.content[1]
    assert (img.source_type, img.data) == ("ref", "blob:deadbeef")
    assert img.byte_size == len(PNG)
    # 落库的是 ref 形态，不是裸 base64——跨重启取回靠的是 blob store。
    assert stored[1].source_type == "ref"
    assert PNG_B64 not in str(payload), "事件 payload 泄漏了 base64"


async def test_without_a_blob_store_inline_bytes_survive_unchanged():
    """不接 blob 的宿主逐字节不变（多模态设计 Phase 2 的既定形态）。"""
    part = ImagePart(data=PNG_B64, media_type="image/png")
    res, stored, _ = await _run([part], store=None)
    assert res.content[1] == part
    assert stored[1].data == PNG_B64


async def test_illegal_image_becomes_a_placeholder_in_the_tool_result():
    """一张不合格的图不掀掉整次工具调用；模型看到的是占位而不是「什么都没发生」。"""
    res, stored, _ = await _run(
        [ImagePart(data=PNG_B64, media_type="image/bmp")], store=_Store())

    assert [type(p).__name__ for p in res.content] == ["TextPart", "TextPart"]
    assert res.content[0].text == "here"
    assert res.content[1].text == "[image dropped: image/bmp rejected by content validation]"
    assert not any(isinstance(p, ImagePart) for p in stored)


async def test_ref_parts_from_media_get_image_are_left_alone():
    """`media:get_image` 那条路必须逐字节不变——它交的就是 ref 形态。"""
    store = _Store()
    part = ImagePart(data="blob:cafe", media_type="image", source_type="ref")
    res, _, _ = await _run([TextPart(text="位置说明"), part], store=store)
    assert store.puts == []
    assert res.content[-1] is part
