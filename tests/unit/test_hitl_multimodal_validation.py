"""HITL 入口的内容校验与外部化（多模态 Phase 3c Task A）。

背景：`validate_content` / `normalize_content` 此前只接了 `run_single_task` 与
`start_session` 两个入口，而 HITL 应答接口在 Phase 1/2 就已放宽成多模态。人类经
HITL 递进来的图**全程不校验、不外部化**——无格式校验（叠加 `b64decode` 默认
`validate=False` 不抛、静默解出垃圾字节，后果是**静默损坏**）、无视觉门控、图以
inline base64 永久留在 memory 里。

接线点是 `resolve_answer` / `resolve_reject`（`answer` / `reject` 的共同下层，
也覆盖冷应答路径），经 `set_content_normalizer` 注入 runtime 的
`_validate_and_normalize_content`。

两条硬约束在下面被显式锁死：
1. **未注入 normalizer 时行为逐字节不变**（大量既有单测直接构造 `HitlManager()`）。
2. **顺序恒为 validate → normalize**——被拒内容不得在 blob store 留垃圾，因此
   「没有写过 blob」这件事必须用计数器 stub 真的断言，而不是靠「没报错」推断。
"""

import base64
import hashlib
from types import SimpleNamespace

import pytest

from ctx_weft.core.errors import InvalidContentError, VisionNotSupportedError
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.protocols import (
    BLOB_REF_PREFIX,
    BlobStore,
    ImagePart,
    ProviderContext,
    TextPart,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 100
_PNG = base64.b64encode(_PNG_BYTES).decode()
# 关键：`base64.b64decode(..., validate=False)`（默认）对它**不抛**，静默解出垃圾字节。
# 必须靠 validate_content 的 validate=True 拦住。
_MALFORMED = "!!!这不是合法的 base64!!!"


class _CountingStore(BlobStore):
    """能真正外部化的 stub store + put 计数器（内容寻址，ref 形态与真实实现一致）。"""

    def __init__(self) -> None:
        self.put_calls = 0
        self.seen: list[tuple[bytes, str]] = []
        self.ctx_session_ids: list[str] = []

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        self.put_calls += 1
        self.seen.append((data, media_type))
        self.ctx_session_ids.append(ctx.session_id)
        return f"{BLOB_REF_PREFIX}{hashlib.sha256(data).hexdigest()}"

    async def get(self, ref: str, ctx: ProviderContext) -> "tuple[bytes, str] | None":
        for data, media_type in self.seen:
            if ref == f"{BLOB_REF_PREFIX}{hashlib.sha256(data).hexdigest()}":
                return data, media_type
        return None


class _VisionClient:
    supports_vision = True
    context_limit = 128_000
    output_reserve = 8_192


class _TextOnlyClient:
    supports_vision = False
    context_limit = 128_000
    output_reserve = 8_192


def _make_runtime(llm, store: "BlobStore | None" = None) -> CtxWeftRuntime:
    """构造一个最小 runtime（CtxWeftRuntime 硬要求至少一个 AgentCapabilityProvider）。"""
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    rt = make_runtime(agent_provider=templates, llm=llm)
    if store is not None:
        rt.providers.register_blob_store(store)
    return rt


async def _pending(rt: CtxWeftRuntime, session_id: str = "ses-hitl") -> str:
    return await rt.hitl_manager.request(
        form="wait", session_id=session_id, task_id="tsk-1", question="?",
    )


# ── 1. 畸形 base64 → 被拒，且 blob store 未被写入 ──────────────────────────────


@pytest.mark.asyncio
async def test_hitl_answer_rejects_malformed_base64_without_touching_blob_store():
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt)

    with pytest.raises(InvalidContentError):
        await rt.hitl_manager.answer(
            hid, [TextPart(text="看这张"), ImagePart(data=_MALFORMED, media_type="image/png")],
        )

    assert store.put_calls == 0, (
        "顺序必须是 validate → normalize：被拒的内容不得在 blob store 留下垃圾"
    )
    req = rt.hitl_manager.get(hid)
    assert req is not None
    assert req.status == "pending", "校验失败不得把请求推进到终态"
    assert req.message == "", "校验失败不得把畸形内容写进 req.message"


@pytest.mark.asyncio
async def test_hitl_reject_rejects_malformed_base64_without_touching_blob_store():
    """reject 路径与 answer 同一下层，同样不得漏校验。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt)

    with pytest.raises(InvalidContentError):
        await rt.hitl_manager.reject(
            hid, message=[ImagePart(data=_MALFORMED, media_type="image/png")],
        )

    assert store.put_calls == 0
    assert rt.hitl_manager.get(hid).status == "pending"


# ── 2. 视觉门控在 HITL 路径同样生效 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_hitl_answer_rejects_image_for_non_vision_model():
    store = _CountingStore()
    rt = _make_runtime(_TextOnlyClient(), store)
    hid = await _pending(rt)

    with pytest.raises(VisionNotSupportedError):
        await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert store.put_calls == 0
    assert rt.hitl_manager.get(hid).status == "pending"


@pytest.mark.asyncio
async def test_hitl_answer_rejects_disallowed_media_type():
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt)

    with pytest.raises(InvalidContentError):
        await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/bmp")])

    assert store.put_calls == 0


# ── 3. 合法图 + 真 blob store → 落进 req.message 的是 ref ─────────────────────


@pytest.mark.asyncio
async def test_hitl_answer_externalizes_valid_image_to_ref():
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt, session_id="ses-blob")

    req = await rt.hitl_manager.answer(
        hid, [TextPart(text="看这张"), ImagePart(data=_PNG, media_type="image/png")],
    )

    assert req.status == "accepted"
    content = req.message
    # 陷阱守卫：`not hasattr(p, "text")` 在 str 上恒为 True，下面的断言才不是重言式。
    assert isinstance(content, list), f"应答内容必须仍是 parts 列表，实为 {type(content)!r}"
    images = [p for p in content if not hasattr(p, "text")]
    assert len(images) == 1
    assert images[0].source_type == "ref", "HITL 递进来的图必须被外部化成 ref"
    assert images[0].data.startswith(BLOB_REF_PREFIX)
    assert images[0].data != _PNG
    assert store.put_calls == 1
    assert store.seen[0] == (_PNG_BYTES, "image/png"), "写进 blob 的必须是解码后的原始字节"
    assert store.ctx_session_ids == ["ses-blob"], "外部化必须锚定该 HITL 请求所属 session"
    assert content[0].text == "看这张", "文本 part 原样保留"
    # 存下来的 ref 必须能解回原始字节（wire 上仍是可解码的 base64 之来源）。
    assert await store.get(images[0].data, ProviderContext(session_id="ses-blob")) == (
        _PNG_BYTES, "image/png",
    )


# ── 4. 未注入 normalizer 时行为逐字节不变 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_bare_hitl_manager_is_identity_without_normalizer():
    """裸 `HitlManager()`（大量既有单测的构造方式）必须是恒等变换。

    刻意递畸形 base64 + 白名单外 media_type：未注入时**一律放行、原样保留**，
    连对象身份都不变。若接线漏了 None 守卫，这条会以 TypeError 转红。
    """
    hm = HitlManager()
    hid = await hm.request(form="wait", session_id="s", task_id="t")
    payload = [TextPart(text="hi"), ImagePart(data=_MALFORMED, media_type="image/bmp")]

    req = await hm.answer(hid, payload)

    assert req.status == "accepted"
    assert req.message is payload, "未注入 normalizer 时必须是恒等变换（同一对象）"


@pytest.mark.asyncio
async def test_bare_hitl_manager_reject_is_identity_without_normalizer():
    hm = HitlManager()
    hid = await hm.request(form="approval", session_id="s", task_id="t")
    payload = [ImagePart(data=_MALFORMED, media_type="image/bmp")]

    req = await hm.reject(hid, message=payload)

    assert req.status == "rejected"
    assert req.message is payload


# ── 5. 纯文本应答逐字节不变（即使已注入 normalizer）─────────────────────────


@pytest.mark.asyncio
async def test_plain_text_hitl_answer_is_byte_identical():
    store = _CountingStore()
    calls: list[int] = []
    rt = _make_runtime(SimpleNamespace(supports_vision=True), store)
    _orig = rt._resolve_llm

    def _counting_resolve(*a, **k):
        calls.append(1)
        return _orig(*a, **k)

    rt._resolve_llm = _counting_resolve  # type: ignore[method-assign]
    hid = await _pending(rt)

    text = "就是一句纯文本"
    req = await rt.hitl_manager.answer(hid, text)

    assert req.message is text, "纯文本必须原样返回同一对象"
    assert store.put_calls == 0, "纯文本不得触碰 blob store"
    assert calls == [], "纯文本不得触发 LLM 解析（视觉门控只对格式合法的图片才需要）"


@pytest.mark.asyncio
async def test_empty_message_reject_is_byte_identical():
    """reject 的默认 message 为空串——最常见的既有调用形态，不得因接线而改变。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt)

    req = await rt.hitl_manager.reject(hid)

    assert req.status == "rejected"
    assert req.message == ""
    assert store.put_calls == 0


# ── 6. 三个调用点共用同一真源，行为一致 ──────────────────────────────────────


def test_runtime_injects_its_own_helper_into_hitl_manager():
    """HITL 用的必须就是那三处共用的同一个方法，不是各写一遍的副本。"""
    rt = _make_runtime(_VisionClient())
    injected = rt.hitl_manager._content_normalizer
    assert injected is not None
    assert injected.__func__ is CtxWeftRuntime._validate_and_normalize_content


@pytest.mark.asyncio
async def test_all_three_entry_points_reject_malformed_base64_before_any_put():
    """run_single_task / start_session / HITL 应答——三处顺序与判据必须一致。"""
    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    bad = [TextPart(text="看"), ImagePart(data=_MALFORMED, media_type="image/png")]

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    store = _CountingStore()
    rt = make_runtime(agent_provider=templates, llm=_VisionClient())
    rt.providers.register_blob_store(store)
    memory = InMemoryMemoryProvider()
    rt.providers.register_memory(memory)

    with pytest.raises(InvalidContentError):
        await rt.run_single_task(template_id="agent:tpl_echo", user_prompt=bad)
    assert store.put_calls == 0
    assert memory._events == [], "入口即拒、不落库"

    with pytest.raises(InvalidContentError):
        await rt.start_session(SessionStartParams.create(
            template_id="agent:tpl_echo", user_prompt=bad, context_limit=128_000,
        ))
    assert store.put_calls == 0
    assert memory._events == []

    hid = await _pending(rt)
    with pytest.raises(InvalidContentError):
        await rt.hitl_manager.answer(hid, bad)
    assert store.put_calls == 0
