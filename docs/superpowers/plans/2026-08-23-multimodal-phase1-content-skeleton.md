# 多模态 Phase 1：内容骨架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让多模态内容能进入运行时、能持久化、能跨重启还原——但还到不了模型（那是 Phase 2）。

**Architecture:** 新增 `core/content.py` 作为唯一的内容形态转换层（拼接、JSON 序列化、事件脱敏），新增 `protocols/filesystem.BlobStore` 协议与 `NullBlobStore` 默认实现，把四个 agent-loop 入口的 `user_prompt`/`message` 签名从 `str` 放宽为 `str | list[ContentPart]`，并让事件 payload 与投影经 `content_to_jsonable` / `content_from_jsonable` 往返。

**Tech Stack:** Python 3.11+，pytest + pytest-asyncio，`uv run pytest`。

**Spec:** `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§4、§5.1、§5.3、§6.1、§6.2、§10 Phase 1）

**Prior phase:** Phase 0 已完成（commit `e8cb02c`），提供 `utils.image_tokens` / `utils.image_part_count`。

## Global Constraints

- **纯文本行为逐字节不变。** 每个任务都必须有一条断言证明：`str` 输入下新旧行为相等。这是贯穿整个多模态改造的硬约束。
- **Phase 1 不做外部化。** `normalize_content` 接受可选 blob store，默认 `NullBlobStore` 时对内容是**恒等变换**。base64 → ref 的转换属于 Phase 3（spec §10）。本 Phase 内 `ImagePart.source_type` 恒为 `"base64"`。
- **Phase 1 不碰装配链、不碰 `providers/llm/*`。** 图片能进 memory、能落事件、能还原，但装配期仍会被 `content_to_text` 拍扁——这是 Phase 2 的工作。不要"顺手"改装配。
- `Session.user_prompt` / `SessionView.user_prompt` **保持 `str`**（spec §6.1）：只存 `content_to_text` 摘要。全量只放 `Task.user_prompt`，避免两处真源。
- `_IMAGE_PART_TOKENS = 1600` 单一真源在 `src/ctx_weft/core/utils.py`；非文本 part 判据保持 `not hasattr(p, "text")`（Phase 1 仍冻结，见 spec §13）。
- 测试运行器：`uv run pytest`。仓库有约 12000 条既有 ruff 违规，**不得**跨仓库跑 `ruff --fix`；如需使用，限定到单文件单规则。
- 全量套件基线：`3 failed, 1479 passed, 3 skipped`。三条失败为既有环境问题（`test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`、`test_golden_conformance.py::test_golden_dir_present`、`test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`），**不要试图修**。出现第四条失败即为本 Phase 引入。

---

### Task 1: `core/content.py` — 拼接与文本归一

**Files:**
- Create: `src/ctx_weft/core/content.py`
- Test: `tests/unit/test_content_module.py`

**Interfaces:**
- Consumes: `ctx_weft.core.utils.content_to_text`（已存在）
- Produces:
  - `content_to_text(content) -> str` —— 从 `utils` re-export，语义不变
  - `content_with_prefix(content, text: str) -> str | list[ContentPart]`
  - `content_with_suffix(content, text: str) -> str | list[ContentPart]`

  两个拼接函数的契约：`str` 输入走原字符串拼接、返回 `str`；`list` 输入返回新列表，把 `text` 并入首个/末个 `TextPart`；列表首/末不是 `TextPart` 时**新插一个**。空 `text` 为 no-op（原样返回）。`None` 输入按空字符串处理，返回 `text`。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_content_module.py`：

```python
from ctx_weft.core.content import content_to_text, content_with_prefix, content_with_suffix
from ctx_weft.protocols import ImagePart, TextPart


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


# ── str 路径：与朴素字符串拼接逐字节相同 ──────────────────────────────────

def test_prefix_on_str_is_plain_concat():
    assert content_with_prefix("body", "head") == "headbody"


def test_suffix_on_str_is_plain_concat():
    assert content_with_suffix("body", "tail") == "bodytail"


def test_empty_text_is_noop_on_str():
    assert content_with_prefix("body", "") == "body"
    assert content_with_suffix("body", "") == "body"


def test_none_content_yields_text():
    assert content_with_prefix(None, "head") == "head"
    assert content_with_suffix(None, "tail") == "tail"


# ── list 路径：并入首/末个 TextPart，图片不动 ──────────────────────────────

def test_prefix_merges_into_leading_text_part():
    out = content_with_prefix([TextPart(text="body"), _img()], "head")
    assert [type(p).__name__ for p in out] == ["TextPart", "ImagePart"]
    assert out[0].text == "headbody"


def test_suffix_merges_into_trailing_text_part():
    out = content_with_suffix([_img(), TextPart(text="body")], "tail")
    assert [type(p).__name__ for p in out] == ["ImagePart", "TextPart"]
    assert out[1].text == "bodytail"


def test_prefix_inserts_new_part_when_leading_is_image():
    out = content_with_prefix([_img(), TextPart(text="body")], "head")
    assert [type(p).__name__ for p in out] == ["TextPart", "ImagePart", "TextPart"]
    assert out[0].text == "head"


def test_suffix_appends_new_part_when_trailing_is_image():
    out = content_with_suffix([TextPart(text="body"), _img()], "tail")
    assert [type(p).__name__ for p in out] == ["TextPart", "ImagePart", "TextPart"]
    assert out[2].text == "tail"


def test_empty_text_is_noop_on_list():
    src = [TextPart(text="body"), _img()]
    assert content_with_suffix(src, "") is src


def test_input_list_is_not_mutated():
    src = [TextPart(text="body"), _img()]
    content_with_prefix(src, "head")
    assert src[0].text == "body", "拼接必须返回新列表，不得就地改写调用方的 part"


def test_empty_list_yields_single_text_part():
    out = content_with_suffix([], "tail")
    assert len(out) == 1 and out[0].text == "tail"


# ── re-export ────────────────────────────────────────────────────────────

def test_content_to_text_reexported():
    from ctx_weft.core.utils import content_to_text as util_impl
    assert content_to_text is util_impl
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_content_module.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ctx_weft.core.content'`

- [ ] **Step 3: 实现**

新建 `src/ctx_weft/core/content.py`：

```python
"""内容形态归一层（多模态 Phase 1）。

全仓唯一允许做「内容形态转换」的地方：拼接、JSON 往返、事件脱敏。其余模块只调
本模块，不各自写 isinstance 分支——这是把改动从「N 处散点」收成「1 个模块 +
N 处替换」的关键（spec 2026-08-20-multimodal-design §3①）。

不变量：所有函数对 ``str`` 输入的行为与改造前的朴素字符串操作**逐字节相同**。
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from ctx_weft.core.utils import content_to_text

if TYPE_CHECKING:
    from ctx_weft.protocols import ContentPart

__all__ = [
    "content_to_text",
    "content_with_prefix",
    "content_with_suffix",
]


def _is_text_part(part: Any) -> bool:
    """非文本 part 的判据与 utils.content_to_text / image_part_count 一致。

    刻意用 duck-type 而非 isinstance：测试桩与第三方 provider 可能给出等价的
    鸭子类型对象。（已知局限见 spec §13 的 dict-shaped part 隐患。）
    """
    return hasattr(part, "text")


def content_with_prefix(
    content: "str | list[ContentPart] | None", text: str
) -> "str | list[ContentPart]":
    """把 text 拼到内容开头。

    str → 朴素拼接（与改造前逐字节相同）。list → 返回**新列表**，并入首个
    TextPart；首个不是 TextPart 时新插一个。空 text 原样返回（含 list 的同一
    对象，供调用方的 no-op 判定）。None 按空文本处理。
    """
    if not text:
        return content if content is not None else ""
    if content is None:
        return text
    if isinstance(content, str):
        return f"{text}{content}"
    from ctx_weft.protocols import TextPart
    if content and _is_text_part(content[0]):
        head = dataclasses.replace(content[0], text=f"{text}{content[0].text}")
        return [head, *content[1:]]
    return [TextPart(text=text), *content]


def content_with_suffix(
    content: "str | list[ContentPart] | None", text: str
) -> "str | list[ContentPart]":
    """把 text 拼到内容末尾。语义与 content_with_prefix 对称。"""
    if not text:
        return content if content is not None else ""
    if content is None:
        return text
    if isinstance(content, str):
        return f"{content}{text}"
    from ctx_weft.protocols import TextPart
    if content and _is_text_part(content[-1]):
        tail = dataclasses.replace(content[-1], text=f"{content[-1].text}{text}")
        return [*content[:-1], tail]
    return [*content, TextPart(text=text)]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_content_module.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py tests/unit/test_content_module.py
git commit -m "feat(content): 内容归一层——保 parts 的拼接与 content_to_text re-export"
```

---

### Task 2: `content.py` — JSON 往返与事件脱敏

**Files:**
- Modify: `src/ctx_weft/core/content.py`
- Test: `tests/unit/test_content_jsonable.py`

**Interfaces:**
- Consumes: Task 1 的 `core/content.py`
- Produces:
  - `content_to_jsonable(content) -> str | list[dict]` —— `str`/`None` 原样返回（`None` → `None`）；list 转成 `[{"type": "text", "text": ...}, {"type": "image", "data": ..., "media_type": ..., "source_type": ...}]`
  - `content_from_jsonable(raw) -> str | list[ContentPart] | None` —— 逆变换。未知 `type` 的元素**跳过并不抛**（前向兼容：将来的 part 类型不应让旧代码崩溃）。
  - `redact_content_for_event(content) -> str` —— 文本原样，每个图片渲染成 `[image {media_type} {source_type}:{data 前 12 字符}…]`

  往返不变量：`content_from_jsonable(content_to_jsonable(x)) == x` 对 `str` / `None` / part 列表均成立。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_content_jsonable.py`：

```python
import json

from ctx_weft.core.content import (
    content_from_jsonable, content_to_jsonable, redact_content_for_event,
)
from ctx_weft.protocols import ImagePart, TextPart


def _img():
    return ImagePart(data="ZGF0YWRhdGFkYXRh", media_type="image/png")


# ── str / None 恒等 ───────────────────────────────────────────────────────

def test_str_passes_through_unchanged():
    assert content_to_jsonable("hello") == "hello"
    assert content_from_jsonable("hello") == "hello"


def test_none_passes_through():
    assert content_to_jsonable(None) is None
    assert content_from_jsonable(None) is None


# ── 往返 ─────────────────────────────────────────────────────────────────

def test_roundtrip_preserves_parts():
    src = [TextPart(text="look"), _img()]
    assert content_from_jsonable(content_to_jsonable(src)) == src


def test_jsonable_output_is_actually_json_serializable():
    """这是本函数存在的理由：ContentPart 是普通 dataclass，json.dumps 会直接炸。"""
    payload = content_to_jsonable([TextPart(text="look"), _img()])
    json.dumps(payload)  # 不抛即通过


def test_image_fields_survive_roundtrip():
    out = content_from_jsonable(content_to_jsonable([_img()]))
    assert out[0].data == "ZGF0YWRhdGFkYXRh"
    assert out[0].media_type == "image/png"
    assert out[0].source_type == "base64"


# ── 前向兼容 ─────────────────────────────────────────────────────────────

def test_unknown_part_type_is_skipped_not_raised():
    raw = [{"type": "text", "text": "keep"}, {"type": "video", "data": "x"}]
    out = content_from_jsonable(raw)
    assert len(out) == 1 and out[0].text == "keep"


# ── 脱敏 ─────────────────────────────────────────────────────────────────

def test_redact_leaves_plain_text_untouched():
    assert redact_content_for_event("hello") == "hello"


def test_redact_replaces_image_with_marker():
    out = redact_content_for_event([TextPart(text="look"), _img()])
    assert "look" in out
    assert "ZGF0YWRhdGFkYXRh" not in out, "脱敏后不得残留完整 base64"
    assert "image/png" in out


def test_redact_none_is_empty_string():
    assert redact_content_for_event(None) == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_content_jsonable.py -v`
Expected: FAIL，`ImportError: cannot import name 'content_to_jsonable' from 'ctx_weft.core.content'`

- [ ] **Step 3: 实现**

在 `src/ctx_weft/core/content.py` 的 `__all__` 中追加三个名字：

```python
__all__ = [
    "content_to_text",
    "content_with_prefix",
    "content_with_suffix",
    "content_to_jsonable",
    "content_from_jsonable",
    "redact_content_for_event",
]
```

并在文件末尾追加：

```python
# ── JSON 往返（事件 payload / 投影快照）─────────────────────────────────────


def content_to_jsonable(
    content: "str | list[ContentPart] | None",
) -> "str | list[dict] | None":
    """把内容转成可 json.dumps 的形态。

    ContentPart 是普通 dataclass，直接进 json.dumps 会 TypeError——事件与投影
    落库前必须过这一层（spec §6.2）。str / None 原样返回，纯文本路径零成本。
    """
    if content is None or isinstance(content, str):
        return content
    out: list[dict] = []
    for part in content:
        if _is_text_part(part):
            out.append({"type": "text", "text": part.text})
        else:
            out.append({
                "type": "image",
                "data": part.data,
                "media_type": part.media_type,
                "source_type": getattr(part, "source_type", "base64"),
            })
    return out


def content_from_jsonable(
    raw: "str | list[dict] | None",
) -> "str | list[ContentPart] | None":
    """content_to_jsonable 的逆变换。

    未知 type 的元素**跳过而不抛**：将来新增 part 类型时，旧版本读到新数据应当
    降级而非崩溃（事件流是只增的，回放会遇到比自己新的数据）。
    """
    if raw is None or isinstance(raw, str):
        return raw
    from ctx_weft.protocols import ImagePart, TextPart
    out: list[ContentPart] = []
    for item in raw:
        kind = item.get("type")
        if kind == "text":
            out.append(TextPart(text=item.get("text", "")))
        elif kind == "image":
            out.append(ImagePart(
                data=item.get("data", ""),
                media_type=item.get("media_type", ""),
                source_type=item.get("source_type", "base64"),
            ))
        # 未知类型：跳过
    return out


# ── 事件脱敏 ───────────────────────────────────────────────────────────────

_REDACT_DATA_PREVIEW = 12


def redact_content_for_event(content: "str | list[ContentPart] | None") -> str:
    """把内容渲染成适合进事件 payload 的字符串。

    图片渲染成短标记而非原始 base64——一张图几万字符，直接进 LLM_PROMPT_SENT
    会把事件库撑爆（spec §6.8）。
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if _is_text_part(part):
            parts.append(part.text)
        else:
            data = getattr(part, "data", "") or ""
            src = getattr(part, "source_type", "base64")
            parts.append(
                f"[image {getattr(part, 'media_type', '?')} "
                f"{src}:{data[:_REDACT_DATA_PREVIEW]}…]"
            )
    return "".join(parts)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_content_jsonable.py tests/unit/test_content_module.py -v`
Expected: 22 passed

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py tests/unit/test_content_jsonable.py
git commit -m "feat(content): JSON 往返与事件脱敏"
```

---

### Task 3: `ImagePart.source_type` + `BlobStore` 协议 + `NullBlobStore` + registry 接线

**Files:**
- Modify: `src/ctx_weft/protocols/context.py`（`ImagePart.source_type`）
- Modify: `src/ctx_weft/protocols/filesystem.py`
- Modify: `src/ctx_weft/protocols/__init__.py`
- Modify: `src/ctx_weft/core/runtime.py`（`ProviderRegistry`）
- Test: `tests/unit/test_blob_store.py`

**先做 `ImagePart` 的一行改动**（spec §4.1）：

```python
    source_type: Literal["base64", "url"] = "base64"
```

改为：

```python
    source_type: Literal["base64", "url", "ref"] = "base64"
```

只扩取值、不加字段。`"ref"` 在 Phase 1 内**不会被任何代码产出**（外部化是 Phase 3），此处先扩是为了让 `content_to_jsonable` 的往返与 Phase 3 的接线不必再动协议。默认值不变，纯文本与既有多模态路径零影响。

**Interfaces:**
- Produces:
  - `ctx_weft.protocols.BlobStore`（ABC，`put(data: bytes, media_type: str, ctx) -> str` / `get(ref: str, ctx) -> tuple[bytes, str] | None`）
  - `ctx_weft.protocols.BLOB_REF_PREFIX = "blob:"`
  - `ctx_weft.protocols.NullBlobStore`（`put` 抛 `NotImplementedError`，`get` 返回 `None`）
  - `ProviderRegistry.register_blob_store(store)` / `.get_blob_store() -> BlobStore`（未注册时返回 `NullBlobStore` 单例）

**为什么 `NullBlobStore.put` 抛而不是返回 base64：** Phase 1 内没有任何调用方会调 `put`（外部化在 Phase 3）。让它抛，可以在 Phase 3 接线错误时立刻暴露，而不是静默产出一个假 ref。`get` 返回 `None` 则是正常的降级路径（spec §5.1 明确要求 `get` 不得 raise）。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_blob_store.py`：

```python
import pytest

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import BLOB_REF_PREFIX, BlobStore, NullBlobStore, ProviderContext


def _ctx():
    return ProviderContext(session_id="s", tenant_id="tn")


def test_null_store_get_returns_none_and_does_not_raise():
    assert NullBlobStore().get("blob:whatever", _ctx()) is not None or True
    # get 是 async——见下一条；此条只保证类可实例化
    assert isinstance(NullBlobStore(), BlobStore)


@pytest.mark.asyncio
async def test_null_store_get_is_none():
    assert await NullBlobStore().get("blob:whatever", _ctx()) is None


@pytest.mark.asyncio
async def test_null_store_put_raises():
    """Phase 1 无调用方；抛错可在 Phase 3 接线错误时立刻暴露。"""
    with pytest.raises(NotImplementedError):
        await NullBlobStore().put(b"x", "image/png", _ctx())


def test_registry_defaults_to_null_store():
    reg = ProviderRegistry()
    assert isinstance(reg.get_blob_store(), NullBlobStore)


def test_registry_returns_registered_store():
    class _Fake(BlobStore):
        async def put(self, data, media_type, ctx):
            return f"{BLOB_REF_PREFIX}fake"

        async def get(self, ref, ctx):
            return (b"x", "image/png")

    reg = ProviderRegistry()
    store = _Fake()
    reg.register_blob_store(store)
    assert reg.get_blob_store() is store


def test_blob_ref_prefix_value():
    assert BLOB_REF_PREFIX == "blob:"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_blob_store.py -v`
Expected: FAIL，`ImportError: cannot import name 'BLOB_REF_PREFIX' from 'ctx_weft.protocols'`

- [ ] **Step 3: 加协议**

在 `src/ctx_weft/protocols/filesystem.py` 末尾追加（`SpillSink` 之后）：

```python
BLOB_REF_PREFIX = "blob:"


class BlobStore(ABC):
    """core 的「二进制 sink」契约：存取图片等二进制内容，core 只见 ref。

    与同处的 SpillSink 同形——core 不直接碰存储，只知道「有个 sink 能存能取」。
    宿主侧实现落点也相同（FilesystemToolsProvider 已持有 per-session workspace）。

    put 必须**内容寻址且幂等**：同样的 data 返回同样的 ref，重复调用不重复存。
    这同时给到三件事：写入端去重、重放安全、以及 rehydrate 字节稳定——同一 ref
    每次还原出的 base64 完全一致，Anthropic 的 prompt cache 前缀不会被打碎。

    get 对不存在 / 已回收的 ref 返回 None，**不得 raise**：blob 过期、宿主换机、
    GC 误删都会发生，调用方据此降级为文本占位，绝不因取图失败中断 loop。
    """

    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        ...

    @abstractmethod
    async def get(
        self, ref: str, ctx: ProviderContext
    ) -> "tuple[bytes, str] | None":
        ...


class NullBlobStore(BlobStore):
    """未注册 BlobStore 时的默认实现——保证不接 blob 的宿主行为完全不变。

    put 刻意抛错：Phase 1 内没有任何调用方（外部化在 Phase 3），抛错可在
    Phase 3 接线错误时立刻暴露，而不是静默产出一个假 ref。
    """

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        raise NotImplementedError(
            "No BlobStore registered; register one via "
            "ProviderRegistry.register_blob_store() before externalizing content."
        )

    async def get(
        self, ref: str, ctx: ProviderContext
    ) -> "tuple[bytes, str] | None":
        return None
```

- [ ] **Step 4: 导出**

在 `src/ctx_weft/protocols/__init__.py` 中，找到从 `ctx_weft.protocols.filesystem` 导入的那一行（现导入 `FS_PROVIDER_NAME` / `FsTool` / `SpillSink`），加入三个新名字，并在 `__all__` 中追加 `"BLOB_REF_PREFIX"`、`"BlobStore"`、`"NullBlobStore"`。若该模块此前未被 `__init__` 导入，则新增一行：

```python
from ctx_weft.protocols.filesystem import (
    BLOB_REF_PREFIX, BlobStore, FS_PROVIDER_NAME, FsTool, NullBlobStore, SpillSink,
)
```

（以文件实际内容为准调整名字集合，**不要**删除任何既有导出。）

- [ ] **Step 5: registry 接线**

在 `src/ctx_weft/core/runtime.py` 的 `ProviderRegistry.__init__` 中，与既有的 `self._llm_provider` 等字段并列，加一行：

```python
        self._blob_store: "BlobStore | None" = None
```

并在 `register_llm_provider` / `get_llm_provider` 附近（`ProviderRegistry` 类内）追加：

```python
    def register_blob_store(self, store: "BlobStore") -> None:
        """注册二进制内容存储。未注册时 get_blob_store() 返回 NullBlobStore。"""
        self._blob_store = store

    def get_blob_store(self) -> "BlobStore":
        """取 blob store；未注册时返回 NullBlobStore（行为与不接 blob 完全一致）。"""
        if self._blob_store is None:
            from ctx_weft.protocols import NullBlobStore
            self._blob_store = NullBlobStore()
        return self._blob_store
```

`BlobStore` 的类型引用加进该文件既有的 `TYPE_CHECKING` import 块；若不便，直接用字符串标注（如上）即可，不要为它引入模块级 import。

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_blob_store.py -v`
Expected: 6 passed

- [ ] **Step 7: 回归**

Run: `uv run pytest tests/unit -k "registry or provider or runtime" -q`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add src/ctx_weft/protocols/filesystem.py src/ctx_weft/protocols/__init__.py src/ctx_weft/core/runtime.py tests/unit/test_blob_store.py
git commit -m "feat(protocols): BlobStore 协议 + NullBlobStore + registry 接线"
```

---

### Task 4: 入口签名放宽 — runtime 与 session_manager

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:301,316,623`
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py:35,96,155`
- Test: `tests/unit/test_multimodal_entry.py`

**Interfaces:**
- Consumes: `ctx_weft.core.content.content_to_text`（Task 1）
- Produces: `SessionStartParams.user_prompt` 与 `run_single_task(user_prompt=)` 接受 `str | list[ContentPart]`；`Task.user_prompt` 承载全量内容，`Session.user_prompt` 承载 `content_to_text` 摘要。

**关键陷阱：** `runtime.py` 的 `run_single_task` 内有 `description=user_prompt[:200]`——对 `list` 切片会得到一个**截断的 part 列表**而不是报错，是静默错误。必须改走 `content_to_text(user_prompt)[:200]`。`session_manager._make_root_task_manager` 若有同类切片，同样处理。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_multimodal_entry.py`：

```python
import pytest

from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import ImagePart, TextPart

pytestmark = pytest.mark.asyncio


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


async def test_session_start_params_accepts_parts():
    p = SessionStartParams.create(
        template_id="t", user_prompt=_content(), context_limit=1000,
    )
    assert p.user_prompt == _content()


async def test_session_start_params_still_accepts_str():
    p = SessionStartParams.create(
        template_id="t", user_prompt="纯文本", context_limit=1000,
    )
    assert p.user_prompt == "纯文本"
```

**同时**在同文件加一条覆盖 `_make_root_task_manager` 的测试。它是 `SessionManager` 的私有方法，直接调用即可（无需真实 runtime）：

```python
from types import SimpleNamespace

from ctx_weft.core.orchestrator.session_manager import SessionManager
from ctx_weft.core.state.models import Session
from ctx_weft.core.utils import content_to_text, now_utc


async def test_root_task_carries_full_content_session_carries_summary():
    sm = SessionManager(lifecycle_manager=SimpleNamespace(), event_bus=SimpleNamespace())
    session = Session(
        id="s1", user_prompt=content_to_text(_content()), status="RUNNING",
        tenant_id="default", root_agent_id="a1", created_at=now_utc(),
    )
    task, _tm = await sm._make_root_task_manager(session, _content(), None)
    assert task.user_prompt == _content(), "Task 承载全量内容"
    assert isinstance(session.user_prompt, str), "Session 只承载文本摘要"
    assert task.description == "" or isinstance(task.description, str), \
        "description 必须是 str——对 list 切片会静默产出截断的 part 列表"
```

**注意：** `SessionManager` 的构造参数与 `_make_root_task_manager` 的签名以源码为准；若上面的调用形式不匹配，按源码调整**测试**，不要改源码签名来迁就测试。若该方法内部需要 `event_bus.emit`，用 `SimpleNamespace(emit=AsyncMock())` 之类的桩补齐。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_multimodal_entry.py -v`
Expected: 前两条可能已 PASS（`str` 标注不会在运行时拦截 list），第三条 FAIL 或报出 `description` 是列表。**若三条全 PASS**，说明类型标注是唯一的阻碍——那么把第三条改成断言 `task.description` 的确切值（`content_to_text(_content())[:200]`），确保它走的是文本路径。

- [ ] **Step 3: 放宽 `runtime.py` 的标注**

`src/ctx_weft/core/runtime.py`：

第 301 行 `user_prompt: str` → `user_prompt: "str | list[ContentPart]"`
第 316 行（`SessionStartParams.create` 的参数）`user_prompt: str` → `user_prompt: "str | list[ContentPart]"`
第 623 行（`run_single_task` 的参数）同样放宽。

在该文件的 `TYPE_CHECKING` import 块加入 `ContentPart`（若已有则跳过）。

`run_single_task` 内构造 `Task` 的那处：

```python
            description=user_prompt[:200],
```

改为：

```python
            description=content_to_text(user_prompt)[:200],
```

并在函数内加 `from ctx_weft.core.content import content_to_text`（该文件已大量使用函数级 import，遵循既有风格）。

- [ ] **Step 4: 放宽 `session_manager.py` 的标注**

`src/ctx_weft/core/orchestrator/session_manager.py` 的第 35、96、155 行三处 `user_prompt: str` 全部放宽为 `"str | list[ContentPart]"`，并在 `TYPE_CHECKING` 块加入 `ContentPart`。

`create_session` 与 `resume_session` 内构造 `Session(...)` 的 `user_prompt=user_prompt` 改为 `user_prompt=content_to_text(user_prompt)`——**Session 只存文本摘要**（spec §6.1，避免两处真源）。

`_make_root_task_manager` 内构造 `Task(...)` 的 `user_prompt=user_prompt` **保持原样**（Task 承载全量）。若该处有 `description=` 或 `title=` 用到 `user_prompt` 的切片，改走 `content_to_text`。

在文件顶部加 `from ctx_weft.core.content import content_to_text`（模块级即可，`content.py` 只依赖 `utils`，无循环风险）。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_multimodal_entry.py -v`
Expected: 3 passed

- [ ] **Step 6: 回归**

Run: `uv run pytest tests/unit -k "session or runtime or start" -q && uv run pytest tests/integration -q`
Expected: 全部 PASS（integration 允许 1 条既有失败 `test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`）

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/session_manager.py tests/unit/test_multimodal_entry.py
git commit -m "feat(entry): start_session / run_single_task 接受多模态 user_prompt"
```

---

### Task 5: 入口签名放宽 — HITL 与 reopen_task

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py:183,170,197,292,299,309`
- Modify: `src/ctx_weft/core/state/models.py:306`
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`reopen_task`，约 498-565 行）
- Modify: `src/ctx_weft/core/runtime.py`（`_inject_user_reply` 与 `_last_user_prompt`）
- Test: `tests/unit/test_multimodal_hitl_reply.py`

**Interfaces:**
- Consumes: `content_with_prefix`、`content_to_text`（Task 1）
- Produces: `HitlRequest.message` 与 `HitlManager.answer/reject/resolve_answer/resolve_reject` 接受 `str | list[ContentPart]`；`_inject_user_reply` 把多模态回复原样写进 memory。

**三处必须用 `content_with_prefix` 而非 f-string 拼接**（否则图片被拍扁）：
1. `runtime._inject_user_reply` 的 `f"Human declined: {req.message}"`
2. 同函数内 `interrupt_edit_note(prev, content)` 的调用
3. `task_manager.reopen_task` 的 `original_user_prompt` 赋值——现为 `task.user_prompt if isinstance(task.user_prompt, str) else ""`，**多模态会被丢空**

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_multimodal_hitl_reply.py`：

```python
import pytest

from ctx_weft.core.state.models import HitlRequest
from ctx_weft.protocols import ImagePart, TextPart


def _content():
    return [TextPart(text="这是我的答复"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


def test_hitl_request_message_accepts_parts():
    req = HitlRequest(id="h1", form="wait", session_id="s", task_id="t")
    req.message = _content()
    assert req.message == _content()


def test_rejected_reply_keeps_image_via_prefix():
    """拒绝路径把「Human declined:」拼到回复前——必须保 parts。"""
    from ctx_weft.core.content import content_with_prefix
    out = content_with_prefix(_content(), "Human declined: ")
    assert any(not hasattr(p, "text") for p in out), "图片不得在拼接中丢失"
    assert out[0].text.startswith("Human declined: ")
```

并加两条真正驱动 `TaskManager.reopen_task` 的测试（仓库此前无 reopen 测试，fixture 需自建；`reopen_task` 只依赖 `self._tasks` / `self._queue` / `self._lock`，用真实 `TaskManager` 即可）：

```python
from ctx_weft.core.events.bus import InProcessEventBus
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Task
from ctx_weft.core.utils import now_utc

pytestmark = pytest.mark.asyncio


async def _finished_task_manager(prompt):
    tm = TaskManager(session_id="s1", event_bus=InProcessEventBus())
    task = Task(
        id="tsk_1", session_id="s1", status="FINISHED", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt=prompt,
        outputs="旧产出", created_at=now_utc(),
    )
    await tm.push_task(task)
    task.status = "FINISHED"          # push 会置 PENDING，reopen 要求 FINISHED
    return tm, task


async def test_reopen_keeps_multimodal_original_prompt():
    """original_user_prompt 是 reopen 的 base；被丢空会让重开后图片永久消失。"""
    tm, task = await _finished_task_manager(_content())
    assert await tm.reopen_task("tsk_1", reason="重做") is True
    assert task.original_user_prompt == _content(), "多模态 base 必须原样快照"
    assert any(not hasattr(p, "text") for p in task.user_prompt), \
        "重写后的 prompt 必须仍带图片"
    assert "## Revision required" in task.user_prompt[-1].text


async def test_reopen_plain_text_prompt_byte_identical():
    """纯文本路径必须与改造前逐字节相同。"""
    tm, task = await _finished_task_manager("原始要求")
    assert await tm.reopen_task("tsk_1", reason="重做") is True
    assert task.original_user_prompt == "原始要求"
    assert task.user_prompt == (
        "原始要求\n\n## Previous attempt (rejected)\n旧产出\n\n## Revision required\n重做"
    )
```

**若 `TaskManager(...)` / `push_task` 的签名与上面不符，按源码调整测试**，不要改源码签名来迁就测试。第二条测试的期望字符串以改造前的实际输出为准——先在改动前跑一次把真实值抄下来，这才是"逐字节不变"的可信守卫。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_multimodal_hitl_reply.py -v`
Expected: 至少一条 FAIL

- [ ] **Step 3: 放宽 HITL 标注**

`src/ctx_weft/core/state/models.py:306`：

```python
    message: str = ""                             # 人类附带的自由文本：答复 / 拒绝理由 / 备注
```

改为：

```python
    # 人类附带的内容：答复 / 拒绝理由 / 备注。多模态回复（含图片）走同一字段。
    message: "str | list[ContentPart]" = ""
```

并在该文件的 `TYPE_CHECKING` 块加入 `ContentPart`。

`src/ctx_weft/core/orchestrator/hitl_manager.py` 中 `answer(text)`、`reject(message)`、`cancel(message)`、`resolve_answer(text)`、`resolve_reject(message)`、`approve(message)` 六处参数标注放宽为 `"str | list[ContentPart]"`。

- [ ] **Step 4: 改 `_inject_user_reply`**

`src/ctx_weft/core/runtime.py` 的 `_inject_user_reply` 内：

```python
        if req.status == "rejected":
            content = f"Human declined: {req.message}" if req.message else "Human rejected the request."
        else:
            content = req.message or "(no response)"
```

改为：

```python
        from ctx_weft.core.content import content_with_prefix
        if req.status == "rejected":
            content = (content_with_prefix(req.message, "Human declined: ")
                       if req.message else "Human rejected the request.")
        else:
            content = req.message or "(no response)"
```

`interrupt_edit_note(prev, content)` 那一处：读 `ctx_weft/core/loop/steps/act.py` 的 `interrupt_edit_note` 实现，若它内部是 f-string 拼接，把它改为接受并返回 `str | list[ContentPart]`、内部用 `content_with_prefix` / `content_with_suffix`。若改动过大，改为在调用点先用 `content_with_prefix` 组装，`interrupt_edit_note` 只处理文本部分——**两种做法都可，但必须有一条测试证明图片没丢**。

`_last_user_prompt` 的返回 `(ups[-1].content or "")` 可能是 list，其调用方期望 `str`——在返回处加 `content_to_text`。

- [ ] **Step 5: 改 `reopen_task`**

**这一步必须两处一起改，只改第一处会让 reopen 在多模态下崩溃。**

`src/ctx_weft/core/orchestrator/task_manager.py` 的 `reopen_task` 内，第一处：

```python
        if task.original_user_prompt is None:
            task.original_user_prompt = task.user_prompt if isinstance(task.user_prompt, str) else ""
```

改为：

```python
        if task.original_user_prompt is None:
            # 原样保留（含多模态）：这是 reopen 的 base，拍扁会让重开后图片永久消失。
            task.original_user_prompt = task.user_prompt or ""
```

第二处——**关键**。现有代码是：

```python
        prev_output = _outputs_to_text(task.outputs) or (task.process_report or "")
        parts = [p for p in [base_prompt] if p]
        if prev_output:
            parts.append(f"## Previous attempt (rejected)\n{prev_output}")
        if upstream is not None:
            head_title, head_reason = upstream
            parts.append(
                f"## Upstream task revised\n"
                f"Predecessor '{head_title}' was reopened (reason: {head_reason}). "
                f"Its updated result appears in the conversation above. "
                f"Redo this task based on the updated result."
            )
        elif reason:
            parts.append(f"## Revision required\n{reason}")
        new_prompt = "\n\n".join(parts) if parts else base_prompt
```

`"\n\n".join(parts)` 在 `base_prompt` 是 part 列表时会 **TypeError 崩溃**（`sequence item 0: expected str instance, list found`）——现有那句 `isinstance(str) else ""` 恰好挡住了它。删掉强制转换而不改这里，reopen 会在多模态下直接炸。

改为：

```python
        prev_output = _outputs_to_text(task.outputs) or (task.process_report or "")
        sections: list[str] = []
        if prev_output:
            sections.append(f"## Previous attempt (rejected)\n{prev_output}")
        if upstream is not None:
            head_title, head_reason = upstream
            sections.append(
                f"## Upstream task revised\n"
                f"Predecessor '{head_title}' was reopened (reason: {head_reason}). "
                f"Its updated result appears in the conversation above. "
                f"Redo this task based on the updated result."
            )
        elif reason:
            sections.append(f"## Revision required\n{reason}")
        # base 可能是多模态（list[ContentPart]），不能进 "\n\n".join()。
        # 有 base 时从 base 起逐段 content_with_suffix；无 base 时退回纯文本 join。
        # 两条路径对 str base 的产物与改造前**逐字节相同**（已逐例核对）。
        if base_prompt:
            new_prompt = base_prompt
            for sec in sections:
                new_prompt = content_with_suffix(new_prompt, f"\n\n{sec}")
        else:
            new_prompt = "\n\n".join(sections) if sections else base_prompt
```

在文件顶部加 `from ctx_weft.core.content import content_with_suffix`。

**等价性核对（实现者请自行复核一遍）：**
- `base` 非空 + 两段：旧 `"\n\n".join([base, s1, s2])` = `base\n\ns1\n\ns2`；新 `base` → `+"\n\n"+s1` → `+"\n\n"+s2` = 同。
- `base` 为空 + 两段：旧 `parts` 过滤掉 base，`join([s1,s2])` = `s1\n\ns2`；新走 else 分支同。
- `base` 为空 + 无段：旧 `parts` 空 → `base_prompt`；新 else 分支 → `base_prompt`。同。

- [ ] **Step 6: 补 reopen 的真实驱动测试**

按 Step 1 的注记，在 `tests/unit/test_multimodal_hitl_reply.py` 中补一条真正调用 `TaskManager.reopen_task` 的测试，断言 `original_user_prompt` 保留了图片。

- [ ] **Step 7: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_multimodal_hitl_reply.py -v`
Expected: 全部 passed

- [ ] **Step 8: 回归**

Run: `uv run pytest tests/unit -k "hitl or reopen or inject or interrupt" -q`
Expected: 全部 PASS

- [ ] **Step 9: 提交**

```bash
git add src/ctx_weft/core/state/models.py src/ctx_weft/core/orchestrator/hitl_manager.py src/ctx_weft/core/orchestrator/task_manager.py src/ctx_weft/core/runtime.py tests/unit/test_multimodal_hitl_reply.py
git commit -m "feat(entry): HITL 回复与 reopen 保留多模态内容"
```

---

### Task 6: 事件 payload 与投影序列化

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`（`SESSION_CREATED` / `SESSION_RESUMED` payload）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（`TASK_CREATED` / `TASK_REOPENED` payload）
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`（`payload["message"]`）
- Modify: `src/ctx_weft/core/control/types.py:47,48`（`TaskView`）
- Modify: `src/ctx_weft/core/control/reducers.py:185,186,257,258,485,508-513` + `110-117`
- Modify: `src/ctx_weft/core/control/converters.py:54,55`
- Test: `tests/unit/test_multimodal_event_roundtrip.py`

**Interfaces:**
- Consumes: `content_to_jsonable` / `content_from_jsonable`（Task 2）
- Produces: 多模态 `Task.user_prompt` 经事件流 → `rebuild_view` → `task_from_projection` 后**完整还原**。这是跨重启保证的核心。

**`SessionView.user_prompt` 保持 `str`**（Session 只存摘要），只有 `TaskView.user_prompt` / `.original_user_prompt` 放宽。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_multimodal_event_roundtrip.py`：

```python
import json
from datetime import datetime, timezone

from ctx_weft.core.content import content_to_jsonable
from ctx_weft.core.control.converters import task_from_projection
from ctx_weft.core.control.reducers import reduce_events, serialize_view
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.orchestrator.task_manager import _task_payload
from ctx_weft.core.state.models import Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import ImagePart, TextPart


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


def _ts():
    return datetime(2026, 8, 23, tzinfo=timezone.utc)


def _ev(seq: int, type_: str, **payload) -> Event:
    """与 tests/unit/test_snapshot_recovery.py 同一构造惯例。"""
    task_id = payload.pop("task_id", None)
    return Event(
        id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="s1",
        type=type_, timestamp=_ts(), task_id=task_id, payload=payload,
    )


# ── 写侧：payload 必须是可 json 化的形态 ──────────────────────────────────

def test_task_payload_is_json_serializable():
    """ContentPart 是普通 dataclass，直接进 payload 会让宿主的 json 持久化炸掉。"""
    task = Task(
        id="tsk_1", session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt=_content(), created_at=now_utc(),
    )
    payload = _task_payload(task)
    json.dumps(payload)  # 不抛即通过
    assert payload["task"]["user_prompt"] == content_to_jsonable(_content())


def test_task_payload_plain_text_unchanged():
    task = Task(
        id="tsk_1", session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id="a1", creator_agent_id="a1",
        title="T", description="d", user_prompt="纯文本", created_at=now_utc(),
    )
    assert _task_payload(task)["task"]["user_prompt"] == "纯文本"


# ── 读侧：事件回放 → 投影 → 运行时模型，完整还原 ─────────────────────────

def test_task_user_prompt_survives_event_replay():
    """跨重启的核心保证。"""
    events = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="看这张图",
            template_id="tmpl_a", root_agent_id="agt_root"),
        _ev(2, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "ACTIVE", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root",
            "user_prompt": content_to_jsonable(_content()),
        }),
    ]
    view = reduce_events(events)
    task = task_from_projection(view.tasks["tsk_1"])
    assert task.user_prompt == _content(), "多模态 prompt 必须经事件回放完整还原"


def test_snapshot_roundtrip_preserves_parts():
    """快照路径（serialize_view）与事件回放路径必须同样无损。"""
    events = [
        _ev(1, EventType.SESSION_CREATED, user_prompt="看这张图",
            template_id="tmpl_a", root_agent_id="agt_root"),
        _ev(2, EventType.TASK_CREATED, task={
            "id": "tsk_1", "status": "ACTIVE", "title": "T1",
            "assigned_agent_id": "agt_root", "creator_agent_id": "agt_root",
            "user_prompt": content_to_jsonable(_content()),
        }),
    ]
    view = reduce_events(events)
    blob = serialize_view(view)
    json.dumps(blob)  # 快照必须可 json 化
    from ctx_weft.core.control.reducers import deserialize_view
    restored = deserialize_view(blob)
    assert task_from_projection(restored.tasks["tsk_1"]).user_prompt == _content()
```

**若 `deserialize_view` 不是该名字**（`serialize_view` 的逆函数），以 `reducers.py` 实际导出为准调整；`tests/unit/test_snapshot_recovery.py` 里有它的真实用法。`_task_payload` 是 `task_manager.py` 的模块级私有函数（约 1151 行），直接 import 测它是刻意的——它是 `TASK_CREATED` payload 的唯一构造点，比走整个 TaskManager 更精准。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_multimodal_event_roundtrip.py -v`
Expected: 第一条 FAIL（还原后是拍扁的字符串或空），第二条 PASS（Task 2 已提供 `content_to_jsonable`）

- [ ] **Step 3: 事件 payload 序列化**

`session_manager.py` 的 `SESSION_CREATED` payload 里 `"user_prompt": user_prompt` —— Session 只存摘要，改为 `content_to_text(user_prompt)`。`SESSION_RESUMED` 同理。

`task_manager.py` 的 `TASK_CREATED` payload 里 `"user_prompt": task.user_prompt or ""` 改为 `content_to_jsonable(task.user_prompt) or ""`。
`TASK_REOPENED` payload 里的 `"user_prompt"` / `"original_user_prompt"` 同样改走 `content_to_jsonable`。

`hitl_manager.py` 的 `payload["message"] = req.message` 改为 `payload["message"] = content_to_jsonable(req.message)`。

- [ ] **Step 4: 投影类型放宽**

`control/types.py` 的 `TaskView`：

```python
    user_prompt: str = ""
    original_user_prompt: str = ""  # reopen 重写前的原始 prompt 快照（防多轮累加，跨重启保留）
```

改为：

```python
    user_prompt: "str | list[ContentPart]" = ""
    # reopen 重写前的原始 prompt 快照（防多轮累加，跨重启保留）
    original_user_prompt: "str | list[ContentPart]" = ""
```

`SessionView.user_prompt` **不改**（保持 `str`）。在文件的 `TYPE_CHECKING` 块加入 `ContentPart`。

- [ ] **Step 5: reducers 与 converters**

`reducers.py`：
- 第 185-186 行（快照写出，`view_to_dict` 内）：`"user_prompt": t.user_prompt` → `content_to_jsonable(t.user_prompt)`；`original_user_prompt` 同理。
- 第 257-258 行（快照读回）：`user_prompt=t.get("user_prompt", "")` → `content_from_jsonable(t.get("user_prompt", ""))`；`original_user_prompt` 同理。
- 第 485 行（`TASK_CREATED` 回放）：`user_prompt=task_data.get("user_prompt", "")` → 包一层 `content_from_jsonable`。
- 第 508-513 行（`TASK_REOPENED` 回放）：两处 `p.get(...)` 包 `content_from_jsonable`。
- 第 110-117 行（HITL 回放）：`req.message = p["message"]` / `p.get("message", "")` 三处包 `content_from_jsonable`。
- 第 159 / 231 行（Session）：**不改**，Session 保持 `str`。

`converters.py` 第 54-55 行：`user_prompt=proj.user_prompt or None` 与 `original_user_prompt=proj.original_user_prompt or None` 保持原样即可（union 类型下 `or None` 语义不变：空串与空列表都是 falsy）。**但**第 46 行的 `prompt_in_memory = bool(proj.user_prompt) and ...` 对 list 同样成立，无需改。确认这两点后不动该文件；若实现中发现别的 `str` 假设，一并处理并在报告中说明。

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_multimodal_event_roundtrip.py -v`
Expected: 全部 passed

- [ ] **Step 7: 回归**

Run: `uv run pytest tests/unit -k "reducer or projection or replay or converter or rebuild or event" -q`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add src/ctx_weft/core/orchestrator/ src/ctx_weft/core/control/ tests/unit/test_multimodal_event_roundtrip.py
git commit -m "feat(persistence): 事件 payload 与投影经 content_to_jsonable 往返，多模态跨重启还原"
```

---

### Task 7: 落库路径 — `_persist_user_prompt`

**Files:**
- Modify: `src/ctx_weft/core/loop/driver.py`（`_persist_user_prompt`，约 167-186 行）
- Test: `tests/unit/test_persist_multimodal_prompt.py`

**Interfaces:**
- Consumes: 前六个任务的全部产出
- Produces: 多模态 `Task.user_prompt` 原样进入 memory（`MemoryEvent.content` 已是 `str | list[ContentPart]`，无需改协议）

**这是图片在改造前第一次消失的地方**（spec §6.3）。改完之后，图片能一路走到 memory；装配期仍会拍扁（Phase 2 处理），所以本任务的测试断言的是 **memory 里存的是什么**，不是 prompt 里出现了什么。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_persist_multimodal_prompt.py`：

```python
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.driver import _persist_user_prompt
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEventType, ProviderContext, TextPart,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio


def _content():
    return [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]


async def _run(prompt):
    """与 tests/unit/test_current_message_framing.py 同一构造惯例。"""
    mem = InMemoryMemoryProvider()
    pctx = ProviderContext(session_id="s1", tenant_id="default")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    task = SimpleNamespace(
        id="t1", user_prompt=prompt, user_prompt_in_memory=False,
    )
    state = SimpleNamespace(task=task, scope=scope)
    ctx = SimpleNamespace(memory=mem, provider_ctx=pctx)
    await _persist_user_prompt(state, ctx)
    recs = await mem.recall_recent(scope, [MemoryEventType.USER_PROMPT], 10, pctx)
    return task, recs


async def test_persist_user_prompt_keeps_parts():
    """图第一次消失的地方：_persist_user_prompt 此前无条件 content_to_text。"""
    task, recs = await _run(_content())
    assert recs, "必须落一条 USER_PROMPT"
    assert recs[0].content == _content(), "存进 memory 的必须仍是 part 列表"
    assert any(not hasattr(p, "text") for p in recs[0].content), "图片不得丢失"
    assert task.user_prompt_in_memory is True


async def test_persist_user_prompt_str_path_unchanged():
    """纯文本路径必须与改造前逐字节相同。"""
    task, recs = await _run("把这个 ppt 转 pdf")
    assert recs[0].content == "把这个 ppt 转 pdf"
    assert "## Current" not in recs[0].content, "raw 落库，呈现态框架不落库"
    assert task.user_prompt_in_memory is True
```

用 `InMemoryMemoryProvider` 走真实 `ingest` 而非 mock——mock 只能证明"调用了 ingest"，证明不了"存进去的还是 parts"。

`_persist_user_prompt` 内还会读 `task` 的其他字段（如 metadata 里的 `task_id`）；若 `SimpleNamespace` 缺字段导致 `AttributeError`，按报错补齐即可，**不要**改源码去迁就测试桩。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_persist_multimodal_prompt.py -v`
Expected: 多模态那条 FAIL（存进去的是拍扁的字符串），纯文本那条 PASS

- [ ] **Step 3: 实现**

`src/ctx_weft/core/loop/driver.py` 的 `_persist_user_prompt`：

现有代码：

```python
    from ctx_weft.core.utils import content_to_text, now_utc
    text = (task.user_prompt if isinstance(task.user_prompt, str)
            else content_to_text(task.user_prompt))
    await ctx.memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=text,
            timestamp=now_utc(),
            role="user",
            metadata={"task_id": task.id},
        ),
        ctx.provider_ctx,
    )
    task.user_prompt_in_memory = True
```

改为：

```python
    from ctx_weft.core.utils import now_utc
    await ctx.memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            # 原样落库（含多模态）：这是图片在改造前第一次消失的地方。
            # 装配期是否拍扁由框架决定（Phase 2），落库必须无损。
            content=task.user_prompt,
            timestamp=now_utc(),
            role="user",
            metadata={"task_id": task.id},
        ),
        ctx.provider_ctx,
    )
    task.user_prompt_in_memory = True
```

即：删掉 `text` 中间变量与 `content_to_text` 的拍扁，原样传 `task.user_prompt`。`MemoryEvent.content` 的类型已是 `str | list[ContentPart] | None`（`protocols/memory.py:153`），无需改协议。`content_to_text` 在该函数内已无其他用途，从函数级 import 中删除。

**若该函数的实际代码与上面不符**（Phase 0 未改动它，应当一致），以源码为准，但改动的实质不变：去掉拍扁、原样传。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_persist_multimodal_prompt.py -v`
Expected: 全部 passed

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "driver or persist or prompt" -q`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/driver.py tests/unit/test_persist_multimodal_prompt.py
git commit -m "feat(driver): _persist_user_prompt 原样落库多模态 prompt"
```

---

### Task 8: Phase 1 全量回归与协议文档

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（`MemoryProvider` 或 `MemoryEvent` 的 docstring）
- Test: 全仓

- [ ] **Step 1: 补协议文档**

在 `src/ctx_weft/protocols/memory.py` 的 `MemoryEvent.content` 字段注释或 `MemoryProvider` 的类 docstring 中，明确写下宿主契约：

> `content` 可能是 `list[ContentPart]`（多模态）。宿主 provider 必须能持久化并原样返回它——`ctx_weft.core.content.content_to_jsonable` / `content_from_jsonable` 是推荐的落库形态。**不得**在持久化时拍扁成文本：装配期是否拍扁由框架决定，provider 只负责无损存取。

- [ ] **Step 2: 全量测试**

Run: `uv run pytest tests/ 2>&1 | tail -3`
Expected: `3 failed, N passed` —— 失败数**必须仍是 3**，且是 Global Constraints 里列出的那三条。任何第四条都是本 Phase 引入的。

- [ ] **Step 3: ruff 增量核对**

```bash
SRC=$(git diff --name-only <phase1-base>..HEAD -- 'src/*.py' | tr '\n' ' ')
uv run ruff check $SRC --select I001,F401,F811
```
Expected: 无违规。（`<phase1-base>` = Phase 1 第一个 commit 的父提交。）

RUF001/002/003 的中文全角标点增量不必核对——仓库既有约 1855 条同类，是既定风格。

- [ ] **Step 4: 人工确认三条不变量**

1. `Session.user_prompt` / `SessionView.user_prompt` 仍是 `str`，且赋值处都过了 `content_to_text`
2. 事件 payload 里凡承载 `Task.user_prompt` 的地方都过了 `content_to_jsonable`，回放处都过了 `content_from_jsonable`
3. 装配链（`assembler/`）**一行未改**——Phase 1 不碰它

`git diff --stat <phase1-base>..HEAD` 应当**不含** `src/ctx_weft/core/assembler/` 下任何文件。若含，说明有人越界，必须回退。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/memory.py
git commit -m "docs(protocols): 写明宿主 memory provider 的多模态无损存取契约"
```

---

## Phase 1 完成标准

- `core/content.py` 提供 6 个函数，纯文本路径逐字节等价
- `BlobStore` 协议就位，`NullBlobStore` 保证不接 blob 的宿主行为不变
- 四个 agent-loop 入口接受 `str | list[ContentPart]`
- 多模态 `Task.user_prompt` 经事件流跨重启完整还原
- 图片能进 memory（装配期仍拍扁，Phase 2 处理）
- 全量套件失败数仍为 3（既有环境问题）
- 装配链一行未改

## 后续 Phase

Phase 2（端到端打通）的计划在 Phase 1 落地后编写：装配链保 parts + adapter wire 转换 + 事件脱敏。它必须显式验证 spec §6.5 留下的两条义务——`composer.py:400` 的图片项从恒 0 转为生效，以及 `budget.py` 与 `token_estimate` 的数据源对齐。
