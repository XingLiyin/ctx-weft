# 多模态 Phase 3a：防 400 护栏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 堵住三个会让 provider 返回 400 的洞——空白文本块、图片打到无视觉能力的模型、畸形/超大图片入口无校验。

**Architecture:** 三件互相独立的小改动。(1) 两家 adapter 的空文本跳过条件从 `if text:` 收紧为 `if text.strip():`；(2) `ModelConfig` 新增 `supports_vision`（**默认 `False`**，严格），经 `_FixedModelClient` 暴露为 duck-typed 属性，core 用 `getattr(llm, "supports_vision", False)` 读取；(3) `core/content.py` 新增 `validate_content()`，在四个 agent-loop 入口处校验 `media_type` 白名单、base64 合法性、单图字节上限，并在拿得到 llm client 时执行视觉门控。

**Tech Stack:** Python 3.11+，pytest + pytest-asyncio，`uv run pytest`。

**Spec:** `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§6.1 入口校验、§6.7 能力门控、§13 未决项前两条）

**Prior phases:** Phase 0（`e8cb02c`）、Phase 1（`65978b7`）、Phase 2（`69e416d`）。

## Global Constraints

- **纯文本行为逐字节不变。** 每个任务都要有一条断言证明：纯文本输入下新旧行为相等。**尤其是校验**——纯文本消息不得因新增校验而改变任何行为（不抛、不改内容）。
- **本 Phase 不做外部化。** `source_type` 恒为 `"base64"`，不得出现 `normalize_content` / ref 产出 / adapter rehydrate（那些是 Phase 3b）。
- **不碰装配链**（`core/assembler/`）、**不碰 `core/loop/steps/`**。本 Phase 只动 adapter、protocols、`core/content.py`、`core/errors.py` 与四个入口所在文件。
- **摘要恒为纯文本**（spec §8），`_history.py` 三个包装器不得动。
- 非文本判据保持 `not hasattr(p, "text")`（spec §13 冻结）。
- **⚠️ 测试写法陷阱**：`assert any(not hasattr(p, "text") for p in content)` 在 `content` 是 `str` 时**恒为 True**（字符串每个字符都没有 `.text`），是重言式。凡断言「图片存活」必须先钉 `assert isinstance(content, list)` 或用完整相等。本分支已两次因此返工。
- 不得新增 PytestWarning；**不得用模块级 `pytestmark`**（会给同步测试也打标记）。
- **绝对禁止 `git stash`**（本分支已因此误弹过用户其它分支的 stash）。需要对比改造前行为用 `git checkout <sha> -- src/` 取旧版、跑完 `git checkout HEAD -- src/` 复原。
- 测试运行器 `uv run pytest`；本环境 `-q` 配合大量 warning 时**不输出终结汇总行**，用 `-v` 或不带 `-q`。
- **跑全量套件时单跑、勿并发**——本仓有对资源竞争敏感的 liveness 测试，并发会制造假失败。
- 全量基线：`3 failed / 1568 passed / 3 skipped`。三条为既有环境失败（`test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`、`test_golden_conformance.py::test_golden_dir_present`、`test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`），**不要试图修**。出现第四条即为本 Phase 引入。

## 读取方枚举

本 Phase 不放宽任何字段的类型，**不扩大 `list[ContentPart]` 的可达范围**，故无需读取方枚举（Phase 1/2 的教训不适用）。新增的 `supports_vision` 属性只有一个读取点（Task 2 的门控），新增的 `validate_content` 只有四个调用点（Task 3）。

---

### Task 1: Q7 —— 空白文本块不出网

**Files:**
- Modify: `src/ctx_weft/providers/llm/anthropic.py`（`_parts_to_blocks` 的两个文本分支）
- Modify: `src/ctx_weft/providers/llm/openai.py`（同上）
- Modify: `src/ctx_weft/providers/llm/anthropic.py`（assistant 分支那处失效注释 = Q8）
- Test: `tests/unit/test_adapter_multimodal_wire.py`（追加）

**Interfaces:**
- Produces: 无新接口。行为变化：`TextPart` 的文本为空**或纯空白**时不再产出文本 block。

**背景（controller 已实测）：** Phase 2 的 I1 修复只跳过了 falsy 文本（`if text:`），而 `TextPart("   ")` 是 **truthy**——它挨着 `ImagePart` 时仍产出 `{"type":"text","text":"   "}`。Anthropic 对空**和纯空白**文本块同样返回 400。实测：

```
纯空白+图 wire: [{"role": "user", "content": [{"type": "text", "text": "   "}, {"type": "image", ...
该消息会被 gateway 丢吗? False   ← 图片确是内容，消息被正确保留
只有纯空白（无图）会被丢吗? True  ← 无图时上游已丢弃
```

最后一行是本改动的**安全性依据**：只含空白 `TextPart` 而无图的消息，在 `llm_gateway._is_empty_content`（它 strip）那里就被丢了，所以收紧跳过条件**不会**产出空 block 列表。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_adapter_multimodal_wire.py` 末尾追加（复用该文件既有的 `_img()` 等 helper；若名字不同以文件实际为准）：

```python
def test_anthropic_whitespace_text_part_next_to_image_dropped():
    """纯空白文本块与空文本块同属一类：Anthropic 对两者都返回 400。"""
    out = anth([LLMMessage(role="user", content=[TextPart(text="   "), _img()])])
    blocks = out[0]["content"]
    assert not any(b.get("type") == "text" and not b.get("text", "").strip()
                   for b in blocks), "空白文本块不得出网"
    assert any(b.get("type") == "image" for b in blocks), "图片必须保留"


def test_openai_whitespace_text_part_next_to_image_dropped():
    out = oai("", [LLMMessage(role="user", content=[TextPart(text="   "), _img()])])
    parts = out[0]["content"]
    assert not any(p.get("type") == "text" and not p.get("text", "").strip()
                   for p in parts), "空白文本块不得出网"
    assert any(p.get("type") == "image_url" for p in parts), "图片必须保留"


def test_anthropic_meaningful_text_with_leading_space_preserved():
    """收紧的是「纯空白」，不是「带空白」——有实义的文本一字不改。"""
    out = anth([LLMMessage(role="user", content=[TextPart(text="  hi  "), _img()])])
    texts = [b["text"] for b in out[0]["content"] if b.get("type") == "text"]
    assert texts == ["  hi  "], "有实义的文本必须原样保留，含首尾空白"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_wire.py -v`
Expected: 前两条 FAIL（空白块仍出网），第三条 PASS

- [ ] **Step 3: 收紧两家 adapter 的跳过条件**

`anthropic.py` 与 `openai.py` 的 `_parts_to_blocks` 中，dict 形态与 dataclass 形态**两个**文本分支里的：

```python
            if text:
```

改为：

```python
            if text.strip():        # 纯空白块与空块同属 provider 400 的一类（spec §13）
```

**注意**：`text` 可能来自 `p.get("text", "")` 或 `getattr(p, "text", "")`，两种取法下都是 `str`，`.strip()` 安全。**四处都要改**（两家 × dict/dataclass）。改完自查一遍，别漏。

- [ ] **Step 4: 修 Q8 的失效注释**

`anthropic.py` 的 assistant 分支有一处注释陈述「`_parts_to_blocks` 对每个 part 恰好产出一个 block」——该不变式在 Phase 2 的 I1 修复后**已经失效**（falsy 文本 part 会被跳过），本任务进一步收紧后更不成立。

代码本身仍正确（`content_blocks or ""` 的兜底行为不变：`content_blocks` 为空仍蕴含消息无实义内容），但注释会误导后来者——**它正是 Task 5 当初删除那段死代码所依据的不变式**。

把该注释更正为陈述当前真实的性质：`content_blocks` 为空 ⟺ 消息既无实义文本也无图片也无 tool_calls ⟹ 此时兜底的 `""` 与拍扁结果一致。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_adapter_multimodal_wire.py -v`
Expected: 全部 passed（含既有的纯文本 wire 基准用例）

- [ ] **Step 6: 变异验证**

把四处 `.strip()` 去掉（改回 `if text:`），确认前两条新测试**转红**；复原后确认全绿。报告里给逐字证据。

- [ ] **Step 7: 回归**

Run: `uv run pytest tests/unit -k "adapter or anthropic or openai or mock or gateway" -v`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add src/ctx_weft/providers/llm/ tests/unit/test_adapter_multimodal_wire.py
git commit -m "fix(llm): 纯空白文本块不出网，堵住 Anthropic 400（Q7）+ 更正失效注释（Q8）"
```

---

### Task 2: 视觉能力门控 —— `supports_vision`（严格默认）

**Files:**
- Modify: `src/ctx_weft/providers/llm/provider.py`（`ModelConfig` + `_FixedModelClient`）
- Modify: `src/ctx_weft/protocols/llm.py`（协议文档，说明这是可选 duck-typed 属性）
- Test: `tests/unit/test_vision_gating.py`（新建）

**Interfaces:**
- Produces:
  - `ModelConfig.supports_vision: bool = False`
  - `_FixedModelClient.supports_vision -> bool`（property，返回 `ModelConfig` 的配置值）
  - 约定：core 一律用 `getattr(llm, "supports_vision", False)` 读取——**未声明即视为无视觉能力**

**为什么落在 `ModelConfig` 而不是 adapter：** 视觉能力是 **per-model** 的（OpenAI 的 gpt-4o 支持、gpt-3.5-turbo 不支持），而 `supports_tool_calling` 是 per-adapter 硬编码 `True`（同一 provider 的现代模型普遍支持）。`ModelConfig` 已经承载 `context_limit` / `output_reserve` / `output_ceiling` 这些 per-model 配置，是自然落点。

**为什么严格默认（用户决定）：** 缺省 `False` 意味着未显式声明的模型一律拒绝图片。这是**破坏性变更**——第三方 adapter 与未更新配置的宿主接图片时会被拒。代价换来的是"绝不让图片打到不支持的模型"。本任务只建立机制与默认值，**门控的执行点在 Task 3**。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_vision_gating.py`：

```python
from ctx_weft.providers.llm.provider import ModelConfig


def test_model_config_defaults_to_no_vision():
    """严格默认：未显式声明即无视觉能力。"""
    assert ModelConfig(name="m", context_limit=1000).supports_vision is False


def test_model_config_accepts_explicit_vision():
    assert ModelConfig(name="m", context_limit=1000, supports_vision=True).supports_vision is True


def test_getattr_convention_on_object_without_property():
    """core 的读取约定：未声明该属性的 client 一律视为无视觉能力。"""
    class _LegacyClient:
        pass

    assert getattr(_LegacyClient(), "supports_vision", False) is False
```

再加两条驱动 `_FixedModelClient` 的测试——它是 `_resolve_llm` 在真实路径上返回的类型，**必须确认属性真的透传下来了**，而不是只在 `ModelConfig` 上存着。

controller 已核实其构造签名为纯赋值、无 IO（`provider.py:56-70`），可直接实例化，`adapter` 传 `None` 即可（本测试不触发 `complete`）：

```python
from ctx_weft.providers.llm.provider import _FixedModelClient


def _client(*, vision: bool):
    return _FixedModelClient(
        adapter=None, model="m", context_limit=1000,
        output_reserve=100, output_ceiling=None, account="acct",
        supports_vision=vision,
    )


def test_fixed_model_client_exposes_vision_true():
    """_FixedModelClient 是 core 实际拿到的 client——属性必须透传。"""
    assert _client(vision=True).supports_vision is True


def test_fixed_model_client_defaults_to_no_vision():
    """构造时不传该参数 → 严格默认。"""
    c = _FixedModelClient(adapter=None, model="m", context_limit=1000, output_reserve=100)
    assert getattr(c, "supports_vision", False) is False
```

**这两条不可省略**：`ModelConfig` 上有字段但 client 没透传，等于门控永远拿不到真值、静默退回严格默认、**所有图片都被拒**——那是一个测试全绿但功能完全失效的形态。

**注意**：上面的构造用了 `supports_vision=` 关键字参数，意味着 Step 4 除了加 property 之外，还要给 `_FixedModelClient.__init__` 加一个**带默认值 `False`** 的同名参数（默认值保证既有调用点不受影响），并由 `LLMProvider.get_client` 从 `ModelConfig` 传入。**若你选择不改构造签名而用别的透传方式**（例如直接持有 `ModelConfig`），按你的实现调整这两条测试，并在报告中说明理由。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_vision_gating.py -v`
Expected: 前两条 FAIL（`ModelConfig` 无 `supports_vision`），第三条 PASS

- [ ] **Step 3: 加 `ModelConfig` 字段**

`provider.py` 的 `ModelConfig` 追加（放在 `output_ceiling` 之后，保持 per-model 能力配置聚在一起）：

```python
    # 是否支持图片输入（多模态）。**默认 False = 严格**：未显式声明的模型一律拒绝图片，
    # 宁可在入口报错，也不让 image block 打到纯文本模型后被 provider 400。
    # 这是破坏性默认——宿主须为支持视觉的模型显式开启。
    supports_vision: bool = False
```

- [ ] **Step 4: 在 `_FixedModelClient` 上暴露并透传**

三处改动：

**(a)** `__init__` 追加带默认值的参数（默认值保证既有调用点全部不受影响）：

```python
        output_ceiling: int | None = None,
        account: str = "",
        supports_vision: bool = False,      # 严格默认，见 ModelConfig
    ) -> None:
        # ...（既有赋值原样保留）
        self._supports_vision = supports_vision
```

**(b)** 与既有的 `output_ceiling` property 并列追加：

```python
    @property
    def supports_vision(self) -> bool:
        return self._supports_vision
```

**(c)** `LLMProvider.get_client` 的返回行（`provider.py:245`）：

```python
        return _FixedModelClient(adapter, resolved_model, ctx_limit, reserve, ceiling, account=name)
```

改为：

```python
        return _FixedModelClient(adapter, resolved_model, ctx_limit, reserve, ceiling,
                                 account=name, supports_vision=vision)
```

并在该函数中从选中的 `ModelConfig` 取出 `vision`——**照它取 `ceiling` 的同一形态**（先读那几行，`ceiling` 怎么从 model config 里取出来的，`vision` 就怎么取；若该函数在 model config 缺失时有兜底分支，`vision` 的兜底一律取 `False`）。

**这一步的 (c) 最容易漏。** 只做 (a)(b) 的话，`ModelConfig` 上配了 `True` 也传不到 client，门控永远拿到 `False`——测试可能仍绿（Step 1 的两条直接构造 client、绕过了 `get_client`），但功能完全失效。Step 7 的回归与 Task 4 的人工核对要覆盖这一点。

- [ ] **Step 5: 在协议文档中记录约定**

`protocols/llm.py` 的 `LLMClient` 中，`output_ceiling` 那段「可选（duck-typed，非协议必需）」注释之后，追加同形态的一段：

```python
    # 可选（duck-typed，非协议必需）：supports_vision -> bool
    #   是否支持图片输入。core 一律用 getattr(llm, "supports_vision", False) 读取——
    #   **未声明即视为无视觉能力**（严格默认，spec §6.7）。这样第三方 adapter 不必被迫
    #   实现新属性，但也因此拿不到视觉能力：宿主须显式配置 ModelConfig.supports_vision=True。
```

**不要**把它加成 `@abstractmethod`——那会让所有既有 adapter 实现失效。

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_vision_gating.py -v`
Expected: 全部 passed

- [ ] **Step 7: 回归**

Run: `uv run pytest tests/unit -k "provider or llm or model or adapter" -v`
Expected: 全部 PASS（新字段有默认值，既有构造不受影响）

- [ ] **Step 8: 提交**

```bash
git add src/ctx_weft/providers/llm/provider.py src/ctx_weft/protocols/llm.py tests/unit/test_vision_gating.py
git commit -m "feat(llm): ModelConfig.supports_vision（严格默认 False）+ duck-typed 读取约定"
```

---

### Task 3: 入口校验 + 门控执行点

**Files:**
- Modify: `src/ctx_weft/core/content.py`（新增 `validate_content`）
- Modify: `src/ctx_weft/core/errors.py`（新增两个异常）
- Modify: `src/ctx_weft/core/runtime.py`（`start_session` / `run_single_task` 两个入口）
- Test: `tests/unit/test_content_validation.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `supports_vision` 约定
- Produces:
  - `core.content.validate_content(content, *, llm=None) -> None` —— 校验通过则返回 `None`，否则抛
  - `core.errors.InvalidContentError`（畸形/超限）与 `core.errors.VisionNotSupportedError`（模型无视觉能力）

**校验项（全部只作用于 `ImagePart`，纯文本零影响）：**

| 项 | 规则 |
|---|---|
| `media_type` 白名单 | `image/png` / `image/jpeg` / `image/gif` / `image/webp`（Anthropic 与 OpenAI 的交集） |
| base64 合法性 | `source_type == "base64"` 时须能 `base64.b64decode(data, validate=True)` |
| 单图字节上限 | 解码后 ≤ 5 MiB（Anthropic 单图约 5MB 上限） |
| 视觉能力 | 传入 `llm` 且内容含图时，`getattr(llm, "supports_vision", False)` 必须为 `True` |

**为什么不校验总量：** spec §6.1 明确「入口只做格式校验，不做 token 准入」——单条消息塞太多图由装配期 `ContextOverflowError` 兜底（子设计 §3）。**不要**在这里加总量检查。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_content_validation.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_content_validation.py -v`
Expected: FAIL，`ImportError: cannot import name 'validate_content'`

- [ ] **Step 3: 加异常**

`core/errors.py` 中与既有异常并列追加：

```python
class InvalidContentError(CtxWeftError):
    """入口内容格式非法：未知 media_type / base64 畸形 / 单图超限。

    入口即拒，不落库——比让畸形内容流到 provider 侧再 400 更早、更可诊断。
    """

    code = "INVALID_CONTENT"


class VisionNotSupportedError(CtxWeftError):
    """当前模型未声明视觉能力，拒绝图片输入（spec §6.7 严格默认）。

    未在 ModelConfig 上显式 supports_vision=True 的模型一律视为无视觉能力。
    宿主若确认该模型支持图片，请显式配置。
    """

    code = "VISION_NOT_SUPPORTED"
```

- [ ] **Step 4: 实现 `validate_content`**

在 `core/content.py` 追加（`__all__` 同步加名）：

```python
# ── 入口校验 ───────────────────────────────────────────────────────────────

# Anthropic 与 OpenAI 都接受的交集。扩这个集合前请先确认两家都支持。
ALLOWED_IMAGE_MEDIA_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp",
})

_MAX_IMAGE_BYTES = 5 * 1024 * 1024      # Anthropic 单图约 5MB 上限


def validate_content(
    content: "str | list[ContentPart] | None", *, llm: object | None = None
) -> None:
    """入口内容校验。通过返回 None，否则抛。

    只作用于 ImagePart——纯文本（str / 全 TextPart / None / 空）零影响、恒通过。

    llm 非 None 且内容含图时，额外执行视觉能力门控：
    ``getattr(llm, "supports_vision", False)`` 必须为真。**未声明即视为无视觉能力**
    （严格默认，spec §6.7）。拿不到 client 的调用点可不传 llm，只做格式校验。

    刻意**不**校验 token 总量——单条消息塞太多图由装配期 ContextOverflowError
    兜底（spec §6.1 / 子设计 §3）。
    """
    if not content or isinstance(content, str):
        return
    images = [p for p in content if not _is_text_part(p)]
    if not images:
        return

    if llm is not None and not getattr(llm, "supports_vision", False):
        raise VisionNotSupportedError(
            "当前模型未声明视觉能力（supports_vision），拒绝图片输入。"
            "若该模型确实支持图片，请在 ModelConfig 上显式设置 supports_vision=True。"
        )

    for img in images:
        media_type = getattr(img, "media_type", "") or ""
        if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
            raise InvalidContentError(
                f"不支持的图片类型 {media_type!r}；"
                f"允许：{sorted(ALLOWED_IMAGE_MEDIA_TYPES)}"
            )
        if getattr(img, "source_type", "base64") != "base64":
            continue        # url / ref 形态不在本 Phase 校验范围
        try:
            raw = base64.b64decode(getattr(img, "data", "") or "", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidContentError(f"图片 base64 解码失败：{exc}") from exc
        if len(raw) > _MAX_IMAGE_BYTES:
            raise InvalidContentError(
                f"单张图片 {len(raw)} 字节超过上限 "
                f"{_MAX_IMAGE_BYTES}（5 MiB）"
            )
```

文件头补 `import base64` 与 `import binascii`，并从 `core.errors` import 两个异常（**注意循环依赖**：先确认 `core/errors.py` 未 import `core/content.py`；若有循环，改用函数内 import 并在报告中说明）。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_content_validation.py -v`
Expected: 12 passed

- [ ] **Step 6: 接进入口**

`runtime.py` 的两个入口——`start_session`（`params.user_prompt`）与 `run_single_task`（`user_prompt`）——在**任何持久化动作之前**调用：

```python
        validate_content(params.user_prompt, llm=self._resolve_llm(
            params.llm_account, params.llm_model))
```

**注意**：`start_session` 中 `_resolve_llm` 的调用时机——若原本在 `SessionManager.create_session` 之后才解析，需要提前解析一次（`_resolve_llm` 是纯查表、无副作用，重复调用安全；**请先读代码确认这一点**，若它有副作用则改为在能拿到 client 的最早位置校验，并在报告中说明）。

`run_single_task` 中 `llm = self._resolve_llm(...)` 已在函数早期，直接在其后加校验即可。

**HITL 应答与 reopen 两个入口本 Phase 不接**——它们拿 client 的路径更绕（需从 session 投影解析），且 reopen 的 prompt 由 core 自己拼装、不含用户新贴的图。记入 spec §13 作为已知缺口。

- [ ] **Step 7: 入口测试**

在 `tests/unit/test_content_validation.py` 追加一条驱动真实入口的测试：构造一个 `supports_vision=False` 的 client，调 `run_single_task(user_prompt=[TextPart, ImagePart])`，断言抛 `VisionNotSupportedError` **且 memory 中无任何记录**（证明"入口即拒、不落库"）。

**若驱动 `run_single_task` 成本过高**，退而验证 `start_session`；两者都过高则在报告中说明，并以直接调用 `validate_content` 的测试为准。**但要先尝试**——"入口即拒、不落库"是这条改动的全部价值，只测函数本身证明不了它接对了位置。

- [ ] **Step 8: 变异验证**

把入口处的 `validate_content(...)` 调用注释掉，确认 Step 7 的测试**转红**；复原后确认全绿。逐字证据进报告。

- [ ] **Step 9: 回归**

Run: `uv run pytest tests/unit -k "runtime or session or start or content" -v`
Expected: 全部 PASS

- [ ] **Step 10: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/errors.py src/ctx_weft/core/runtime.py tests/unit/test_content_validation.py
git commit -m "feat(entry): 入口校验（media_type/base64/尺寸）+ 视觉能力门控，入口即拒不落库"
```

---

### Task 4: 本仓两家 adapter 的视觉声明 + spec 更新 + 全量回归

**Files:**
- Modify: `src/ctx_weft/providers/llm/anthropic.py` / `openai.py`（若需要，见 Step 1）
- Modify: `docs/superpowers/specs/2026-08-20-multimodal-design.md`
- Test: 全仓

- [ ] **Step 1: 判断本仓 adapter 是否需要声明**

**先读代码再动手**：`_resolve_llm` 在真实路径上返回的是 `_FixedModelClient`（它包着 adapter），而 Task 2 已让它从 `ModelConfig` 暴露 `supports_vision`。

所以问题是：**是否存在绕过 `_FixedModelClient`、直接把裸 adapter 交给 core 的路径？** 若有，那条路径上的 adapter 需要自己声明；若没有，本仓 adapter **不需要改**——宿主通过 `ModelConfig` 配置即可。

在报告中给出你的判断与依据。**不要为了"看起来完整"而给 adapter 加一个用不到的硬编码属性**——那会与 per-model 配置冲突（adapter 级的 `True` 会让所有该 provider 的模型都被视为支持视觉，绕过 `ModelConfig` 的严格默认）。

- [ ] **Step 2: 更新 spec**

`docs/superpowers/specs/2026-08-20-multimodal-design.md`：

1. **§13 未决项**：把「纯空白文本块仍会出网」与「`anthropic.py` 注释失效」两条标注为**已兑现**（Task 1），注明测试函数名。**不删原文，只追加**。
2. **§6.7**：把视觉能力门控从"待实现"改为已落地，写明：
   - 落在 `ModelConfig.supports_vision`（per-model，非 per-provider）
   - **严格默认 `False`** —— 这是破坏性变更，未显式配置的模型拒绝图片
   - core 的读取约定 `getattr(llm, "supports_vision", False)`
3. **§6.1**：写明入口校验已落地（`media_type` 白名单 / base64 / 5 MiB 单图上限），以及**刻意不做 token 总量准入**的理由。
4. **§13 新增已知缺口**：HITL 应答与 `reopen_task` 两个入口**未接**校验与门控。

- [ ] **Step 3: 全量回归（单跑、勿并发）**

Run: `uv run pytest tests/`
Expected: 失败数**必须仍是 3**，且正是那三条既有环境失败。任何第四条都是本 Phase 引入——**报告，不要试图修**。

- [ ] **Step 4: ruff 增量核对**

```bash
SRC=$(git diff --name-only <phase3a-base>..HEAD -- 'src/*.py' | tr '\n' ' ')
uv run ruff check $SRC --select I001,F401,F811
```
与基线（`git checkout <phase3a-base> -- src/` 后同命令）**逐条**比对，只看 HEAD 独有的行。期望**零新增**。比对完务必 `git checkout HEAD -- src/` 复原。

- [ ] **Step 5: 人工确认三条不变量**

1. **纯文本零影响**：`validate_content` 对 `str` / `None` / 全 `TextPart` 恒早返回；两家 adapter 的纯文本 wire 形态未变（既有基准测试仍绿）。
2. **严格默认真的严格**：未声明 `supports_vision` 的 client 走 `getattr(..., False)` 拿到 `False`，图片被拒。
3. **未越界**：`git diff --stat` 不含 `core/assembler/`、`core/loop/steps/`。

- [ ] **Step 6: 提交**

```bash
git add docs/superpowers/specs/ src/ctx_weft/providers/llm/
git commit -m "docs(spec): Q7/Q8 兑现、视觉门控与入口校验落地说明、HITL/reopen 缺口记录"
```

---

## Phase 3a 完成标准

- 纯空白文本块不再出网（两家 adapter，四处跳过条件）
- `ModelConfig.supports_vision` 就位，**严格默认 `False`**，`_FixedModelClient` 已透传
- `validate_content` 在 `start_session` / `run_single_task` 两个入口生效，**入口即拒、不落库**
- 纯文本行为逐字节不变；全量失败数仍为 3
- spec 记录：两条已兑现、门控与校验的落地形态、HITL/reopen 的已知缺口

## 后续

**Phase 3b（外部化）**：真 `BlobStore` 实现 + `normalize_content`（base64→ref）+ adapter rehydrate + **per-purpose rehydrate 策略**（`compact` / `recognize_intent` 不 rehydrate，降级为文本占位——见 spec §13 关于 compaction 重发所有图片的条目）。

Phase 3b 的计划中必须携带 Phase 1 终审留下的契约：**`normalize_content` 不得 try/except `NullBlobStore.put` 的 `NotImplementedError`**——那会把"响亮失败"变成控制流。必须先探询 store（`isinstance` 或 `can_externalize` 属性）再决定是否外部化。
