# 模态能力回归 adapter 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「模型支不支持图片」的判断从 core 入口彻底移除，改由「注册了哪个 adapter 类」表达；runtime 全程透传多模态数据到 `LLMClient` 面前。

**Architecture:** 内置 adapter 基类（`AnthropicAdapter` / `OpenAIAdapter`）新增可覆盖的 `_prepare_messages(request)` 接缝，默认把图片降级成确定性文本占位并记 warning；新增两个子类 `AnthropicMultimodalAdapter` / `OpenAIMultimodalAdapter` 覆盖该方法为原样透传。`_serialize_messages` 一字不改。core 侧删掉 `validate_content` 的视觉门控、`llm_gateway._gate_tool_images`、`VisionNotSupportedError` 与 `ModelConfig.supports_vision`。

**Tech Stack:** Python 3.11+、pytest / pytest-asyncio、httpx（adapter，extras `[llm]`）

**Spec:** `docs/superpowers/specs/2026-08-28-multimodal-adapter-dispatch-design.md`

## Global Constraints

- **纯文本路径逐字节不变**：无图会话的 wire payload、日志条数、对象身份都不得变化。`_prepare_messages` 在无图时必须返回**同一个 list 对象**，且不发任何日志。
- **adapter 绝不 raise**：adapter 在同步出网主路径上，raise 会掀掉整个 LLM 请求。降级 + warning，不抛。
- **占位逐字节确定**：复用 `core.content.downgrade_images_to_text`，占位为 `[image {media_type}]`，不得含 blob sha / 随机 id / 时间戳 / 计数器（否则砸掉 prompt 前缀缓存）。
- **图片判据冻结**：`not hasattr(p, "text")`，与 `core/utils.image_part_count` / `content_to_text` 同源（spec §13 冻结，用户裁定 D1）。不得在本次改动中另写一份。
- **零迁移**：存量 `LLMAccount.style` 字符串（`"anthropic"` / `"openai"`）行为不变，仍拿到纯文本 adapter。
- 运行测试统一用 `uv run pytest`。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/ctx_weft/providers/llm/_modality.py` | 纯文本 adapter 的降级 + 告警逻辑，两家共用一份 | **新建** |
| `src/ctx_weft/providers/llm/anthropic.py` | 加 `_prepare_messages` 接缝 + `AnthropicMultimodalAdapter` | 修改 |
| `src/ctx_weft/providers/llm/openai.py` | 加 `_prepare_messages` 接缝 + `OpenAIMultimodalAdapter` | 修改 |
| `src/ctx_weft/providers/llm/provider.py` | 新 style 分派；删 `ModelConfig.supports_vision` / `_FixedModelClient.supports_vision` | 修改 |
| `src/ctx_weft/providers/llm/__init__.py` | 惰性导出两个新类 | 修改 |
| `src/ctx_weft/core/content.py` | `validate_content` 删视觉门控与两个 llm 参数 | 修改 |
| `src/ctx_weft/core/errors.py` | 删 `VisionNotSupportedError` | 修改 |
| `src/ctx_weft/core/runtime.py` | 三处入口不再解析/传递 llm | 修改 |
| `src/ctx_weft/core/loop/llm_gateway.py` | 删 `_gate_tool_images` | 修改 |
| `src/ctx_weft/protocols/llm.py` | 改写 duck-typed 注释块 | 修改 |
| `tests/unit/test_adapter_multimodal_dispatch.py` | 四个 adapter 类 × 三种角色的分流断言 | **新建** |
| `tests/unit/test_vision_gating.py` | 门控已不存在 | **删除** |

`_modality.py` 单独成文件，沿用本包既有的私有辅助模块惯例（`_finalize.py` / `_schema.py`）：两家 adapter 的降级逻辑逐字相同，各写一份就是两份会分叉的真源。

---

## Task 1: 纯文本降级辅助模块

**Files:**
- Create: `src/ctx_weft/providers/llm/_modality.py`
- Test: `tests/unit/test_adapter_multimodal_dispatch.py`

**Interfaces:**
- Consumes: `core.content.downgrade_images_to_text`、`core.utils.image_part_count`（均已存在）
- Produces: `downgrade_for_text_only(request: LLMRequest, *, adapter_hint: str) -> list[LLMMessage]` —— 返回降级后的 messages；无图时返回 `request.messages` **同一对象**且不记日志

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/test_adapter_multimodal_dispatch.py`：

```python
"""内置 adapter 的模态分流：类型即声明（spec 2026-08-28-multimodal-adapter-dispatch）。

纯文本 adapter 把图降级成占位 + warning；多模态子类原样透传。
"""

from __future__ import annotations

import base64
import logging

import pytest

from ctx_weft.protocols import ImagePart, LLMMessage, LLMRequest, TextPart

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG_BYTES).decode()


def _img(media_type: str = "image/png") -> ImagePart:
    return ImagePart(data=_PNG_B64, media_type=media_type)


def _req(*messages: LLMMessage) -> LLMRequest:
    return LLMRequest(model="m-1", system="", messages=list(messages))


# ── Task 1：降级辅助 ──────────────────────────────────────────────────────


def test_downgrade_replaces_image_with_deterministic_placeholder():
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()]))
    out = downgrade_for_text_only(req, adapter_hint="AnthropicMultimodalAdapter")

    assert [p.text for p in out[0].content] == ["看图", "[image image/png]"]


def test_downgrade_is_identity_for_text_only_messages():
    """纯文本路径逐字节不变：返回同一个 list 对象，不重建。"""
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content="纯文本"))
    assert downgrade_for_text_only(req, adapter_hint="X") is req.messages


def test_downgrade_warns_once_with_model_count_and_fix_hint(caplog):
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(
        LLMMessage(role="user", content=[_img(), _img()]),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    )
    with caplog.at_level(logging.WARNING):
        downgrade_for_text_only(req, adapter_hint="OpenAIMultimodalAdapter")

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1, "每次请求最多一条 warning"
    msg = records[0].getMessage()
    assert "m-1" in msg and "3" in msg and "OpenAIMultimodalAdapter" in msg


def test_downgrade_emits_no_log_for_text_only(caplog):
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    req = _req(LLMMessage(role="user", content="纯文本"))
    with caplog.at_level(logging.WARNING):
        downgrade_for_text_only(req, adapter_hint="X")
    assert caplog.records == []


def test_downgrade_does_not_mutate_input_messages():
    from ctx_weft.providers.llm._modality import downgrade_for_text_only

    original = LLMMessage(role="user", content=[_img()])
    req = _req(original)
    downgrade_for_text_only(req, adapter_hint="X")
    assert isinstance(original.content[0], ImagePart), "入参消息不得被就地改写"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ctx_weft.providers.llm._modality'`

- [ ] **Step 3: 写实现**

新建 `src/ctx_weft/providers/llm/_modality.py`：

```python
"""纯文本 adapter 的模态降级——两家共用一份（spec 2026-08-28-multimodal-adapter-dispatch §3）。

本包既有的私有辅助模块惯例（`_finalize.py` / `_schema.py`）：两家 adapter 的降级逻辑
逐字相同，各写一份就是两份会分叉的真源。
"""

from __future__ import annotations

import dataclasses
import logging

from ctx_weft.core.content import downgrade_images_to_text
from ctx_weft.core.utils import image_part_count
from ctx_weft.protocols import LLMMessage, LLMRequest

logger = logging.getLogger(__name__)

__all__ = ["downgrade_for_text_only"]


def downgrade_for_text_only(
    request: LLMRequest, *, adapter_hint: str
) -> list[LLMMessage]:
    """把请求里的图片 part 降级成确定性文本占位；返回新 messages 列表。

    ``adapter_hint`` 是对应多模态子类的类名，只用于 warning 文案——把修复路径直接
    写进日志，而不是让运维去翻文档。

    **不抛异常**：调用方在同步出网主路径上，raise 会掀掉整个 LLM 请求（同
    ``_parts_to_blocks`` 的 getattr 兜底、``rehydrate_content`` 的 get→None 降级、
    ``MemoryBlobStore.get`` 恒不抛）。

    **无图时返回 ``request.messages`` 同一对象、且不记任何日志**——纯文本会话逐字节
    不受影响（本计划 Global Constraints 第一条）。

    占位由 ``core.content.downgrade_images_to_text`` 产出（``[image {media_type}]``），
    逐字节确定，不砸 prompt 前缀缓存；图片判据 ``not hasattr(p, "text")`` 复用
    ``image_part_count``，不在本模块另写一份（spec §13 冻结判据）。
    """
    out: list[LLMMessage] = []
    dropped = 0
    for m in request.messages:
        downgraded = downgrade_images_to_text(m.content)
        if downgraded is not m.content:
            dropped += image_part_count(m.content)
            m = dataclasses.replace(m, content=downgraded)
        out.append(m)
    if not dropped:
        return request.messages
    logger.warning(
        "模型 %s 走的是纯文本 adapter，本次请求的 %d 张图片已降级成文本占位。"
        "若该模型确实支持图片，请改用 %s。",
        request.model, dropped, adapter_hint,
    )
    return out
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/llm/_modality.py tests/unit/test_adapter_multimodal_dispatch.py
git commit -m "feat(llm): 纯文本 adapter 的模态降级辅助——两家共用一份"
```

---

## Task 2: Anthropic 的分流接缝与多模态子类

**Files:**
- Modify: `src/ctx_weft/providers/llm/anthropic.py`（`_build_payload` 约 307-309 行；类尾追加子类）
- Test: `tests/unit/test_adapter_multimodal_dispatch.py`

**Interfaces:**
- Consumes: Task 1 的 `downgrade_for_text_only(request, *, adapter_hint)`
- Produces: `AnthropicAdapter._prepare_messages(self, request: LLMRequest) -> list[LLMMessage]`（可覆盖接缝）；`AnthropicMultimodalAdapter(AnthropicAdapter)`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_adapter_multimodal_dispatch.py`：

```python
# ── Task 2：Anthropic 分流 ────────────────────────────────────────────────


def _anth(multimodal: bool):
    from ctx_weft.providers.llm.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    cls = AnthropicMultimodalAdapter if multimodal else AnthropicAdapter
    return cls(api_key="k")


def test_anthropic_text_only_downgrades_user_image():
    payload = _anth(multimodal=False)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[1]["text"] == "[image image/png]"


def test_anthropic_multimodal_keeps_user_image():
    payload = _anth(multimodal=True)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert blocks[1]["source"]["data"] == _PNG_B64


def test_anthropic_text_only_downgrades_assistant_image():
    payload = _anth(multimodal=False)._build_payload(
        _req(LLMMessage(role="assistant", content=[_img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text"]


def test_anthropic_text_only_downgrades_tool_result_image():
    payload = _anth(multimodal=False)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    tool_msg = payload["messages"][-1]["content"][0]
    assert [b["type"] for b in tool_msg["content"]] == ["text"]


def test_anthropic_multimodal_keeps_tool_result_image():
    payload = _anth(multimodal=True)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    tool_msg = payload["messages"][-1]["content"][0]
    assert [b["type"] for b in tool_msg["content"]] == ["image"]


def test_anthropic_plain_text_payload_unchanged_between_classes():
    """纯文本会话在两个类上产出完全相同的 wire。"""
    req = _req(LLMMessage(role="user", content="纯文本"))
    assert _anth(multimodal=False)._build_payload(req) == \
        _anth(multimodal=True)._build_payload(req)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -k anthropic -v`
Expected: FAIL，`ImportError: cannot import name 'AnthropicMultimodalAdapter'`

- [ ] **Step 3: 写实现**

`src/ctx_weft/providers/llm/anthropic.py`，在 `_build_payload` 里把 `_serialize_messages(request.messages)` 换成经接缝的版本，并在 `_build_payload` 之前插入接缝方法：

```python
    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        """出网前的模态处置——**本类是纯文本 adapter**，图片降级成文本占位并告警。

        能力由「注册了哪个 adapter 类」表达（spec 2026-08-28 §2「类型即声明」）：
        要发图请用 ``AnthropicMultimodalAdapter``，它覆盖本方法为原样透传。

        接缝开在这里而不是改 ``_serialize_messages``：后者已经能正确处理图片 part
        （``_parts_to_blocks`` 都在），两种 adapter 的唯一区别是**图片能不能活着走到
        它面前**。故 ``_serialize_messages`` 一字不改。

        纯文本请求返回同一对象、不记日志（无图会话逐字节不受影响）。
        """
        return downgrade_for_text_only(
            request, adapter_hint="AnthropicMultimodalAdapter")

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model if request.model and request.model != "mock" else self._model
        messages = _serialize_messages(self._prepare_messages(request))
```

（`_build_payload` 其余行不动。）

文件顶部 import 区追加：

```python
from ctx_weft.providers.llm._modality import downgrade_for_text_only
```

`LLMMessage` 与 `LLMRequest` 已在该文件的 `from ctx_weft.protocols import (...)` 块里（已核实），无需改动 import 的这一部分。

文件末尾（模块级函数之前或之后均可，建议紧跟 `AnthropicAdapter` 类之后）追加子类：

```python
class AnthropicMultimodalAdapter(AnthropicAdapter):
    """支持图片输入的 Anthropic adapter。

    与基类的唯一区别：``_prepare_messages`` 原样透传，图片得以走到
    ``_serialize_messages`` 面前被拼成 Anthropic image block。

    能力由类型表达（spec 2026-08-28 §2）——host 注册哪个类，就是在声明该账号下的
    模型收不收图。core 对此零判断。
    """

    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        return request.messages
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 跑既有 adapter 测试，确认无回归**

Run: `uv run pytest tests/unit/test_adapter_multimodal_wire.py tests/unit/test_openai_tool_image_relocation.py -v`
Expected: PASS（全绿——本任务未改 `_serialize_messages`）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/providers/llm/anthropic.py tests/unit/test_adapter_multimodal_dispatch.py
git commit -m "feat(llm): AnthropicAdapter 分流接缝 + AnthropicMultimodalAdapter"
```

---

## Task 3: OpenAI 的分流接缝与多模态子类

**Files:**
- Modify: `src/ctx_weft/providers/llm/openai.py`（`_build_payload` 约 303-304 行；类尾追加子类）
- Test: `tests/unit/test_adapter_multimodal_dispatch.py`

**Interfaces:**
- Consumes: Task 1 的 `downgrade_for_text_only(request, *, adapter_hint)`
- Produces: `OpenAIAdapter._prepare_messages(self, request: LLMRequest) -> list[LLMMessage]`；`OpenAIMultimodalAdapter(OpenAIAdapter)`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_adapter_multimodal_dispatch.py`：

```python
# ── Task 3：OpenAI 分流 ───────────────────────────────────────────────────


def _oai(multimodal: bool):
    from ctx_weft.providers.llm.openai import OpenAIAdapter, OpenAIMultimodalAdapter
    cls = OpenAIMultimodalAdapter if multimodal else OpenAIAdapter
    return cls(api_key="k")


def _roles(payload) -> list[str]:
    return [m["role"] for m in payload["messages"]]


def test_openai_text_only_downgrades_user_image():
    payload = _oai(multimodal=False)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert blocks[1]["text"] == "[image image/png]"


def test_openai_multimodal_keeps_user_image_as_data_url():
    payload = _oai(multimodal=True)._build_payload(
        _req(LLMMessage(role="user", content=[TextPart(text="看图"), _img()])))
    blocks = payload["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "image_url"]
    assert blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_openai_text_only_emits_no_relocated_user_message():
    """纯文本 adapter：tool 图已降级 → 不产生重定位的 user 消息。"""
    payload = _oai(multimodal=False)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    assert _roles(payload) == ["user", "tool"]
    assert "[image see the following message]" not in payload["messages"][-1]["content"]


def test_openai_multimodal_relocates_tool_image_to_following_user_message():
    payload = _oai(multimodal=True)._build_payload(_req(
        LLMMessage(role="user", content="q"),
        LLMMessage(role="tool", content=[_img()], tool_call_id="tc1"),
    ))
    assert _roles(payload) == ["user", "tool", "user"]
    assert [b["type"] for b in payload["messages"][-1]["content"]] == ["image_url"]


def test_openai_text_only_downgrades_assistant_image():
    payload = _oai(multimodal=False)._build_payload(
        _req(LLMMessage(role="assistant", content=[_img()])))
    assert [b["type"] for b in payload["messages"][0]["content"]] == ["text"]


def test_openai_plain_text_payload_unchanged_between_classes():
    req = _req(LLMMessage(role="user", content="纯文本"))
    assert _oai(multimodal=False)._build_payload(req) == \
        _oai(multimodal=True)._build_payload(req)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -k openai -v`
Expected: FAIL，`ImportError: cannot import name 'OpenAIMultimodalAdapter'`

- [ ] **Step 3: 写实现**

`src/ctx_weft/providers/llm/openai.py`，接缝方法插在 `_build_payload` 之前，并改其第二行：

```python
    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        """出网前的模态处置——**本类是纯文本 adapter**，图片降级成文本占位并告警。

        能力由「注册了哪个 adapter 类」表达（spec 2026-08-28 §2「类型即声明」）：
        要发图请用 ``OpenAIMultimodalAdapter``，它覆盖本方法为原样透传。

        降级之后 ``_serialize_messages`` 里的 tool 图重定位（``_TOOL_IMAGE_NOTICE``）
        天然空转：已无 image part，``relocated`` 恒为空，一条 user 消息都不追加。
        故那段逻辑不需要任何改动。

        纯文本请求返回同一对象、不记日志（无图会话逐字节不受影响）。
        """
        return downgrade_for_text_only(
            request, adapter_hint="OpenAIMultimodalAdapter")

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model if request.model and request.model != "mock" else self._model
        messages = _serialize_messages(request.system, self._prepare_messages(request))
```

（`_build_payload` 其余行不动。）

文件顶部 import 区追加：

```python
from ctx_weft.providers.llm._modality import downgrade_for_text_only
```

`LLMMessage` 与 `LLMRequest` 已在该文件的 `from ctx_weft.protocols import (...)` 块里（已核实），无需改动 import 的这一部分。

`OpenAIAdapter` 类之后追加子类：

```python
class OpenAIMultimodalAdapter(OpenAIAdapter):
    """支持图片输入的 OpenAI adapter。

    与基类的唯一区别：``_prepare_messages`` 原样透传，图片得以走到
    ``_serialize_messages`` 面前——被拼成 ``image_url`` data URL，且 ``role="tool"``
    里的图按既有逻辑重定位到随后的 user 消息（OpenAI 的 tool 只收纯文本）。

    能力由类型表达（spec 2026-08-28 §2）——host 注册哪个类，就是在声明该账号下的
    模型收不收图。core 对此零判断。
    """

    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        return request.messages
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -v`
Expected: PASS（17 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/llm/openai.py tests/unit/test_adapter_multimodal_dispatch.py
git commit -m "feat(llm): OpenAIAdapter 分流接缝 + OpenAIMultimodalAdapter"
```

---

## Task 4: LLMProvider 接线与包导出

**Files:**
- Modify: `src/ctx_weft/providers/llm/provider.py:21`（`SUPPORTED_STYLES`）、`:339-357`（`_build_adapter`）
- Modify: `src/ctx_weft/providers/llm/__init__.py`
- Test: `tests/unit/test_adapter_multimodal_dispatch.py`

**Interfaces:**
- Consumes: Task 2/3 的 `AnthropicMultimodalAdapter` / `OpenAIMultimodalAdapter`
- Produces: style `"anthropic-multimodal"` / `"openai-multimodal"`；`ctx_weft.providers.llm` 顶层导出这两个名字

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_adapter_multimodal_dispatch.py`：

```python
# ── Task 4：LLMProvider 接线 ─────────────────────────────────────────────


class _StoreStub:
    def save(self, a): ...
    def delete(self, n): return True
    def list_all(self): return []


def _provider_with(style: str):
    from ctx_weft.providers.llm import LLMAccount, LLMProvider, ModelConfig
    p = LLMProvider(_StoreStub())
    p.register_account(LLMAccount(
        name="a", style=style, api_key="k", base_url="https://x",
        models=[ModelConfig(name="m", context_limit=200_000)],
        default_model="m",
    ), persist=False)
    return p


@pytest.mark.parametrize(("style", "cls_name"), [
    ("anthropic", "AnthropicAdapter"),
    ("anthropic-multimodal", "AnthropicMultimodalAdapter"),
    ("openai", "OpenAIAdapter"),
    ("openai-multimodal", "OpenAIMultimodalAdapter"),
])
def test_build_adapter_dispatches_on_style(style, cls_name):
    p = _provider_with(style)
    assert type(p._adapters["a"]).__name__ == cls_name


def test_unknown_style_still_rejected():
    from ctx_weft.providers.llm import LLMAccount, LLMProvider
    p = LLMProvider(_StoreStub())
    with pytest.raises(ValueError):
        p.register_account(LLMAccount(
            name="a", style="gemini", api_key="k", base_url=""), persist=False)


def test_multimodal_classes_exported_from_package():
    import ctx_weft.providers.llm as pkg
    assert pkg.AnthropicMultimodalAdapter.__name__ == "AnthropicMultimodalAdapter"
    assert pkg.OpenAIMultimodalAdapter.__name__ == "OpenAIMultimodalAdapter"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -k "dispatches or exported" -v`
Expected: FAIL，`ValueError: Unsupported style 'anthropic-multimodal'`

- [ ] **Step 3: 写实现**

`provider.py:21` 改为：

```python
# 纯文本与多模态是**两个 style**，不是一个 style 加一个开关——能力由「注册了哪个
# adapter 类」表达（spec 2026-08-28-multimodal-adapter-dispatch §2「类型即声明」）。
# 存量 account 的 "anthropic" / "openai" 行为不变，仍拿到纯文本 adapter，零迁移。
SUPPORTED_STYLES = {
    "anthropic", "anthropic-multimodal",
    "openai", "openai-multimodal",
}
```

`_build_adapter` 改为按前缀取基址、按是否多模态选类：

```python
    def _build_adapter(self, account: LLMAccount) -> LLMClient:
        multimodal = account.style.endswith("-multimodal")
        if account.style.startswith("anthropic"):
            from ctx_weft.providers.llm.anthropic import (
                AnthropicAdapter,
                AnthropicMultimodalAdapter,
            )
            cls = AnthropicMultimodalAdapter if multimodal else AnthropicAdapter
            return cls(
                api_key=account.api_key,
                base_url=account.base_url or "https://api.anthropic.com",
                timeout_sec=account.timeout_sec,
                max_http_retries=self._max_http_retries,
            )
        if account.style.startswith("openai"):
            from ctx_weft.providers.llm.openai import (
                OpenAIAdapter,
                OpenAIMultimodalAdapter,
            )
            cls = OpenAIMultimodalAdapter if multimodal else OpenAIAdapter
            return cls(
                api_key=account.api_key,
                base_url=account.base_url or "https://api.openai.com",
                timeout_sec=account.timeout_sec,
                max_http_retries=self._max_http_retries,
            )
        raise ValueError(f"Unsupported style: {account.style}")
```

`__init__.py`：`TYPE_CHECKING` 块、`__all__`、`__getattr__` 三处各加两个名字：

```python
if TYPE_CHECKING:  # 让类型检查器/IDE 看得到，但运行时不触发 httpx 导入
    from ctx_weft.providers.llm.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    from ctx_weft.providers.llm.openai import OpenAIAdapter, OpenAIMultimodalAdapter
```

```python
    "AnthropicAdapter",
    "AnthropicMultimodalAdapter",
    "OpenAIAdapter",
    "OpenAIMultimodalAdapter",
```

```python
def __getattr__(name: str):
    """惰性导出 httpx-dependent adapter（仅在被访问时才 import）。"""
    if name in ("AnthropicAdapter", "AnthropicMultimodalAdapter"):
        from ctx_weft.providers.llm import anthropic as _m
        return getattr(_m, name)
    if name in ("OpenAIAdapter", "OpenAIMultimodalAdapter"):
        from ctx_weft.providers.llm import openai as _m
        return getattr(_m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

模块 docstring 里的示例注释同步补上两个新名字。

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_adapter_multimodal_dispatch.py -v`
Expected: PASS（23 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/llm/provider.py src/ctx_weft/providers/llm/__init__.py tests/unit/test_adapter_multimodal_dispatch.py
git commit -m "feat(llm): 新增 anthropic-multimodal / openai-multimodal 两个 style"
```

---

## Task 5: 摘掉 core 入口的视觉门控

**Files:**
- Modify: `src/ctx_weft/core/content.py:359-446`（`validate_content`）
- Modify: `src/ctx_weft/core/errors.py:133-141`（删 `VisionNotSupportedError`）
- Modify: `src/ctx_weft/core/runtime.py:578-620`（`_validate_and_normalize_content`）、`:660-690`（`_normalize_hitl_content`）、`:843`（`run_single_task` 调用）、`:929-955`（`start_session` 注释与调用）
- Modify: `src/ctx_weft/protocols/llm.py:289-292`（duck-typed 注释块）
- Test: `tests/unit/test_content_validation.py`、`tests/unit/test_normalize_content.py`、`tests/unit/test_hitl_multimodal_validation.py`

**Interfaces:**
- Produces: `validate_content(content, *, event_blob_store=None) -> None`（`llm` / `llm_resolver` 两个参数删除）；`CtxWeftRuntime._validate_and_normalize_content(content, session_id, *, tenant_id="default")`（`llm` / `llm_account` / `llm_model` 三个参数删除）

- [ ] **Step 1: 改测试，让它们先失败**

`tests/unit/test_content_validation.py`：
1. 顶部 import 去掉 `VisionNotSupportedError`（保留 `InvalidContentError`）
2. 删掉 `_TextOnlyClient` / `_LegacyClient` / `_VisionClient` 三个桩类及「视觉门控」整节（约 93-115 行）
3. 删 `test_llm_resolver_*` 系列（约 350-460 行里所有传 `llm=` / `llm_resolver=` 的用例）
4. 把仍需要的用例中的 `llm=_VisionClient()` 实参一律删除
5. 新增一条替代用例，钉住「不再有视觉门控」：

```python
def test_no_vision_gating_any_more():
    """模态能力已回归 adapter（spec 2026-08-28）：入口对图片一律放行，
    只做格式校验。任何模型能力判断都不在这里。"""
    validate_content([ImagePart(data=_PNG, media_type="image/png")])
```

`tests/unit/test_normalize_content.py`：删 223-233 行那条 `VisionNotSupportedError` 用例、删 287-295 行那条；`_make_runtime_with_store` 的 `supports_vision` 形参与其全部实参删除（236-363 行各调用点）。

`tests/unit/test_hitl_multimodal_validation.py`：删 25 行的 `VisionNotSupportedError` import；删 `supports_vision` 桩属性（91、97 行）；删 180、494、545、569 行四条门控用例及其辅助；`test_vision_gate_allows_image_when_the_named_model_supports_vision`（508 行）改名为 `test_hitl_image_reaches_memory`，去掉 `supports_vision` 相关断言，只保留「图片成功落库」。

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_content_validation.py tests/unit/test_normalize_content.py tests/unit/test_hitl_multimodal_validation.py -v`
Expected: FAIL —— `validate_content` 仍接受 `llm=`，新用例 `test_no_vision_gating_any_more` 可能已通过，但 `test_normalize_content.py` 的 `_make_runtime_with_store` 调用会因签名不符报 `TypeError`

- [ ] **Step 3: 改实现**

`core/content.py`：`validate_content` 签名与 docstring 改为——

```python
def validate_content(
    content: "str | list[ContentPart] | None",
    *,
    event_blob_store: "Any" = None,
) -> None:
    """入口内容校验。通过返回 None，否则抛。

    只作用于 ImagePart——纯文本（str / 全 TextPart / None / 空）零影响、恒通过。

    两道门控，顺序刻意是「格式校验 → event blob 门控」：格式畸形的内容必须报
    ``InvalidContentError``，不能被 blob 门控抢先拦成 ``BlobStoreRequiredError``
    ——那会掩盖真正的问题（终审 2026-08-25 缺陷 B）。

    ⚠️ **这里没有、也不该有「模型支不支持图片」这道门控**（spec
    2026-08-28-multimodal-adapter-dispatch）。模态能力是 ``LLMClient`` 实现方的
    性质，由「host 注册了哪个 adapter 类」表达；core 全程透传多模态内容，纯文本
    adapter 在出网时自行降级成占位并告警。原先那道门控读的是 duck-typed 的
    ``supports_vision``，host 自写的 adapter 几乎必然读不到 → 一律被误判为无视觉。

    刻意**不**校验 token 总量——单条消息塞太多图由装配期 ContextOverflowError
    兜底（spec §6.1 / 子设计 §3）。
    """
```

函数体删除末尾整段视觉门控（`client = llm if ... raise VisionNotSupportedError(...)`），保留其后的 event blob 门控；把 event blob 门控注释里「放在最后，与前两道同理」改成「放在最后，与格式校验同理」。顶部 import 去掉 `VisionNotSupportedError`。

`core/errors.py`：删除 `VisionNotSupportedError` 整个类（133-141 行）。

`core/runtime.py`：

```python
    async def _validate_and_normalize_content(
        self,
        content: "str | list[ContentPart]",
        session_id: str,
        *,
        tenant_id: str = "default",
    ) -> "str | list[ContentPart]":
        """入口内容校验 + 外部化的**单一真源**（三个入口共用）。

        顺序恒为 `validate_content` → `normalize_content`，理由两条（spec §6.1）：
        (a) 被拒的内容不该在 blob store 里留下垃圾——校验失败必须发生在任何 `put`
        之前；(b) `normalize_content` 里的 `b64decode(..., validate=True)` 刻意不加
        try/except，靠 validate 先行把畸形 base64 拦成 `InvalidContentError`。抽成
        这一个方法之后，三处调用点的顺序不会再各自漂移。

        **不解析 LLM、不判模型能力**（spec 2026-08-28）：模态处置归 `LLMClient`
        实现方，core 全程透传。这也让「纯文本不提前解析 LLM」这条不变量自动成立
        ——本方法根本不碰 LLM。

        不能外部化（`NullMemoryBlobStore`）时原样返回同一对象，整段是 no-op。
        """
        from ctx_weft.core.content import normalize_content, validate_content

        event_blob_store = self.providers.get_event_blob_store()
        validate_content(content, event_blob_store=event_blob_store)
        blob_store = self.providers.get_memory_blob_store()
        if not blob_store.can_externalize:
            return content
        return await normalize_content(
            content,
            blob_store=blob_store,
            event_blob_store=event_blob_store,
            ctx=ProviderContext(session_id=session_id, tenant_id=tenant_id),
        )
```

`run_single_task` 里的调用去掉 `llm=llm`：

```python
        user_prompt = await self._validate_and_normalize_content(
            user_prompt, sid, tenant_id=tenant_id,
        )
```

`start_session` 里的调用去掉 `llm_account` / `llm_model`：

```python
        normalized = await self._validate_and_normalize_content(
            params.user_prompt, sid or "", tenant_id=params.tenant_id,
        )
```

并把它上方那段以「入口即拒、不落库」开头的长注释整体替换为：

```python
        # 入口即拒、不落库：sm.create_session / sm.resume_session 会立即持久化
        # （instantiate_agent + SESSION_CREATED/RESUMED 事件），所以格式校验与
        # EventBlobStore 门控必须在它们之前。
        # **不做模型能力判断**（spec 2026-08-28-multimodal-adapter-dispatch）：
        # 模态处置归 LLMClient 实现方，core 全程透传。原先为了视觉门控要在这里
        # 惰性解析 LLM，现在整段不碰 LLM，「纯文本不提前解析 LLM」自动成立。
        # 顺序关键：validate 先于 normalize——被拒的内容不该在 blob store 留垃圾。
```

`_normalize_hitl_content` 的实现改为不再传 llm，docstring 里的 **llm** 小节整段删除，只保留 **tenant** 小节：

```python
        return await self._validate_and_normalize_content(
            content, req.session_id, tenant_id=tenant_id,
        )
```

`protocols/llm.py:289-292` 那段注释块替换为：

```python
    # ⚠️ **core 对模态能力零判断**（spec 2026-08-28-multimodal-adapter-dispatch）。
    #   core 会把 `list[ContentPart]`（含 ImagePart）原样透传到 complete()，实现方
    #   自行决定发多模态、降级成纯文本、还是报错。曾经有一个 duck-typed 的
    #   `supports_vision` 被 core 在入口读取并据以拒绝内容——它不在本协议上，host
    #   自写的 adapter 几乎必然读不到，于是一律被误判为无视觉能力。已删除。
    #   内置实现的做法见 providers/llm/{anthropic,openai}.py 的 _prepare_messages。
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_content_validation.py tests/unit/test_normalize_content.py tests/unit/test_hitl_multimodal_validation.py tests/unit/test_multimodal_entry.py -v`
Expected: PASS

- [ ] **Step 5: 确认 `VisionNotSupportedError` 全仓无残留**

Run: `grep -rn "VisionNotSupportedError" src/ tests/ --include=*.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/errors.py src/ctx_weft/core/runtime.py src/ctx_weft/protocols/llm.py tests/unit/test_content_validation.py tests/unit/test_normalize_content.py tests/unit/test_hitl_multimodal_validation.py
git commit -m "refactor(core): 删除入口视觉门控——模态能力归 LLMClient 实现方"
```

---

## Task 6: 删除 gateway 的 `_gate_tool_images`

**Files:**
- Modify: `src/ctx_weft/core/loop/llm_gateway.py:379-410`（删 `_gate_tool_images`）、`:428`（删调用）、import 区
- Test: `tests/unit/test_openai_tool_image_relocation.py:238-390`

**Interfaces:**
- Produces: `stream_llm(llm, request, *, stream=True, blob_store=None, provider_ctx=None)` 不再对 tool 图做任何门控

- [ ] **Step 1: 改测试，让它们先失败**

`tests/unit/test_openai_tool_image_relocation.py`：

1. 模块 docstring 里「**策略在 gateway**」那段（7-10 行）改为：

```
- **策略在 adapter**：模态能力由「注册了哪个 adapter 类」表达（spec 2026-08-28）。
  纯文本 adapter 在 ``_prepare_messages`` 里把图降级；gateway 不再做任何模态判断。
  本文件只覆盖 wire 序列化（``_serialize_messages`` 的 tool 图重定位），
  分流行为见 tests/unit/test_adapter_multimodal_dispatch.py。
```

2. 删除以下六条依赖 `_gate_tool_images` 的用例（293-390 行）：
   `test_no_vision_downgrades_tool_images_and_adapter_adds_no_user_message`、
   `test_undeclared_vision_is_treated_as_no_vision`、
   `test_vision_model_keeps_tool_images`、
   `test_gate_only_touches_tool_role`、
   `test_plain_text_messages_untouched_by_gate`、
   `test_no_vision_skips_blob_fetch_for_tool_images`

3. `_CapturingLLM`（238-250 行）删掉 `supports_vision` 形参与属性赋值。

4. `test_vision_model_tool_ref_rehydrated_then_relocated`（378 行）保留但去掉
   `supports_vision=True` 实参——它测的是 gateway 的 rehydrate，与模态门控无关。

5. 新增一条钉住「gateway 不再动模态」的用例：

```python
@pytest.mark.asyncio
async def test_gateway_passes_images_through_untouched() -> None:
    """gateway 对模态零判断（spec 2026-08-28）：图片原样到达 LLMClient，
    降不降级由 adapter 自己决定。"""
    llm = _CapturingLLM()
    msg = LLMMessage(role="tool", content=[TextPart(text="截图"), _img()],
                     tool_call_id="tc1")
    await _drain(llm, _request(*_legal(msg)))

    tool_msg = _by_role(llm.seen, "tool")[0]
    assert len(_images(tool_msg.content)) == 1, "gateway 不得降级任何图片"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/unit/test_openai_tool_image_relocation.py -v`
Expected: FAIL —— `test_gateway_passes_images_through_untouched` 因 `_gate_tool_images` 仍在、`_CapturingLLM` 未声明 `supports_vision`（严格默认 False）而把图降掉，断言 `len(...) == 1` 失败

- [ ] **Step 3: 改实现**

`core/loop/llm_gateway.py`：
1. 删除 `_gate_tool_images` 整个函数（379-410 行）
2. `stream_llm` 里删除 `request.messages = _gate_tool_images(llm, request.messages)` 一行
3. `stream_llm` 的 docstring 首句改为：

```
    """发送前合法化 ``request.messages``、把 blob ref 还原成 base64，再流式转发 chunk。

    ⚠️ **本函数不做任何模态门控**（spec 2026-08-28-multimodal-adapter-dispatch）：
    图片原样送到 ``LLMClient`` 面前，发多模态还是降级成占位由实现方决定。曾经这里
    有一个 ``_gate_tool_images``，只降 ``role == "tool"`` 的图——它的前提是「用户递的
    图在入口已被视觉门控拒掉」，入口门控删除后该前提不成立，覆盖面必须扩到所有角色，
    而那正是 adapter 的 ``_prepare_messages`` 在做的事。留在这里就是第二处会分叉的判据。
```

4. import 区：若 `downgrade_images_to_text` 在删除后已无其他使用方，从 `from ctx_weft.core.content import ...` 中移除（`rehydrate_content` 保留）

- [ ] **Step 4: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_openai_tool_image_relocation.py -v`
Expected: PASS

- [ ] **Step 5: 确认无残留**

Run: `grep -rn "_gate_tool_images" src/ tests/ --include=*.py`
Expected: 无输出

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/llm_gateway.py tests/unit/test_openai_tool_image_relocation.py
git commit -m "refactor(gateway): 删除 _gate_tool_images——模态门控不再有第二处判据"
```

---

## Task 7: 删除 `ModelConfig.supports_vision`

**Files:**
- Modify: `src/ctx_weft/providers/llm/provider.py:36-39`、`:62-77`、`:100-105`、`:254-257`
- Delete: `tests/unit/test_vision_gating.py`
- Modify: `tests/unit/test_multimodal_entry.py:99`、`tests/integration/test_multimodal_end_to_end.py:106,190,291,376`、`tests/integration/test_media_fold_replay_e2e.py:265,279,692,704`

**Interfaces:**
- Produces: `ModelConfig` 与 `_FixedModelClient` 回到 master 的字段集合（无 `supports_vision`）

> 🔴 **本任务最容易出错的一步不是删字段，是换基类。** 两个 e2e 文件里有 5 个
> adapter 桩继承自 `AnthropicAdapter` / `OpenAIAdapter`，靠 `self.supports_vision = True`
> 这个实例属性拿到视觉能力。删掉字段后它们会**沉默地退化成纯文本 adapter**——
> 断言「图片进 wire image block」的既有 e2e 会全部转红。基类必须一并换成多模态类。

- [ ] **Step 1: 删除测试文件**

```bash
git rm tests/unit/test_vision_gating.py
```

- [ ] **Step 2: 把 e2e adapter 桩的基类换成多模态类**

`tests/integration/test_multimodal_end_to_end.py`：

1. import 处把 `AnthropicAdapter` 换成 `AnthropicMultimodalAdapter`
2. `_WireCapturingAnthropicAdapter(AnthropicAdapter)`（228 行）→
   `_WireCapturingAnthropicAdapter(AnthropicMultimodalAdapter)`
3. 删掉四处 `llm.supports_vision = True` 及其注释（106、190、291、376 行）——
   能力现在来自基类，不再是实例属性

`tests/integration/test_media_fold_replay_e2e.py`：

1. import 处补 `AnthropicMultimodalAdapter` / `OpenAIMultimodalAdapter`
2. 四个类的基类与 `__init__` 里的显式基类调用同步改：
   - `_WireCapturingAnthropicLLM(_PlaceholderReadingLLM, AnthropicAdapter)` →
     `(_PlaceholderReadingLLM, AnthropicMultimodalAdapter)`，
     `AnthropicAdapter.__init__(self, ...)` → `AnthropicMultimodalAdapter.__init__(self, ...)`
   - `_WireCapturingOpenAILLM` 同构（`OpenAIMultimodalAdapter`）
   - `_TextAnthropicLLM` 同构
   - `_TextOpenAILLM` 同构
3. 删掉四处 `self.supports_vision = True` 及其注释（265、279、692、704 行）

`tests/unit/test_multimodal_entry.py:99`：删掉 `_FixedModelClient(...)` 构造里的
`supports_vision=True` 实参。

- [ ] **Step 3: 运行 e2e，确认换基类没破坏现状**

Run: `uv run pytest tests/integration/ -v`
Expected: PASS（此时字段仍在、只是不再被这些桩使用；本步先把测试侧清干净，
避免下一步删字段时报 `TypeError: unexpected keyword argument`）

- [ ] **Step 4: 删实现**

`provider.py` 删除四处（全部是多模态分支自己加的，删完即回 master 形状）：

1. `ModelConfig` 的 `supports_vision: bool = False` 字段与其上方三行注释
2. `_FixedModelClient.__init__` 的 `supports_vision: bool = False` 形参与 `self._supports_vision = supports_vision`
3. `_FixedModelClient.supports_vision` 属性（含 `@property`）
4. `get_client` 里的 `vision = model_cfg.supports_vision if model_cfg else False`，并把返回语句改回：

```python
        return _FixedModelClient(adapter, resolved_model, ctx_limit, reserve, ceiling, account=name)
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `uv run pytest tests/unit/test_multimodal_entry.py tests/unit/test_adapter_multimodal_dispatch.py tests/integration/ -v`
Expected: PASS

- [ ] **Step 6: 确认全仓无残留**

Run: `grep -rn "supports_vision" src/ tests/ --include=*.py`
Expected: 无输出

- [ ] **Step 7: 提交**

```bash
git add -A src/ctx_weft/providers/llm/provider.py tests/
git commit -m "refactor(llm): 删除 ModelConfig.supports_vision——能力由 adapter 类型表达"
```

---

## Task 8: 端到端回归与文档

**Files:**
- Test: `tests/integration/test_multimodal_end_to_end.py`（追加）
- Modify: `README.md`（LLM 接入一节）
- Modify: `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§6.7 标注作废）

**Interfaces:**
- Consumes: 前 7 个任务的全部产出

- [ ] **Step 1: 写失败的测试**

追加到 `tests/integration/test_multimodal_end_to_end.py`——本条钉住 spec §7 的核心承诺「是传递不是丢弃」。复用本文件既有的 `_run_multimodal_session` / `_recall_user_prompt_parts` / `_image_parts`（366 / 357 / 346 行），只把 adapter 换成纯文本类：

```python
class _TextOnlyWireCapturingAdapter(_WireCapturingAnthropicAdapter, AnthropicAdapter):
    """与 `_WireCapturingAnthropicAdapter` 同样捕获 wire，但走**纯文本** adapter 的
    `_prepare_messages`（spec 2026-08-28：能力由类型表达）。

    MRO 说明：`_WireCapturingAnthropicAdapter` 已继承 `AnthropicMultimodalAdapter`，
    这里显式把 `AnthropicAdapter` 排在后面不足以覆盖——故直接覆盖那个方法本身。
    """

    def _prepare_messages(self, request):
        return AnthropicAdapter._prepare_messages(self, request)


async def _run_text_only_session(*, blob_store):
    """跑一整个多模态 user_prompt 的会话，但 LLM 侧是纯文本 adapter。

    装配与 `_run_multimodal_session` 逐行相同，只换 adapter 类——不另起一套。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _TextOnlyWireCapturingAdapter()
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)
    runtime.providers.register_event_blob_store(_StubEventBlobStore())
    runtime.providers.register_memory_blob_store(blob_store)

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=_MULTIMODAL_PROMPT,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)
    assert state is not None
    assert state.task.status == "FINISHED", f"expected FINISHED, got {state.task.status}"
    return runtime, memory, state, llm


@pytest.mark.asyncio
async def test_text_only_adapter_persists_image_and_keeps_bytes_retrievable(tmp_path) -> None:
    """纯文本 adapter 的会话：图片照样落库、字节照样取得回，只是没上 wire。

    这是 spec 2026-08-28 §7 的核心承诺——旧行为在入口抛 VisionNotSupportedError、
    一个字都不落库；新行为是「传递而非丢弃」，换成多模态 adapter 后同一份历史立刻可看图。
    """
    blob_store = _make_sql_blob_store(tmp_path)   # 同 test_ref_externalized_... 的既有装配
    _rt, memory, state, llm = await _run_text_only_session(blob_store=blob_store)

    # (a) memory 侧：图片仍在，且已外部化成 ref
    content = await _recall_user_prompt_parts(memory, state)
    assert isinstance(content, list), (
        f"USER_PROMPT 记录应仍是 part 列表，实为 {type(content).__name__}"
    )
    images = _image_parts(content)
    assert len(images) == 1, "纯文本 adapter 不得影响落库——图片必须还在 memory 里"
    assert images[0].source_type == "ref"

    # (b) blob 侧：字节真的取得回（换个 adapter 就能看图，不是空头承诺）
    got = await blob_store.get(images[0].data, ProviderContext(session_id=state.session.id))
    assert got is not None and got[0], "blob 里必须有真实字节"

    # (c) wire 侧：这一次确实没发图，而是文本占位
    assert _image_sources(llm.captured_payloads[0]) == [], "纯文本 adapter 不得把图发上 wire"
```

> 实施说明 ①：`_make_sql_blob_store(tmp_path)` 沿用 `test_ref_externalized_in_memory_but_full_base64_on_the_wire`（399 行）里已有的 `SqlMemoryProvider` 装配方式——若那段是内联写的，把它抽成这个辅助函数并让两处共用，不要复制第二份。
>
> 实施说明 ②（**刻意偏离 spec §8 的措辞，不是遗漏**）：spec 写的是「`media:get_image` 仍取得回」，这里断言的是 `blob_store.get(ref)` 拿得到字节。理由：`get_image` 定位的是 **L0.5 占位**，需要先发生一轮 compact 降级才存在，那是一条重得多的链路，且已由 `tests/integration/test_media_fold_replay_e2e.py` 完整覆盖。对本次改动而言，「字节还在、取得回」才是承诺的实质——`get_image` 能否取回完全建立在它之上。不要把这条改写成拉起整个 compact 流程。

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest tests/integration/test_multimodal_end_to_end.py::test_text_only_adapter_persists_image_and_keeps_bytes_retrievable -v`
Expected: FAIL，`NameError: name '_make_sql_blob_store' is not defined`

- [ ] **Step 3: 抽出 `_make_sql_blob_store` 辅助并让测试通过**

把 399 行那条用例里的 `SqlMemoryProvider` 装配抽成模块级 `_make_sql_blob_store(tmp_path)`，两处共用。

- [ ] **Step 4: 运行全量测试**

Run: `uv run pytest -q`
Expected: 全绿

- [ ] **Step 5: 更新文档**

`README.md` 的「LLM 接入 · 方式 B」示例后追加：

```markdown
`style` 取值：`"anthropic"` / `"openai"`（纯文本）与 `"anthropic-multimodal"` /
`"openai-multimodal"`（收图片）。**能力由类型表达**——注册哪个 style，就是在声明
该账号下的模型收不收图。纯文本 adapter 收到图片时会降级成 `[image ...]` 文本占位
并记一条 warning，**不中断会话**；core 对模态零判断，全程透传到 `LLMClient` 面前。
自写 adapter 时，`complete()` 收到的 `LLMMessage.content` 可能是
`list[ContentPart]`，如何处置完全由你决定。
```

`docs/superpowers/specs/2026-08-20-multimodal-design.md` 的 §6.7 段首插入：

```markdown
> ⚠️ **本节已作废（2026-08-28）**：入口视觉门控与 `supports_vision` 严格默认已删除，
> 模态能力回归 adapter。见 `2026-08-28-multimodal-adapter-dispatch-design.md`。
```

- [ ] **Step 6: 提交**

```bash
git add tests/integration/test_multimodal_end_to_end.py README.md docs/superpowers/specs/2026-08-20-multimodal-design.md
git commit -m "test(multimodal): 纯文本 adapter 仍落库图片的端到端回归 + 文档更新"
```
