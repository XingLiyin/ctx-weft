"""approval 的人工备注带图时，图必须随工具结果到达模型。

AuthorizationDecision.message 此前是 str，human.py 用 content_to_text 展平；gateway 两处
拼接是 f-string，遇到 parts 会拍成 repr。改走 content_with_prefix / content_with_suffix
——对 str 逐字节原样，对 parts 并进首/末个 TextPart。

本文件全程走真实 gateway 路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from ctx_weft.core.utils.content import collect_blob_refs
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols import ImagePart, MemoryAddress, MemoryScope, ProviderContext, TextPart
from ctx_weft.protocols.capability import (
    AuthorizationDecision,
    Authorizer,
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

NOTE_REF = "blob:" + "b" * 64
TOOL_REF = "blob:" + "c" * 64


def _img(ref: str) -> ImagePart:
    return ImagePart(data=ref, media_type="image/png", source_type="ref")


class _Prov(ToolCapabilityProvider):
    name = "mcp:t"

    def __init__(self, text: str, parts: list | None = None) -> None:
        self._text, self._parts = text, parts

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:t:go", name="go", description="d")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)
    async def cancel(self, invocation_id, ctx) -> None: return None

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            content = ([TextPart(text=self._text), *self._parts]
                       if self._parts else self._text)
            yield CapabilityEvent(kind="result", payload={"content": content})
        return _run()


class _Az(Authorizer):
    """按构造参数返回放行/拒绝 + 任意形态的 message。"""

    def __init__(self, message, *, allowed: bool = True) -> None:
        self._message, self._allowed = message, allowed

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):
        return AuthorizationDecision(allowed=self._allowed, message=self._message)


async def _run(provider, authorizer):
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default",
                                     task_id="tsk_1", agent_id="agt_1"),
    )
    cache = CapabilityCache()
    cache.put("agt_1", [provider._cap()])
    gw = CapabilityGateway(
        capability_cache=cache, capability_providers=[provider],
        memory=mem, event_bus=InProcessEventBus(), default_authorizer=authorizer,
    )
    return await gw.invoke("mcp__t__go", {}, state, ctx), mem


@pytest.mark.asyncio
async def test_human_note_with_image_reaches_the_model():
    """🔴 本任务存在的理由：审批备注里的图必须随工具结果送到模型面前。"""
    res, mem = await _run(_Prov("tool output"),
                        _Az([TextPart(text="看这个"), _img(NOTE_REF)]))
    assert isinstance(res.content, list)
    assert res.content[0].text == "[Human note: 看这个]\ntool output"
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF]

    # GC 的 mark 判据（collect_blob_refs）必须能在已入库的那条 tool 记录上看到这个 ref——
    # 否则宽限期一过，字节被回收，模型读到的 get_image 会取不回刚刚贴进来的图。
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    view = await mem.load_view(scope, MemoryScope.TASK, ProviderContext(
        session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"))
    tool_record = next(r for r in view if r.role == "tool")
    assert NOTE_REF in collect_blob_refs(tool_record)


@pytest.mark.asyncio
async def test_note_parts_precede_tool_result_parts():
    """顺序裁定：备注图在工具图之前——与文本顺序一致（[Human note: …] 也在前）。"""
    res, _ = await _run(_Prov("out", [_img(TOOL_REF)]),
                        _Az([TextPart(text="注意"), _img(NOTE_REF)]))
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF, TOOL_REF]


@pytest.mark.asyncio
async def test_plain_text_note_keeps_content_a_str():
    """备注是纯 str 且工具无 parts → content 仍是 str，逐字节不变（Global Constraint 一）。"""
    res, _ = await _run(_Prov("tool output"), _Az("be careful"))
    assert isinstance(res.content, str)
    assert res.content == "[Human note: be careful]\ntool output"


@pytest.mark.asyncio
async def test_blocked_with_image_keeps_the_part_and_both_affixes():
    """拒绝路径：[Blocked by human: …] 的前后缀都并进文本 part，图不丢。"""
    res, mem = await _run(_Prov("never runs"),
                          _Az([TextPart(text="不许跑"), _img(NOTE_REF)], allowed=False))
    assert res.is_error is True
    assert isinstance(res.content, list)
    assert res.content[0].text == "[Blocked by human: 不许跑]"
    assert [p.data for p in res.content if not hasattr(p, "text")] == [NOTE_REF]


@pytest.mark.asyncio
async def test_blocked_plain_text_is_byte_identical():
    """纯文本拒绝路径必须与改造前的 f-string 逐字节相同。"""
    res, _ = await _run(_Prov("never runs"), _Az("no", allowed=False))
    assert res.content == "[Blocked by human: no]"
