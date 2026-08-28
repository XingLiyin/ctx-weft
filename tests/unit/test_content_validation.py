import base64

import pytest

from ctx_weft.core.content import content_has_image, validate_content
from ctx_weft.core.errors import InvalidContentError, VisionNotSupportedError
from ctx_weft.protocols import ImagePart, TextPart

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 100).decode()


class _VisionClient:
    supports_vision = True


class _TextOnlyClient:
    supports_vision = False


class _LegacyClient:
    """未声明该属性——严格默认下应视为无视觉能力。"""


# ── 纯文本：零影响 ────────────────────────────────────────────────────────

def test_plain_str_always_passes():
    validate_content("hello")                      # 不抛
    validate_content("hello", llm=_TextOnlyClient())  # 纯文本不受门控约束


def test_none_and_empty_pass():
    validate_content(None)
    validate_content("")
    validate_content([])


def test_text_parts_only_pass_without_vision():
    validate_content([TextPart(text="a"), TextPart(text="b")], llm=_TextOnlyClient())


# ── 格式校验 ─────────────────────────────────────────────────────────────

def test_valid_image_passes():
    validate_content([ImagePart(data=_PNG, media_type="image/png")])


def test_unknown_media_type_rejected():
    with pytest.raises(InvalidContentError) as ei:
        validate_content([ImagePart(data=_PNG, media_type="image/tiff")])
    assert "image/tiff" in str(ei.value)


def test_malformed_base64_rejected():
    with pytest.raises(InvalidContentError):
        validate_content([ImagePart(data="not!valid!base64!", media_type="image/png")])


def test_oversized_image_rejected():
    big = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
    with pytest.raises(InvalidContentError) as ei:
        validate_content([ImagePart(data=big, media_type="image/png")])
    assert "5" in str(ei.value), "错误文案应报出上限，便于宿主自查"


# ── M8：media_type 归一（大小写/参数/image-jpg 别名）──────────────────────────

def test_media_type_uppercase_accepted():
    validate_content([ImagePart(data=_PNG, media_type="IMAGE/PNG")])


def test_media_type_with_params_accepted():
    validate_content([ImagePart(data=_PNG, media_type="image/jpeg; charset=binary")])


def test_media_type_jpg_alias_accepted():
    validate_content([ImagePart(data=_PNG, media_type="image/jpg")])


def test_media_type_still_rejects_unsupported_after_normalization():
    with pytest.raises(InvalidContentError):
        validate_content([ImagePart(data=_PNG, media_type="IMAGE/TIFF")])


# ── M5：非 base64 source_type 应 raise，不再静默 continue 跳过校验 ─────────────

def test_non_base64_source_type_raises_not_silently_skipped():
    """Phase 3a 恒 base64（constraint 2），本分支今日不可达；但原 `continue` 会让
    Phase 3b 引入 ref/url 形态后静默跳过尺寸/内容校验。改成 raise，强制显式处理。"""
    with pytest.raises(InvalidContentError):
        validate_content([ImagePart(data=_PNG, media_type="image/png", source_type="url")])


# ── 视觉门控 ─────────────────────────────────────────────────────────────

def test_image_rejected_when_model_lacks_vision():
    with pytest.raises(VisionNotSupportedError):
        validate_content([ImagePart(data=_PNG, media_type="image/png")],
                         llm=_TextOnlyClient())


def test_image_rejected_when_client_does_not_declare():
    """严格默认：未声明 supports_vision 的 client 一律拒绝图片。"""
    with pytest.raises(VisionNotSupportedError):
        validate_content([ImagePart(data=_PNG, media_type="image/png")],
                         llm=_LegacyClient())


def test_image_passes_with_vision_client():
    validate_content([ImagePart(data=_PNG, media_type="image/png")], llm=_VisionClient())


def test_no_llm_means_no_gating():
    """不传 llm 时只做格式校验——供拿不到 client 的调用点使用。"""
    validate_content([ImagePart(data=_PNG, media_type="image/png")])


# ── content_has_image：门控开关本身的直测 ───────────────────────────────────
#
# Task 3 复审指出该函数没有任何直测，全靠 validate_content / start_session 的
# 测试间接覆盖。它是门控是否触发的开关——判据错了，视觉门控会静默失效（图片
# 直接放行、不报错）。这里逐类型直接断言其返回值。


def test_content_has_image_str_is_false():
    assert content_has_image("hello") is False


def test_content_has_image_none_is_false():
    assert content_has_image(None) is False


def test_content_has_image_empty_list_is_false():
    assert content_has_image([]) is False


def test_content_has_image_all_text_parts_is_false():
    assert content_has_image([TextPart(text="a"), TextPart(text="b")]) is False


def test_content_has_image_with_image_part_is_true():
    assert content_has_image([TextPart(text="看"), ImagePart(data=_PNG, media_type="image/png")]) is True


# ── 真实入口：入口即拒、不落库 ───────────────────────────────────────────────
#
# 只测 validate_content 本身证明不了它接对了位置——下面这条测试驱动真实的
# runtime.run_single_task() 入口，断言：(1) 抛 VisionNotSupportedError，
# (2) memory 中无任何记录（instantiate_agent / Session / Task 均未落库）。


@pytest.mark.asyncio
async def test_run_single_task_rejects_image_before_persisting_anything():
    from types import SimpleNamespace

    from ctx_weft.providers.llm.mock import MockLLMAdapter
    from ctx_weft.providers.llm.provider import _FixedModelClient
    from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    runtime = make_runtime(agent_provider=templates)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    # supports_vision 未传 → _FixedModelClient 默认 False（严格默认）。
    runtime.providers.register_llm_provider(
        SimpleNamespace(
            get_client=lambda account=None, model=None: _FixedModelClient(
                MockLLMAdapter(responses=[]), model or "mock-model", 128_000, 8_192,
                account=account or "acct-main",
            ),
        )
    )

    with pytest.raises(VisionNotSupportedError):
        await runtime.run_single_task(
            template_id="agent:tpl_echo",
            user_prompt=[TextPart(text="看这张图"), ImagePart(data=_PNG, media_type="image/png")],
        )

    assert memory._events == [], (
        "校验必须在任何持久化动作之前拒绝——memory 中不应有任何记录"
    )


# ── 纯文本行为逐字节不变：start_session 不得因新增校验而提前解析 LLM ────────────
#
# 改动前 start_session 从不调用 _resolve_llm——LLM 解析完全推迟到任务真正执行时
# （_make_task_runner → _SessionTaskRunner）才异步发生。若纯文本路径也提前解析，
# "解析不出 LLM" 这件事会从"任务执行时才失败"变成"start_session 里同步失败"，
# 这是本 Phase 明令禁止的行为变化（纯文本逐字节不变）。这条测试锁死：没有注册
# 任何 LLM provider、也没有 llm= fallback 时，纯文本 start_session 仍应正常返回
# RunHandle，而不是同步抛 RuntimeError("No LLM available...")。


@pytest.mark.asyncio
async def test_start_session_plain_text_does_not_eagerly_resolve_llm():
    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    # 故意不传 llm=、不 register_llm_provider——_resolve_llm 此刻必然抛
    # RuntimeError("No LLM available...")。纯文本 start_session 不该触发它。
    runtime = make_runtime(agent_provider=templates)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt="纯文本，没有图片",
            context_limit=100_000,
        )
    )
    assert handle is not None, (
        "纯文本 start_session 应像改动前一样正常返回 RunHandle——"
        "LLM 解析失败应推迟到任务执行时才发生，不应被新增校验提前触发"
    )


@pytest.mark.asyncio
async def test_start_session_plain_text_resolver_never_called():
    """上一条测试断言"整体不抛"；这条用计数器 stub 直接锁死 resolver 调用次数
    为 0——比"不抛异常"更强的证据，直接证明惰性解析确实惰性。"""
    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    runtime = make_runtime(agent_provider=templates)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    calls = []
    original_resolve = runtime._resolve_llm

    def _counting_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return original_resolve(*args, **kwargs)

    runtime._resolve_llm = _counting_resolve

    await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt="纯文本，没有图片",
            context_limit=100_000,
        )
    )
    assert calls == [], (
        "纯文本 start_session 不应触发 _resolve_llm——validate_content 的 "
        "llm_resolver 必须惰性，格式校验早返回时 resolver 从不被调用"
    )


@pytest.mark.asyncio
async def test_start_session_dict_text_does_not_eagerly_resolve_llm():
    """缺陷 A 的端到端回归：dict 形态纯文本（{"type":"text","text":...}）经
    content_has_image 误判为「含图」，但改用 llm_resolver 惰性解析后，
    格式校验会先于门控/resolver 调用抛出 InvalidContentError——不再是
    RuntimeError("No LLM available...")，resolver 也从未被调用。"""
    from ctx_weft.core.errors import InvalidContentError
    from ctx_weft.core.runtime import SessionStartParams
    from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider,
        make_echo_template,
        make_runtime,
    )

    templates = InlineAgentTemplateProvider()
    templates.register(make_echo_template())
    # 故意不注册任何 LLM provider——_resolve_llm 若被调用必然抛
    # RuntimeError("No LLM available...")。这条测试要证明它压根没被调用到。
    runtime = make_runtime(agent_provider=templates)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    calls = []
    original_resolve = runtime._resolve_llm

    def _counting_resolve(*args, **kwargs):
        calls.append((args, kwargs))
        return original_resolve(*args, **kwargs)

    runtime._resolve_llm = _counting_resolve

    with pytest.raises(InvalidContentError):
        await runtime.start_session(
            SessionStartParams.create(
                template_id="agent:tpl_echo",
                user_prompt=[{"type": "text", "text": "hello"}],
                context_limit=100_000,
            )
        )
    assert calls == [], (
        "dict 形态纯文本应在格式校验阶段就被拒——resolver 不该被调用，"
        "更不该抛出掩盖了真正问题的 RuntimeError"
    )


# ── I3（评审 2026-08-24 fix wave，选项 B）：dict 形态 part 入口硬拒绝 ──────────
#
# _is_text_part 用 `hasattr(part, "text")` 判据（spec §13 冻结，Phase 3a 不得更改）。
# dict 永远不满足 hasattr，故 dict 形态的 part——即便是纯文本 dict
# {"type":"text","text":...}——也会被误判成"非文本"（图片）。评审给出两个修法选项：
# (A) 让归一层认识 Mapping；(B) 保持现状但钉住这个限制、更新 spec。这里选 (B)：
# _is_text_part 与 utils.content_to_text / image_part_count 共享同一判据字面量
# （content.py 顶部 docstring 明确要求两者一致），content_to_text 还被
# test_content_module.py::test_content_to_text_reexported 钉死为 utils 的同一个
# 对象——只在 content.py 本地扩展 Mapping 支持会让这条"一致性"断言出现分叉：
# validate_content/content_has_image 认得 dict-text，utils.content_to_text /
# image_part_count 仍不认得，两条路径对同一份 dict 内容产出不同判断。这个新分叉
# 比现状的"两处对称地不认识 dict"更难追踪，故选 (B)：不改判据本身，只把限制钉死
# 成测试 + 文档（spec §13），要求宿主在 ingest 前把 dict 形态 rehydrate 成
# ContentPart 对象。
#
# 下面两条测试锁死这个已知限制的具体表现——若将来实现选项 (A)（连带把 utils.py
# 一并改成 Mapping-aware），这两条测试需要同步更新为新的、正确的行为。


def test_dict_text_part_is_misclassified_as_image_known_limitation():
    """纯文本 dict part 被 content_has_image 误判为「含图」——已知限制（选项 B），
    非本 Phase 修复范围。终审 2026-08-25（缺陷 A）之后 content_has_image 已不再
    是 start_session 决定是否提前解析 LLM 的判据（改用 validate_content 的
    llm_resolver 惰性解析），所以这个误判不再连带让 start_session 提前解析 LLM——
    见 test_start_session_dict_text_does_not_eagerly_resolve_llm。"""
    assert content_has_image([{"type": "text", "text": "hello"}]) is True


def test_dict_text_part_rejected_by_validate_content_known_limitation():
    """纯文本 dict part 被 validate_content 当成「无媒体类型的图片」硬拒绝——
    已知限制（选项 B）。宿主必须在 ingest 前把 dict 形态 rehydrate 成
    ContentPart 对象（TextPart/ImagePart），不能直接喂 dict-shaped part 进入口。"""
    with pytest.raises(InvalidContentError) as ei:
        validate_content([{"type": "text", "text": "hello"}])
    assert "''" in str(ei.value)  # media_type 取不到值（dict 无 .media_type 属性），报出空字符串类型


# ── 终审 2026-08-25：缺陷 A（dict 纯文本误触发提前解析 LLM）与
#    缺陷 B（validate_content 先门控后格式校验，掩盖真正问题）的回归测试 ──────


def test_dict_text_part_raises_invalid_content_not_runtime_error_via_resolver():
    """缺陷 A 的核心断言：即便 llm_resolver 会抛（模拟"无 LLM 可用"），
    dict 形态纯文本也必须先在格式校验阶段被拒（InvalidContentError），
    resolver 根本不该被调用到——不是 RuntimeError、也不是 VisionNotSupportedError。"""
    def _resolver_must_not_be_called():
        raise RuntimeError("No LLM available (should never be reached)")

    with pytest.raises(InvalidContentError):
        validate_content(
            [{"type": "text", "text": "hello"}],
            llm_resolver=_resolver_must_not_be_called,
        )


def test_malformed_image_rejected_before_vision_gating():
    """缺陷 B 的核心断言：格式畸形的图片 + 不支持视觉的模型 → 报的必须是
    InvalidContentError（格式问题），不能被门控抢先拦成 VisionNotSupportedError
    （那会掩盖真正的问题：内容本身就是畸形的）。"""
    with pytest.raises(InvalidContentError):
        validate_content(
            [ImagePart(data=_PNG, media_type="image/tiff")],
            llm=_TextOnlyClient(),
        )


def test_valid_image_still_gated_by_vision_capability():
    """门控没有被削弱：格式合法的图片遇上纯文本模型，仍然报
    VisionNotSupportedError（而不是被格式校验的重排意外放行）。"""
    with pytest.raises(VisionNotSupportedError):
        validate_content(
            [ImagePart(data=_PNG, media_type="image/png")],
            llm=_TextOnlyClient(),
        )


def test_llm_resolver_not_called_for_plain_text():
    """llm_resolver 是惰性的：纯文本（无图）路径下，即便传了 resolver，
    也绝不该被调用——resolver 抛异常时纯文本校验仍必须正常通过。"""
    calls = []

    def _resolver():
        calls.append(1)
        raise AssertionError("resolver 不该被调用")

    validate_content("纯文本", llm_resolver=_resolver)
    validate_content([TextPart(text="纯文本")], llm_resolver=_resolver)
    assert calls == []


# ── event blob 门控（Task 4）：第三道门控，放在视觉门控之后 ────────────────────

async def test_image_requires_event_blob_store() -> None:
    """携图会话未注册 EventBlobStore → 入口即拒（spec §7）。

    口径统一：事件库恒不含字节、恒可回读，没有例外分支。
    """
    from ctx_weft.core.errors import BlobStoreRequiredError
    from ctx_weft.protocols.events import NullEventBlobStore

    with pytest.raises(BlobStoreRequiredError):
        validate_content(
            [ImagePart(data=_PNG, media_type="image/png")],
            llm=_VisionClient(),
            event_blob_store=NullEventBlobStore(),
        )


def test_plain_text_unaffected_by_event_blob_gate() -> None:
    """纯文本在门控之前就已返回——这条不变量不可破。"""
    from ctx_weft.protocols.events import NullEventBlobStore

    validate_content("纯文本", event_blob_store=NullEventBlobStore())
    validate_content(None, event_blob_store=NullEventBlobStore())


def test_gate_order_format_before_vision_before_blob() -> None:
    """三道门控的顺序：格式 → 视觉 → blob。

    畸形内容必须报 InvalidContentError，不能被后两道抢先——那会掩盖真正的问题。
    """
    from ctx_weft.core.errors import InvalidContentError
    from ctx_weft.protocols.events import NullEventBlobStore

    with pytest.raises(InvalidContentError):
        validate_content(
            [ImagePart(data="!!!not-base64!!!", media_type="image/png")],
            llm=_VisionClient(), event_blob_store=NullEventBlobStore(),
        )


def test_llm_resolver_called_lazily_only_for_valid_images():
    """惰性解析的正面用例：只有格式合法的图片才会真的触发 resolver 调用，
    并且门控结果反映 resolver 返回的 client。"""
    calls = []

    def _resolver():
        calls.append(1)
        return _TextOnlyClient()

    with pytest.raises(VisionNotSupportedError):
        validate_content(
            [ImagePart(data=_PNG, media_type="image/png")],
            llm_resolver=_resolver,
        )
    assert calls == [1], "格式合法的图片应当触发恰好一次 resolver 调用"
