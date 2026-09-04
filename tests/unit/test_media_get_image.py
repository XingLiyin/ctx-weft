"""Phase 4 Task 4：`media:get_image` —— 取回被 L0.5 降级掉的图（子设计 §4.2 / §8 / §10）。

Task 2 把图收起来（真图 → 含 ref 的占位，落库），本模块把它取回来。取回的图随 tool
result 回到对话**尾部**（KV cache，子设计 §4.4），因此位置信息文本是取舍的**补偿**而
非装饰——它必须指向正确的那条 user 回合，故本文件专门钉这一条。

⚠️ 「ref 不存在时不返回 ImagePart」是「某件事没有发生」型断言，一个什么都不返回的实现
同样通过。故每处都配了**同一份视图上「存在的 ref 确实取回了 ImagePart」的对照**
（见 `test_unknown_ref_returns_text_only_but_known_ref_still_returns_image`
与 `test_no_blob_store_yields_text_but_with_blob_store_yields_image`），Step 6 另做变异验证。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY, CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.media import demote_for_budget, get_image
from ctx_weft.core.media.capability import (
    GET_IMAGE_DESCRIPTION,
    GET_IMAGE_ID,
    MAX_REFS_PER_CALL,
    MediaCapabilityProvider,
    _ordinal,
)
from ctx_weft.core.media.refs import encode_image_placeholder
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.core.registry import ProviderRegistry
from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.protocols import (
    MemoryBlobStore,
    ImagePart,
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    TextPart,
)
from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    CapabilityProviderInfo,
    qualify,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

_BASE = datetime(2026, 8, 27, tzinfo=UTC)
_ADDR = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")

REF = "blob:" + "ab" * 32
OTHER_REF = "blob:" + "cd" * 32
MT = "image/png"


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default",
                           task_id="tsk_1", agent_id="agt_1")


def _ph(ref: str = REF, media_type: str = MT) -> TextPart:
    """L0.5 占位。**经 refs 编码**——本文件不自造第二份占位文本。"""
    return TextPart(text=encode_image_placeholder(ref, media_type))


def _img(ref: str = REF) -> ImagePart:
    return ImagePart(data=ref, media_type=MT, source_type="ref", byte_size=4096)


class _Blobs(MemoryBlobStore):
    """假 blob store：只认预置的 ref。未预置 → get 返回 None（与 NullMemoryBlobStore 同）。"""

    def __init__(self, **blobs: bytes) -> None:
        self._d = {REF: b"PNGBYTES" * 4} if not blobs else dict(blobs)

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        raise NotImplementedError

    async def get(self, ref: str, ctx: ProviderContext) -> "tuple[bytes, str] | None":
        data = self._d.get(ref)
        return (data, MT) if data is not None else None


async def _seed(mem, rows) -> None:
    """rows = [(role, content), ...]，按顺序落库（timestamp 递增，保住视图顺序）。"""
    for i, (role, content) in enumerate(rows):
        await mem.ingest(MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_ADDR,
            content=content, timestamp=_BASE + timedelta(seconds=i), role=role,
        ), _pctx())


def _images(parts) -> list:
    """判据 `not hasattr(p, "text")` —— 裁定 D1 冻结，测试侧同源。"""
    return [p for p in parts if not hasattr(p, "text")]


def _text(parts) -> str:
    return "\n".join(p.text for p in parts if hasattr(p, "text"))


# ── 1. 降级过的记录 → [TextPart, ImagePart] ───────────────────────────────────


async def test_round_trip_from_real_demotion_returns_text_then_image() -> None:
    """Task 2 降下去、Task 4 取回来：整条回路走真实的 `demote_for_budget`，不手搓占位。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [TextPart(text="look"), _img()])])

    assert await demote_for_budget(mem, _ADDR, _pctx(), keep_recent=0) == 1

    parts = await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs())

    assert len(parts) == 2
    assert hasattr(parts[0], "text")            # [0] 是位置信息文本
    img = parts[1]
    assert not hasattr(img, "text")             # [1] 是图（判据 D1）
    assert img.data == REF
    assert img.media_type == MT
    assert img.source_type == "ref"             # 不塞 base64，rehydrate 归 adapter
    assert img.byte_size == len(b"PNGBYTES" * 4)  # 体积回填，供 image_tokens 估预算


async def test_restored_text_names_the_ref_and_says_it_is_at_the_end() -> None:
    """文本要讲清两件事：这是哪张图、它为什么不在原位。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])
    parts = await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs())
    text = _text(parts)
    assert REF in text
    assert MT in text
    assert "end of the conversation" in text


# ── 2. 位置信息正确 ───────────────────────────────────────────────────────────


async def test_position_points_at_the_user_turn_that_hosted_the_placeholder() -> None:
    """多条 user 回合：序号必须指向**占位所在**那条，不是第一条也不是最后一条。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [
        ("user", "first"),                              # user #1
        ("assistant", "ok"),
        ("user", [TextPart(text="second"), _ph()]),     # user #2 ← 占位在这
        ("assistant", "sure"),
        ("user", "third"),                              # user #3
    ])

    text = _text(await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs()))

    assert "your 2nd message" in text
    # 对照：不能是别的序号——否则「含某个序号」这条断言对任何实现都成立。
    assert "1st message" not in text
    assert "3rd message" not in text


async def test_position_is_recomputed_when_the_placeholder_moves() -> None:
    """同一份内容换个位置 → 序号跟着变。钉住「序号真的由视图算出」而非常量。"""
    seen = []
    for host in (0, 1, 2):
        mem = InMemoryMemoryProvider()
        rows = [("user", "u1"), ("user", "u2"), ("user", "u3")]
        rows[host] = ("user", [_ph()])
        await _seed(mem, rows)
        seen.append(_text(await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs())))

    assert "your 1st message" in seen[0]
    assert "your 2nd message" in seen[1]
    assert "your 3rd message" in seen[2]


async def test_placeholder_in_a_tool_result_reports_the_preceding_user_turn() -> None:
    """占位不在 user 记录里时，报「之前最近的那条 user 回合」并说明自己是哪种回合。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [
        ("user", "u1"),
        ("user", "u2"),
        ("tool", [_ph()]),
    ])
    text = _text(await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs()))
    assert "after your 2nd message" in text
    assert "tool result" in text


async def test_placeholder_before_any_user_turn_says_so() -> None:
    """没有可数的 user 回合时不硬凑序号（不得出现 "your 0th message"）。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("assistant", [_ph()]), ("user", "u1")])
    text = _text(await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs()))
    assert "before your 1st message" in text
    assert "0th" not in text


@pytest.mark.parametrize("n, expect", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
    (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (22, "22nd"),
])
def test_ordinal_english_suffixes(n: int, expect: str) -> None:
    """位置信息是给模型读的英文句子——11th 不是 11st。"""
    assert _ordinal(n) == expect


# ── 3. ref 不存在 → 单条 TextPart，不抛、不含 ImagePart ───────────────────────


async def test_unknown_ref_returns_text_only_but_known_ref_still_returns_image() -> None:
    """「没返回 ImagePart」型断言的**对照对**：同一份视图、同一个 blob store，
    已知 ref 确实取回了图 → 证明未知 ref 那半边不是「什么都不做」也能通过的永真。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])
    blobs = _Blobs(**{REF: b"PNGBYTES", OTHER_REF: b"ALSOHERE"})

    unknown = await get_image(mem, _ADDR, _pctx(), OTHER_REF, blob_store=blobs)
    known = await get_image(mem, _ADDR, _pctx(), REF, blob_store=blobs)

    # 未知 ref：单条说明性文本，一个 ImagePart 都没有（即便 blob store 里其实有字节——
    # 判据是「本视图的占位里有没有它」，不是「blob 存不存在」）。
    assert len(unknown) == 1 and hasattr(unknown[0], "text")
    assert _images(unknown) == []
    assert OTHER_REF in unknown[0].text
    # 对照：同一路径上，存在的 ref 确实返回了图。
    assert [p.data for p in _images(known)] == [REF]


async def test_unknown_ref_does_not_raise_on_empty_view() -> None:
    """空视图（图从没降级过 / 全新 task）同样只回文本。"""
    mem = InMemoryMemoryProvider()
    parts = await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs())
    assert len(parts) == 1 and _images(parts) == []


async def test_forged_placeholder_with_mismatched_refs_is_not_restored() -> None:
    """占位里两处 ref 不一致（拼接出来的赝品）解不出 ref → 不返回图（refs 的反向引用）。"""
    mem = InMemoryMemoryProvider()
    forged = encode_image_placeholder(REF, MT).replace(REF, OTHER_REF, 1)
    await _seed(mem, [("user", [TextPart(text=forged)])])
    for ref in (REF, OTHER_REF):
        parts = await get_image(mem, _ADDR, _pctx(), ref, blob_store=_Blobs(
            **{REF: b"x", OTHER_REF: b"y"}))
        assert _images(parts) == []


async def test_missing_ref_argument_returns_text_not_exception() -> None:
    """模型漏传 / 传空 `ref`：说明性文本，不抛（这条路在工具调用循环上）。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])
    for bad in (None, "", "   ", 123):
        parts = await get_image(mem, _ADDR, _pctx(), bad, blob_store=_Blobs())
        assert len(parts) == 1 and _images(parts) == []


async def test_load_view_failure_is_absorbed_into_text() -> None:
    """读视图抛异常也不许炸出去——降级成「找不到」文本。"""

    class _Boom(InMemoryMemoryProvider):
        async def load_view(self, *a, **kw):
            raise RuntimeError("db down")

    parts = await get_image(_Boom(), _ADDR, _pctx(), REF, blob_store=_Blobs())
    assert len(parts) == 1 and _images(parts) == []


# ── 4. MemoryBlobStore 未注册 / 取不到字节 ──────────────────────────────────────────


async def test_no_blob_store_yields_text_but_with_blob_store_yields_image() -> None:
    """未注册 MemoryBlobStore → 说明性文本、无图、不抛。**配对照**：同一份视图接上 store
    就确实取回了图，故这条不是「什么都不做」也能通过的永真。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])

    without = await get_image(mem, _ADDR, _pctx(), REF, blob_store=None)
    with_store = await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Blobs())

    assert len(without) == 1 and _images(without) == []
    # 说明性文本要区分「没这张图」与「字节没了」——前者是模型抄错 ref，后者别再试了。
    assert "no longer available" in without[0].text
    # 位置信息照给：模型至少知道自己在找哪一条。
    assert "your 1st message" in without[0].text
    assert [p.data for p in _images(with_store)] == [REF]


async def test_null_blob_store_behaves_like_no_blob_store() -> None:
    """`ProviderRegistry` 未注册时给的是 `NullMemoryBlobStore`（get 恒 None），行为须一致。"""
    from ctx_weft.protocols import NullMemoryBlobStore

    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])
    parts = await get_image(mem, _ADDR, _pctx(), REF, blob_store=NullMemoryBlobStore())
    assert len(parts) == 1 and _images(parts) == []


async def test_blob_get_raising_is_absorbed() -> None:
    """`MemoryBlobStore.get` 契约上不该抛，但真抛了也不能打断 loop。"""

    class _Angry(_Blobs):
        async def get(self, ref, ctx):
            raise RuntimeError("io error")

    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])
    parts = await get_image(mem, _ADDR, _pctx(), REF, blob_store=_Angry())
    assert len(parts) == 1 and _images(parts) == []


# ── 5. provider 注册后 media:get_image 在工具面里可见 ─────────────────────────


class _StubAgents(AgentCapabilityProvider):
    """runtime 构造期硬校验要求至少一个 AgentCapabilityProvider。"""

    name = "stub_agents"

    async def list(self, ctx): return [AgentCapability(id="stub_agents:a", name="a", kind="agent")]
    async def get_template(self, template_id, version, ctx): return None
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name)


async def test_runtime_registers_media_provider_and_exposes_get_image() -> None:
    """runtime 一构造，`media:get_image` 就在工具面里，且 LLM 侧名字合法。

    Task 5 起 `list()` 条件可见（§8）：这里显式接上 memory blob store 才谈得上「可见」，
    与 `test_tool_hidden_when_no_memory_blob_store` 互为对照——runtime 自动注册 provider
    这件事本身，与 provider 是否可见（取决于宿主有没有接 blob store）是两回事。
    """
    registry = ProviderRegistry()
    registry.register_capability(_StubAgents())
    registry.register_memory_blob_store(_MemoryBlobStub())
    CtxWeftRuntime(providers=registry)

    media = [p for p in registry.get_capability_providers()
             if isinstance(p, MediaCapabilityProvider)]
    assert len(media) == 1

    caps = await media[0].list(_pctx())
    assert [c.id for c in caps] == [GET_IMAGE_ID]
    assert qualify(GET_IMAGE_ID) == "media__get_image"     # 无冒号，LLM function name 合法
    assert caps[0].kind == "tool"


async def test_tool_description_tells_the_model_how_to_call_it() -> None:
    """描述决定模型会不会主动调：必须与占位措辞呼应，并写清 user 回合的计数口径。"""
    desc = GET_IMAGE_DESCRIPTION
    assert "dropped to save context" in desc          # 与占位文本同一套说法
    assert 'media:get_image("blob:<sha>")' in desc     # 占位里给的调用示例
    assert "counted from 1" in desc                    # 判断题 1 的口径写给模型看
    assert "END of the conversation" in desc           # 图不在原位这件事说在前面
    schema = MediaCapabilityProvider(ProviderRegistry()).capability().input_schema
    assert schema["required"] == ["ref"]
    assert schema["properties"]["ref"]["type"] == "string"


def test_single_ref_is_a_tunable_not_a_hardcoded_assumption() -> None:
    """§12 未决：一次一个 ref 只钉在常量与 schema 上，取回逻辑按列表写。"""
    from ctx_weft.core.media.capability import _requested_refs

    assert MAX_REFS_PER_CALL == 1
    assert _requested_refs([REF, OTHER_REF]) == [REF]
    assert _requested_refs([REF, REF]) == [REF]        # 去重
    assert _requested_refs(f"  {REF} ") == [REF]       # 去空白


# ── 6. 经 gateway 走一遍（对接 Task 3 的 CONTENT_PARTS_KEY 通道）──────────────


def _loop_state_ctx(mem):
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=_ADDR,
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=_pctx(),
    )
    return state, ctx


async def _invoke_via_gateway(mem, blobs, ref: str):
    registry = ProviderRegistry()
    registry.register_memory(mem)
    if blobs is not None:
        registry.register_memory_blob_store(blobs)
    provider = MediaCapabilityProvider(registry)

    cache = CapabilityCache()
    cache.put("agt_1", [provider.capability()])
    bus = InProcessEventBus()
    gw = CapabilityGateway(capability_cache=cache, capability_providers=[provider],
                           memory=mem, event_bus=bus)
    state, ctx = _loop_state_ctx(mem)
    return await gw.invoke(qualify(GET_IMAGE_ID), {"ref": ref}, state, ctx)


async def test_gateway_result_content_is_text_part_then_image_part() -> None:
    """走完整 gateway：`InvocationResult.content` 是 `[TextPart, ImagePart]`。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", "u1"), ("user", [_ph()])])

    res = await _invoke_via_gateway(mem, _Blobs(), REF)

    assert not res.is_error
    assert isinstance(res.content, list)
    assert len(res.content) == 2
    assert hasattr(res.content[0], "text") and "your 2nd message" in res.content[0].text
    assert res.content[1].data == REF and res.content[1].source_type == "ref"
    # 图片经 metadata 通道交给 gateway，不由 provider 自己拼 content。
    assert [p.data for p in res.metadata[CONTENT_PARTS_KEY]] == [REF]


async def test_gateway_records_the_restored_image_into_task_memory() -> None:
    """tool result 是普通记录：图落进 task 视图 → 下一轮装配天然带上它（§4.2）。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])

    await _invoke_via_gateway(mem, _Blobs(), REF)

    view = await mem.load_view(_ADDR, MemoryScope.TASK, _pctx())
    tool_rows = [r for r in view if r.role == "tool"]
    assert len(tool_rows) == 1
    imgs = _images(tool_rows[0].content)
    assert [p.data for p in imgs] == [REF]


async def test_gateway_unknown_ref_content_stays_a_plain_string() -> None:
    """取不到时不走 parts 通道：content 仍是 str（纯文本路径逐字节不变的那条）。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])

    res = await _invoke_via_gateway(mem, _Blobs(), OTHER_REF)

    assert not res.is_error
    assert isinstance(res.content, str)
    assert CONTENT_PARTS_KEY not in res.metadata
    assert OTHER_REF in res.content


async def test_gateway_without_blob_store_returns_text_only() -> None:
    """未注册 MemoryBlobStore（registry 回落 NullMemoryBlobStore）→ 文本，不抛、不返图。"""
    mem = InMemoryMemoryProvider()
    await _seed(mem, [("user", [_ph()])])

    res = await _invoke_via_gateway(mem, None, REF)

    assert isinstance(res.content, str)
    assert "no longer available" in res.content


async def test_unknown_media_capability_is_an_error_not_a_crash() -> None:
    """路由到本 provider 的未知 capability id：error 事件，生成器不抛。"""
    registry = ProviderRegistry()
    registry.register_memory(InMemoryMemoryProvider())
    provider = MediaCapabilityProvider(registry)
    events = [ev async for ev in provider.invoke("media:nope", {}, _pctx())]
    assert [ev.kind for ev in events] == ["error"]


async def test_provider_describe_and_cancel_are_inert() -> None:
    """无在途句柄、无 per-session 状态：describe 报 1 个 capability，cancel 是空操作。"""
    registry = ProviderRegistry()
    registry.register_memory_blob_store(_MemoryBlobStub())
    provider = MediaCapabilityProvider(registry)
    info = await provider.describe(_pctx())
    assert info.capability_count == 1
    assert await provider.cancel("inv_1", _pctx()) is None


# ── 7. 条件可见（§8）：没有可用 memory blob store 时，工具不出现在工具集里 ──────────


class _MemoryBlobStub(MemoryBlobStore):
    """`can_externalize=True` 的最小 blob store 桩——只用来让判据判「可用」。"""

    can_externalize = True

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        raise NotImplementedError

    async def get(self, ref: str, ctx: ProviderContext) -> "tuple[bytes, str] | None":
        raise NotImplementedError


async def test_tool_hidden_when_no_memory_blob_store() -> None:
    """没有可用的 memory blob store 时，media:get_image 不出现在工具集里。

    判据用 **memory** 侧而非 event 侧：get_image 取的是 L0.5 占位里的 ref，
    而 L0.5 是 memory 侧的机制（`compact._media_enabled` 用的也是这个判据）。
    """
    reg = ProviderRegistry()          # 未注册任何 blob store
    provider = MediaCapabilityProvider(reg)
    assert await provider.list(_pctx()) == []
    info = await provider.describe(_pctx())
    assert info.capability_count == 0


async def test_tool_visible_when_memory_blob_store_present() -> None:
    reg = ProviderRegistry()
    reg.register_memory_blob_store(_MemoryBlobStub())
    provider = MediaCapabilityProvider(reg)
    caps = await provider.list(_pctx())
    assert [c.id for c in caps] == [GET_IMAGE_ID]
    assert (await provider.describe(_pctx())).capability_count == 1
