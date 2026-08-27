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
        self.ctx_tenant_ids: list[str] = []

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        self.put_calls += 1
        self.seen.append((data, media_type))
        self.ctx_session_ids.append(ctx.session_id)
        self.ctx_tenant_ids.append(ctx.tenant_id)
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


@pytest.mark.asyncio
async def test_runtime_injects_helper_that_delegates_to_the_shared_source():
    """HITL 用的薄包装底下必须仍是那三处共用的同一个方法，不是各写一遍的副本。

    Task A2 起回调收整个 `HitlRequest`（要拿 resume_llm_* 与 session_id），故注入的是
    `_normalize_hitl_content`；这条断言它**确实转调**共用真源，而不是自己重写一遍
    validate → normalize。
    """
    rt = _make_runtime(_VisionClient())
    injected = rt.hitl_manager._content_normalizer
    assert injected is not None
    assert injected.__func__ is CtxWeftRuntime._normalize_hitl_content

    seen: list[tuple] = []

    async def _spy(content, session_id, **kw):
        seen.append((content, session_id, kw))
        return content

    rt._validate_and_normalize_content = _spy  # type: ignore[method-assign]
    hid = await _pending(rt, session_id="ses-delegate")
    await rt.hitl_manager.answer(hid, "hi")

    assert len(seen) == 1, "薄包装必须转调共用真源（_validate_and_normalize_content）"
    assert seen[0][0] == "hi"
    assert seen[0][1] == "ses-delegate"


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


# ══ Task A2：补 Task A 的三条缺口 ══════════════════════════════════════════════
#
# 三条都是 controller 简报之误（已逐条核实）：
#   缺口 1 `resolve_approve` 未接 normalizer——approve 的备注同样是多模态，同一个洞还开着；
#   缺口 2 视觉门控解析的是**默认** client，不是本次应答真正要用的模型（正确性缺陷）；
#   缺口 3 blob 的 tenant 锚点写死 "default"，多租户宿主下落错 tenant。
# 修法：回调签名从 (content, session_id) 扩成 (content, req)，runtime 侧由 req 取
# resume_llm_* 与 session_id。


class _RoutingLLMProvider:
    """按 (account, model) 分派 client 的 stub provider，并记下每次解析的实参。

    缺口 2 必须用它才能真的红：只有「默认 client 与指定 client 能力不同」时，
    「门控判的是哪一个」才可观测。
    """

    def __init__(self, default, by_key: dict) -> None:
        self.default = default
        self.by_key = by_key
        self.calls: list[tuple] = []

    def get_client(self, account=None, model=None):
        self.calls.append((account, model))
        return self.by_key.get((account, model), self.default)


def _make_routing_runtime(provider: _RoutingLLMProvider, store: "BlobStore | None" = None):
    """注册按账号/模型分派的 provider——_resolve_llm 优先走 registry。"""
    rt = _make_runtime(_VisionClient(), store)
    rt.providers.register_llm_provider(provider)
    return rt


# ── A2-1/2：approve 路径同样接上（缺口 1）───────────────────────────────────────


@pytest.mark.asyncio
async def test_hitl_approve_rejects_malformed_base64_without_touching_blob_store():
    """`approve(message=...)` 与 answer/reject 同为多模态入口，不得漏校验。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await rt.hitl_manager.request(
        form="approval", session_id="ses-hitl", task_id="tsk-1", question="放行？",
    )

    with pytest.raises(InvalidContentError):
        await rt.hitl_manager.approve(
            hid, message=[TextPart(text="备注"), ImagePart(data=_MALFORMED, media_type="image/png")],
        )

    assert store.put_calls == 0, "被拒的内容不得在 blob store 留垃圾"
    req = rt.hitl_manager.get(hid)
    assert req is not None
    assert req.status == "pending", "校验失败不得把请求推进到终态"
    assert req.message == "", "校验失败不得把畸形内容写进 req.message"
    assert req.modified_arguments is None, "校验失败不得写 modified_arguments"


@pytest.mark.asyncio
async def test_hitl_approve_externalizes_valid_image_to_ref():
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await rt.hitl_manager.request(
        form="approval", session_id="ses-appr", task_id="tsk-1", question="放行？",
    )

    req = await rt.hitl_manager.approve(
        hid, message=[TextPart(text="放行，见图"), ImagePart(data=_PNG, media_type="image/png")],
    )

    assert req.status == "accepted"
    content = req.message
    # 陷阱守卫：`not hasattr(p, "text")` 在 str 上恒为 True。
    assert isinstance(content, list), f"应答内容必须仍是 parts 列表，实为 {type(content)!r}"
    images = [p for p in content if not hasattr(p, "text")]
    assert len(images) == 1
    assert images[0].source_type == "ref", "approve 备注里的图同样必须被外部化"
    assert images[0].data.startswith(BLOB_REF_PREFIX)
    assert images[0].data != _PNG
    assert store.put_calls == 1
    assert store.ctx_session_ids == ["ses-appr"]


# ── A2-3：门控判的是 req.resume_llm_* 指定的模型，不是默认 client（缺口 2）────


@pytest.mark.asyncio
async def test_vision_gate_uses_the_model_named_by_the_reply_not_the_default_client():
    """默认 client 支持视觉、本次应答指定的模型不支持 → 必须拒。

    这条是本任务的实质：修好前 runtime 只能 `_resolve_llm(None, None)`，会拿到
    **支持视觉的默认 client** 而放行——门控判的不是真正会收到这张图的那个模型。
    """
    store = _CountingStore()
    provider = _RoutingLLMProvider(
        default=_VisionClient(), by_key={("acct-text", "text-only"): _TextOnlyClient()},
    )
    rt = _make_routing_runtime(provider, store)
    hid = await _pending(rt)

    with pytest.raises(VisionNotSupportedError):
        await rt.hitl_manager.answer(
            hid, [ImagePart(data=_PNG, media_type="image/png")],
            llm_account="acct-text", llm_model="text-only",
        )

    assert ("acct-text", "text-only") in provider.calls, (
        "视觉门控必须按 req.resume_llm_account / resume_llm_model 解析 client"
    )
    assert store.put_calls == 0
    assert rt.hitl_manager.get(hid).status == "pending"


@pytest.mark.asyncio
async def test_vision_gate_allows_image_when_the_named_model_supports_vision():
    """反向：默认 client **不**支持视觉、本次应答指定的模型支持 → 必须放行。

    与上一条构成拒/放两侧的双向锁：只判默认 client 的实现会在这一条上错误拒绝。
    """
    store = _CountingStore()
    provider = _RoutingLLMProvider(
        default=_TextOnlyClient(), by_key={("acct-v", "vision-model"): _VisionClient()},
    )
    rt = _make_routing_runtime(provider, store)
    hid = await _pending(rt, session_id="ses-named-vision")

    req = await rt.hitl_manager.answer(
        hid, [ImagePart(data=_PNG, media_type="image/png")],
        llm_account="acct-v", llm_model="vision-model",
    )

    assert req.status == "accepted"
    assert ("acct-v", "vision-model") in provider.calls
    assert store.put_calls == 1
    content = req.message
    assert isinstance(content, list), f"应答内容必须仍是 parts 列表，实为 {type(content)!r}"
    assert content[0].source_type == "ref"


@pytest.mark.asyncio
async def test_approve_vision_gate_also_uses_the_named_model():
    """approve 路径同样按 req.resume_llm_* 解析（缺口 1 与缺口 2 的交叉点）。"""
    store = _CountingStore()
    provider = _RoutingLLMProvider(
        default=_VisionClient(), by_key={("acct-text", "text-only"): _TextOnlyClient()},
    )
    rt = _make_routing_runtime(provider, store)
    hid = await rt.hitl_manager.request(
        form="approval", session_id="ses-hitl", task_id="tsk-1", question="放行？",
    )

    with pytest.raises(VisionNotSupportedError):
        await rt.hitl_manager.approve(
            hid, message=[ImagePart(data=_PNG, media_type="image/png")],
            llm_account="acct-text", llm_model="text-only",
        )

    assert store.put_calls == 0
    assert rt.hitl_manager.get(hid).status == "pending"


# ── A2-4：resume_llm_* 均为 None → 回落默认 client（既有行为不变）─────────────


@pytest.mark.asyncio
async def test_falls_back_to_default_client_when_reply_names_no_model():
    """未指定模型（host 没传 llm_account/llm_model）时必须仍解默认 client。"""
    store = _CountingStore()
    provider = _RoutingLLMProvider(
        default=_TextOnlyClient(), by_key={("acct-v", "vision-model"): _VisionClient()},
    )
    rt = _make_routing_runtime(provider, store)
    hid = await _pending(rt)

    # 默认 client 不支持视觉 → 递图被拒，正说明解的是默认 client。
    with pytest.raises(VisionNotSupportedError):
        await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert provider.calls == [(None, None)], (
        "未指定模型时必须以 (None, None) 解析——即 _resolve_llm 的既有默认回落"
    )
    assert store.put_calls == 0


# ── A2-5：blob 落在正确的 tenant 锚点（缺口 3）────────────────────────────────


def _install_live_owner(rt, session_id: str, tenant_id: str):
    """给 session 装一个活 owner TaskManager（热应答的主路径就是从它读 tenant）。"""
    from ctx_weft.core.orchestrator.task_manager import TaskManager
    from ctx_weft.core.state.models import Session

    tm = TaskManager(session_id=session_id, event_bus=rt.event_bus)
    tm.set_session(Session(
        id=session_id, user_prompt="", status="RUNNING", tenant_id=tenant_id,
    ))
    rt._task_managers[session_id] = tm
    return tm


@pytest.mark.asyncio
async def test_blob_anchors_to_the_session_tenant_via_live_owner():
    """热路径：活 owner TM 持有 Session，tenant 必须从它身上来，而非写死 default。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    _install_live_owner(rt, "ses-tenant-hot", "tenant-alpha")
    hid = await _pending(rt, session_id="ses-tenant-hot")

    await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert store.put_calls == 1
    assert store.ctx_tenant_ids == ["tenant-alpha"], (
        "blob 必须落在该 session 所属 tenant 的锚点上"
    )
    assert store.ctx_session_ids == ["ses-tenant-hot"]


@pytest.mark.asyncio
async def test_blob_anchors_to_the_session_tenant_via_event_log_on_cold_reply():
    """冷路径：进程重启后无活 TM，tenant 由事件日志解出（每条 Event 都带 tenant_id）。"""
    from ctx_weft.core.events.types import Event
    from ctx_weft.core.utils import generate_id, now_utc

    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    await rt.event_store.append(Event(
        id=generate_id("evt"), run_id=None, sequence=0, session_id="ses-tenant-cold",
        type="SessionCreated", timestamp=now_utc(), tenant_id="tenant-beta",
    ))
    hid = await _pending(rt, session_id="ses-tenant-cold")

    await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert store.put_calls == 1
    assert store.ctx_tenant_ids == ["tenant-beta"]


# ── A2-6：解不出 session 时回落 default 且不抛 ───────────────────────────────


@pytest.mark.asyncio
async def test_unknown_session_falls_back_to_default_tenant_without_raising():
    """既无活 TM 也无事件（陌生 session）→ 回落 default，**不得抛**。

    HITL 应答路径上抛错会卡住人类应答，故这里刻意保持现状而不是报错。
    """
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    hid = await _pending(rt, session_id="ses-nowhere")

    req = await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert req.status == "accepted"
    assert store.ctx_tenant_ids == ["default"]


@pytest.mark.asyncio
async def test_event_store_failure_falls_back_to_default_tenant_without_raising():
    """事件存储不可用（宿主 DB 抖动）时同样只能回落，不得把异常甩给应答方。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)

    async def _boom(session_id):
        raise RuntimeError("event store down")

    rt.event_store.read_by_session = _boom  # type: ignore[method-assign]
    hid = await _pending(rt, session_id="ses-broken-store")

    req = await rt.hitl_manager.answer(hid, [ImagePart(data=_PNG, media_type="image/png")])

    assert req.status == "accepted"
    assert store.ctx_tenant_ids == ["default"]


@pytest.mark.asyncio
async def test_plain_text_reply_never_touches_the_event_store():
    """纯文本应答不写 blob，就不该为解 tenant 去读事件日志（读了也白读）。"""
    store = _CountingStore()
    rt = _make_runtime(_VisionClient(), store)
    reads: list[str] = []
    _orig = rt.event_store.read_by_session

    async def _counting(session_id):
        reads.append(session_id)
        return await _orig(session_id)

    rt.event_store.read_by_session = _counting  # type: ignore[method-assign]
    hid = await _pending(rt)

    req = await rt.hitl_manager.answer(hid, "纯文本")

    assert req.message == "纯文本"
    assert reads == [], "纯文本应答不得为了解 tenant 去读事件日志"
