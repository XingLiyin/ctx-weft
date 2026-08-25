import base64

import pytest

from ctx_weft.core.content import validate_content
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
