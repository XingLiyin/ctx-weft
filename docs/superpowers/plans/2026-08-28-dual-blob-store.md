# 双 blob store——事件流全 ref 化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给事件流一套独立的 `EventBlobStore`，让事件库恒不含字节、所有图片都以 ref 形式可回读。

**Architecture:** 新协议 `EventBlobStore` 落在 `protocols/events.py`（与 `MemoryBlobStore` 同形但类型无关）；`ProviderRegistry` 加一个**显式**注册槽（不自动回落到 memory provider）；入口 `normalize_content` 在同一循环内**双写**两个 store，内容寻址保证同一个 ref，读侧两条路径各取各的、永不交叉；五个事件发射点改用新的 async `content_to_event_jsonable`；携图会话未注册 `EventBlobStore` 时入口即拒。

**Tech Stack:** Python 3.11 / pytest（asyncio auto 模式，异步测试直接 `async def`，**不加** `@pytest.mark.asyncio`）/ uv

**Spec:** `docs/superpowers/specs/2026-08-27-dual-blob-store-design.md`
（⚠️ 该 spec 写于 8-27，**§0 记录了六项已变的前提**——实施前必读那一节，正文已按它更新。）

## Global Constraints

- **基线**：全量测试 `3 failed / 5 skipped / 0 xfail`。三个既存失败与本工作无关，
  不得增减、不得试图修：
  - `tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`
  - `tests/unit/test_golden_conformance.py::test_golden_dir_present`
  - `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`
  另有一个**既存的顺序依赖 flake** `test_dispatch_boundary_recap_e2e`：偶尔在全量里冒出、
  隔离跑就过。撞见就重跑确认，**不要修它，也不要因此以为自己弄坏了什么**。
- ⚠️ 本环境的全量 `uv run pytest -q` **不打印**末尾的 `N passed, M failed` 统计行
  （既存环境怪癖）——直接数 `FAILED` / `SKIPPED` 行来核对。
- 🚫 **禁止 `git stash` / `git checkout <path>` / `git reset` / `git clean`。**
  工作区里有**调用者的未提交改动** `tests/unit/conftest.py`。要对照基线就用
  `git show HEAD:<path>`。
- 提交用**精确的文件列表**——**不要 `git add -A` / `git add .` / `git add tests/`**
  （`conftest.py` 就在 `tests/unit/` 下，会被卷进去）。
- **纯文本路径逐字节不变**：不含图的内容在每一层都必须走原路径、返回同一对象。
- **绝不解析占位文案**：`core/media/refs.py` 是本仓唯一知道 L0.5 占位长什么样的地方。
- **不改 memory 侧 blob 的任何既有行为**（回收 SQL、宽限期、租户取向、`collect_blob_refs`）。
- **`protocols/` 不得 import `ctx_weft.core`**；**`providers/events/` 不得 import
  `ctx_weft.core`**（两条都有 ast 守卫钉住，见 `tests/unit/test_protocols_events_relocation.py`）。
- ruff：本仓有**既存** I001 约 321 处，**不要修**；只需确认无**新增**
  （`uv run ruff check --select I,F,E9 --output-format=concise src/ tests/`）。
- 注释与 docstring 用中文，解释「为什么」而非「是什么」。

## 已核实的实施前提（写计划时实测）

- `protocols/events.py` 的 import 块已有 `from abc import abstractmethod`，
  新增 `EventBlobStore` 需**补 `ABC`**。
- `BLOB_REF_PREFIX` 在 `protocols/context.py`（协议层划界移过去的，正是为了让两个 blob
  协议各自取用而不互相 import）。
- 注册面现名 `register_memory_blob_store` / `get_memory_blob_store`（`core/runtime.py:303/307`）。
- 五个发射点现状：
  - `session_manager.py:80`（SESSION_CREATED）、`:149`（SESSION_RESUMED）→ 现用
    `content_to_jsonable_refs_only`（**过渡实现，本计划删除**）
  - `task_manager.py:1185`（`_task_payload` → TASK_CREATED）、`:578` `:579`
    （`reopen_task` → TASK_REQUEUED）→ 现用 `content_to_jsonable`
  - `hitl_manager.py:394`（HITL_*）→ 现用 `content_to_jsonable`
  - `reducers.py:167`（`serialize_view` session 侧）→ 现用 `refs_only`；`:193` `:194`
    （task 侧）→ 现用 `content_to_jsonable`。**`serialize_view` 是同步的**，按 spec §6
    继续用 `content_to_jsonable`（view 从事件还原，本就是 ref，无 base64 可外部化）。
- `MediaCapabilityProvider`：暴露工具的是 `list(ctx) -> list[ToolCapability]`
  （`core/media/capability.py:374`）；`describe()`（`:377`）返回的是
  `CapabilityProviderInfo`。**spec §8 初稿写错成 describe，已订正。**
- `_validate_and_normalize_content` 在 `core/runtime.py:549`，其中
  `if not blob_store.can_externalize: return content` 的短路在 `:582`。

---

## File Structure

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `src/ctx_weft/protocols/events.py` | + `EventBlobStore` / `NullEventBlobStore` | Task 1 |
| `src/ctx_weft/core/runtime.py` | + 注册槽；入口双写接线；门控 | Task 1、2、4 |
| `src/ctx_weft/core/content.py` | `normalize_content` 加参数；+ `content_to_event_jsonable`；− `content_to_jsonable_refs_only` | Task 2、3 |
| `src/ctx_weft/core/errors.py` | + `BlobStoreRequiredError` | Task 4 |
| `src/ctx_weft/core/orchestrator/{session,task,hitl}_manager.py` | 五个发射点改 async 外部化 | Task 3 |
| `src/ctx_weft/core/control/reducers.py` | session 侧改回 `content_to_jsonable` | Task 3 |
| `src/ctx_weft/core/media/capability.py` | `list()` 条件可见 | Task 5 |
| `tests/unit/conftest.py` | ⚠️ **有调用者的未提交改动，任何任务都不得修改此文件** | — |

---

### Task 1: `EventBlobStore` 协议 + 显式注册槽

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（追加协议，补 `ABC` import）
- Modify: `src/ctx_weft/protocols/__init__.py`（导出）
- Modify: `src/ctx_weft/core/runtime.py`（`ProviderRegistry` 加注册槽）
- Test: `tests/unit/test_event_blob_store.py`（新建）

**Interfaces:**
- Produces: `ctx_weft.protocols.events.EventBlobStore` / `NullEventBlobStore`；
  `ProviderRegistry.register_event_blob_store(store)` / `.get_event_blob_store()`。
  Task 2/3/4/5 全部依赖这两个名字。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_event_blob_store.py`：

```python
"""EventBlobStore：事件流侧独立的 blob 协议。

spec: docs/superpowers/specs/2026-08-27-dual-blob-store-design.md

与 MemoryBlobStore 同形但**类型无关**——两侧语义会各自演进（最明显的是回收锚点不同：
memory 侧是记录 is_superseded，event 侧是事件保留策略）。host 要共用就一个类同时实现两者。
"""

from __future__ import annotations

from ctx_weft.core.runtime import ProviderRegistry
from ctx_weft.protocols import ProviderContext
from ctx_weft.protocols.events import EventBlobStore, NullEventBlobStore
from ctx_weft.protocols.memory import MemoryBlobStore


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="ses_1", tenant_id="default")


class _Stub(EventBlobStore):
    """最小实现，验协议可被继承。"""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, str]] = {}

    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
        import hashlib
        ref = f"blob:{hashlib.sha256(data).hexdigest()}"
        self.blobs[ref] = (data, media_type)
        return ref

    async def get(self, ref: str, ctx: ProviderContext):
        return self.blobs.get(ref)


def test_null_store_cannot_externalize() -> None:
    assert NullEventBlobStore().can_externalize is False


def test_stub_can_externalize_by_default() -> None:
    """基类默认 True——既有实现无需改动就是「能存」。"""
    assert _Stub().can_externalize is True


async def test_null_store_get_returns_none_never_raises() -> None:
    """get 对不存在的 ref 恒返 None 不抛：取图失败绝不能中断 loop。"""
    assert await NullEventBlobStore().get("blob:nope", _ctx()) is None


async def test_null_store_put_raises_loudly() -> None:
    """put 刻意抛错：调用方应先探询 can_externalize，而不是调用后捕异常。

    把「响亮失败」降级成控制流，会让真正的接线错误也被静默吞掉。
    """
    import pytest
    with pytest.raises(NotImplementedError):
        await NullEventBlobStore().put(b"x", "image/png", _ctx())


def test_is_independent_of_memory_blob_store() -> None:
    """同形但**类型无关**——不是子类型，也不共用一个 ABC（spec §3）。"""
    assert not issubclass(EventBlobStore, MemoryBlobStore)
    assert not issubclass(MemoryBlobStore, EventBlobStore)


def test_one_class_can_implement_both() -> None:
    """host 要共用就一个类同时继承两者——这是「可分可合」的合的那一半。"""

    class Both(MemoryBlobStore, EventBlobStore):
        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    both = Both()
    assert isinstance(both, MemoryBlobStore)
    assert isinstance(both, EventBlobStore)


def test_registry_explicit_registration() -> None:
    reg = ProviderRegistry()
    assert reg.get_event_blob_store().can_externalize is False  # 默认 Null
    stub = _Stub()
    reg.register_event_blob_store(stub)
    assert reg.get_event_blob_store() is stub


def test_registry_does_not_fall_back_to_memory_provider() -> None:
    """**刻意不自动回落**（spec §4）：自动解析会让「共用」成为隐式默认，
    而本设计的出发点正是让两者可分。host 要共用就把同一个实例注册两次。
    """

    class BothProvider(MemoryBlobStore, EventBlobStore):
        name = "both"

        async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str:
            return "blob:x"

        async def get(self, ref: str, ctx: ProviderContext):
            return None

    reg = ProviderRegistry()
    reg.register_memory_blob_store(BothProvider())
    # memory 侧拿得到，event 侧仍是 Null——不串门
    assert reg.get_memory_blob_store().can_externalize is True
    assert reg.get_event_blob_store().can_externalize is False
```

⚠️ `ProviderRegistry()` 的构造签名请先读 `core/runtime.py` 确认（前面的计划里
`CtxWeftRuntime()` 就被发现无参会抛 `ValueError`；`ProviderRegistry` 未必相同）。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py`
Expected: FAIL — `ImportError: cannot import name 'EventBlobStore' from 'ctx_weft.protocols.events'`

- [ ] **Step 3: 实现**

**(a)** `protocols/events.py` 的 import 补 `ABC`：`from abc import ABC, abstractmethod`。

**(b)** 文件末尾追加（放在 `EventStore` 之后，用 `# ──` 分节，与该文件既有风格一致）：

```python
# ── Blob 存储（事件流的字节侧）─────────────────────────────────────────────────


class EventBlobStore(ABC):
    """事件流侧的「二进制 sink」：存取图片等二进制内容，事件库里只留 ref。

    与 `protocols.memory.MemoryBlobStore` **同形但类型无关**（spec §3）。不做成子类型、
    也不共用一个 ABC，理由是两侧语义会各自演进——最明显的是**回收锚点不同**：memory 侧
    是记录 `is_superseded`，event 侧是事件保留策略。今天同形不代表明天同形。

    host 要共用就一个类同时实现两者，注册两次：

        class MyBlobStore(MemoryBlobStore, EventBlobStore): ...

    **ref 前缀取自 `protocols.context.BLOB_REF_PREFIX`**，与 memory 侧同一个常量——
    内容寻址的 sha 口径两边必须逐字节一致，入口双写才能得到同一个 ref。

    ⚠️ **回收策略由 host 定，core 不规定。** 事件流里的 ref 能否取回字节，完全取决于
    host 让 event blob 活多久：想让事件流永远可重建，就让回收与事件保留策略对齐
    （例如永不回收，或按事件 TTL）。**共用一个实例时尤其当心**——该实现要同时看两侧的
    引用才能安全回收，仅套用 memory 侧 `collect_blobs` 的判据会删掉事件流仍需要的字节
    （spec §9）。
    """

    @property
    def can_externalize(self) -> bool:
        """本 store 是否真的能存——`NullEventBlobStore` 返回 False。

        调用方据此**先探询、再决定**，而不是调用 put 并捕获 NotImplementedError：
        后者会把「响亮失败」降级成控制流，让真正的接线错误也被静默吞掉。
        基类默认 True，既有实现无需改动。
        """
        return True

    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: "ProviderContext") -> str:
        """存字节，返回 ref。必须**内容寻址且幂等**：同样的 data 返回同样的 ref。

        这同时给到三件事：写入端去重、重放安全、以及 rehydrate 字节稳定——同一 ref
        每次还原出的 base64 完全一致，prompt cache 前缀不会被打碎。
        """

    @abstractmethod
    async def get(self, ref: str, ctx: "ProviderContext") -> "tuple[bytes, str] | None":
        """取字节。对不存在 / 已回收的 ref 返回 `None`，**不得 raise**。

        blob 过期、宿主换机、GC 误删都会发生，调用方据此降级为文本占位，
        绝不因取图失败中断 loop。
        """


class NullEventBlobStore(EventBlobStore):
    """未注册 `EventBlobStore` 时的默认实现。

    `put` 刻意抛错而不是静默产出假 ref：调用方（`core.content`）先探询
    `can_externalize` 决定是否外部化，**不**捕获这里的 NotImplementedError——
    它仍是接线错误的响亮信号。
    """

    @property
    def can_externalize(self) -> bool:
        return False

    async def put(self, data: bytes, media_type: str, ctx: "ProviderContext") -> str:
        raise NotImplementedError(
            "No EventBlobStore registered; register one via "
            "ProviderRegistry.register_event_blob_store() before externalizing content."
        )

    async def get(self, ref: str, ctx: "ProviderContext") -> "tuple[bytes, str] | None":
        return None
```

⚠️ `ProviderContext` 来自 `protocols.context`。`protocols/events.py` 目前**不 import
同层模块**——若直接 import 会不会破坏什么？不会（`context.py` 不依赖任何同层模块，
无环），但请用 `TYPE_CHECKING` 守卫 + 字符串注解，保持该文件「运行时只依赖 stdlib」
的现状不变（层序 ast 守卫只禁 `ctx_weft.core`，不禁同层，但保持现状更稳）。

**(c)** `protocols/__init__.py`：把两个新名加进 events 组的 import 与 `__all__`。

**(d)** `core/runtime.py` 的 `ProviderRegistry`：仿 `register_memory_blob_store` /
`get_memory_blob_store` 加一对，但 **`get_event_blob_store` 只有两级**：

```python
    def register_event_blob_store(self, store: "EventBlobStore") -> None:
        """注册事件流侧的二进制存储。未注册时 get_event_blob_store() 返回 NullEventBlobStore。"""
        self._event_blob_store = store

    def get_event_blob_store(self) -> "EventBlobStore":
        """取 event blob store。**只有两级：显式注册 > NullEventBlobStore。**

        刻意不像 `get_memory_blob_store()` 那样自动回落到 memory provider（spec §4）：
        自动解析会让「共用」成为隐式默认，而双 store 的出发点正是让两者**可分**。
        host 要共用就把同一个实例注册两次——意图写在接线代码里，而不是藏在解析规则里。

        `NullEventBlobStore` 实例只建一次，重复调用返回同一对象。
        """
        if self._event_blob_store is not None:
            return self._event_blob_store
        if self._null_event_blob_store is None:
            from ctx_weft.protocols.events import NullEventBlobStore
            self._null_event_blob_store = NullEventBlobStore()
        return self._null_event_blob_store
```

`__init__` 里加 `self._event_blob_store = None` 与 `self._null_event_blob_store = None`
（参照既有的 memory 侧两个字段）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py`
Expected: 8 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`，无新增失败

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: 全绿（层序守卫不得因新增协议而红）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/protocols/__init__.py src/ctx_weft/core/runtime.py tests/unit/test_event_blob_store.py
git commit -m "feat(protocols): EventBlobStore 协议 + 显式注册槽（不自动回落到 memory）"
```

---

### Task 2: 入口双写

**Files:**
- Modify: `src/ctx_weft/core/content.py`（`normalize_content` 加 `event_blob_store` 参数）
- Modify: `src/ctx_weft/core/runtime.py`（`_validate_and_normalize_content` 传两个 store）
- Test: `tests/unit/test_event_blob_store.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `get_event_blob_store()`
- Produces: `normalize_content(content, *, blob_store, event_blob_store, ctx)` —— 参数名
  与顺序钉死，Task 4 的测试会按此构造。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_event_blob_store.py`：

```python
import base64

from ctx_weft.core.content import normalize_content
from ctx_weft.protocols import ImagePart, TextPart
from ctx_weft.protocols.memory import NullMemoryBlobStore

_RAW = b"\x89PNG\r\n\x1a\n" + b"payload" * 20
_B64 = base64.b64encode(_RAW).decode("ascii")


class _MemStub(_Stub):
    """与 _Stub 同实现，只为在测试里区分两个 store 实例。"""


async def test_dual_write_yields_one_ref_both_stores_have_it() -> None:
    """内容寻址保证两边 sha 相同，故**只有一个 ref**，两边都取得到。"""
    mem, evt = _MemStub(), _Stub()
    out = await normalize_content(
        [TextPart(text="看图"), ImagePart(data=_B64, media_type="image/png")],
        blob_store=mem, event_blob_store=evt, ctx=_ctx(),
    )
    ref = out[1].data
    assert ref.startswith("blob:")
    assert out[1].source_type == "ref"
    assert out[1].byte_size == len(_RAW)
    assert await mem.get(ref, _ctx()) is not None
    assert await evt.get(ref, _ctx()) is not None, "event 侧也必须有，否则事件流取不回"


async def test_shared_instance_is_idempotent() -> None:
    """host 共用同一实例时第二次 put 幂等命中，零额外成本。"""
    both = _Stub()
    out = await normalize_content(
        [ImagePart(data=_B64, media_type="image/png")],
        blob_store=both, event_blob_store=both, ctx=_ctx(),
    )
    assert len(both.blobs) == 1, "同一份字节只应存一行"
    assert await both.get(out[0].data, _ctx()) is not None


async def test_no_dual_write_when_memory_cannot_externalize() -> None:
    """memory 侧不可外部化时整个函数短路——否则会去调 NullMemoryBlobStore.put 抛错。

    这一组合下事件的 ref 化**不由入口负责**，由 Task 3 的发射点函数独立完成。
    """
    evt = _Stub()
    content = [ImagePart(data=_B64, media_type="image/png")]
    out = await normalize_content(
        content, blob_store=NullMemoryBlobStore(), event_blob_store=evt, ctx=_ctx(),
    )
    assert out is content, "应原样返回同一对象"
    assert evt.blobs == {}, "短路时 event 侧也不该被写"


async def test_plain_text_is_untouched() -> None:
    mem, evt = _MemStub(), _Stub()
    s = "纯文本"
    assert await normalize_content(
        s, blob_store=mem, event_blob_store=evt, ctx=_ctx()) is s
    assert mem.blobs == {} and evt.blobs == {}


async def test_ref_parts_are_not_re_externalized() -> None:
    """已是 ref 的 part 原样保留，不重复 put。"""
    mem, evt = _MemStub(), _Stub()
    part = ImagePart(data="blob:already", media_type="image/png", source_type="ref")
    out = await normalize_content(
        [part], blob_store=mem, event_blob_store=evt, ctx=_ctx())
    assert out[0] is part
    assert mem.blobs == {} and evt.blobs == {}
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py -k dual_write`
Expected: FAIL — `TypeError: normalize_content() got an unexpected keyword argument 'event_blob_store'`

- [ ] **Step 3: 实现**

`core/content.py::normalize_content` 加参数并在**同一循环内**双写：

```python
async def normalize_content(
    content: "str | list[ContentPart] | None",
    *,
    blob_store: "Any",
    event_blob_store: "Any" = None,
    ctx: "Any",
) -> "str | list[ContentPart] | None":
```

循环体里 `put` 之后追加：

```python
        ref = await blob_store.put(raw, media_type, ctx)
        # 双写（spec §5）：同一份字节也存进 event 侧。内容寻址保证两边 sha 相同，
        # 故只有一个 ref，memory 路径与事件流路径各取各的都取得到，读侧因此一行不用改。
        # **必须在同一个循环里**：put 之后 raw bytes 就不再持有，分两趟要么重新
        # b64decode（热路径上不可接受，同 image_byte_size 拒绝解码的理由），要么从
        # store 取回（一次无谓 IO）。
        # 共用同一实例时第二次 put 幂等命中已有行，零额外成本。
        if event_blob_store is not None and event_blob_store.can_externalize:
            await event_blob_store.put(raw, media_type, ctx)
```

⚠️ 函数开头 `if not blob_store.can_externalize: return content` 的短路**保持不变**
——memory 侧不可外部化时整段（含 event 侧的 put）随之短路，否则会去调
`NullMemoryBlobStore.put` 触发 `NotImplementedError`。docstring 里补一句说明这个组合下
事件 ref 化由发射点负责（Task 3）。

`core/runtime.py::_validate_and_normalize_content` 的调用处传上
`event_blob_store=self.providers.get_event_blob_store()`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py`
Expected: 13 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/runtime.py tests/unit/test_event_blob_store.py
git commit -m "feat(content): 入口双写——同一份字节存进两个 blob store，拿同一个 ref"
```

---

### Task 3: `content_to_event_jsonable` + 五个发射点

**Files:**
- Modify: `src/ctx_weft/core/content.py`（+ `content_to_event_jsonable`；− `content_to_jsonable_refs_only`）
- Modify: `src/ctx_weft/core/orchestrator/session_manager.py`（2 处）
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`（3 处）
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`（1 处）
- Modify: `src/ctx_weft/core/control/reducers.py`（session 侧改回 `content_to_jsonable`）
- Modify: `tests/unit/test_session_prompt_refs_only.py`（6 条纯函数用例删除，3 条重放用例改挂新函数）
- Test: `tests/unit/test_event_blob_store.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `EventBlobStore`
- Produces: `async content_to_event_jsonable(content, *, event_blob_store, ctx) -> str | list[dict] | None`

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_event_blob_store.py`：

```python
from ctx_weft.core.content import content_to_event_jsonable


async def test_ref_parts_pass_through_without_put() -> None:
    """入口双写已保证 event store 持有这份字节，不必重复 put。"""
    evt = _Stub()
    out = await content_to_event_jsonable(
        [TextPart(text="看图"),
         ImagePart(data="blob:aaa", media_type="image/png", source_type="ref")],
        event_blob_store=evt, ctx=_ctx(),
    )
    assert out == [
        {"type": "text", "text": "看图"},
        {"type": "image", "data": "blob:aaa", "media_type": "image/png",
         "source_type": "ref"},
    ]
    assert evt.blobs == {}, "ref 已在 store 里，不该重复 put"


async def test_inline_base64_is_externalized_here() -> None:
    """memory 侧无 blob 时入口不外部化，content 里仍是 inline base64——
    只要 event blob 可用，事件侧仍能独立完成 ref 化。这是「所有 base64 变引用」
    在 memory 无 blob 时也成立的关键（spec §6）。
    """
    evt = _Stub()
    out = await content_to_event_jsonable(
        [ImagePart(data=_B64, media_type="image/png")],
        event_blob_store=evt, ctx=_ctx(),
    )
    assert out[0]["source_type"] == "ref"
    assert out[0]["data"].startswith("blob:")
    assert _B64 not in str(out), "事件载荷里绝不能出现字节"
    assert len(evt.blobs) == 1


async def test_plain_text_returns_same_object() -> None:
    evt = _Stub()
    s = "纯文本"
    assert await content_to_event_jsonable(
        s, event_blob_store=evt, ctx=_ctx()) is s
    assert await content_to_event_jsonable(
        None, event_blob_store=evt, ctx=_ctx()) is None


async def test_transitional_helper_is_gone() -> None:
    """`content_to_jsonable_refs_only` 是本设计落地前的过渡实现，应已删除。"""
    import ctx_weft.core.content as c
    assert not hasattr(c, "content_to_jsonable_refs_only")
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py -k event_jsonable or transitional`
Expected: FAIL — `ImportError: cannot import name 'content_to_event_jsonable'`

- [ ] **Step 3: 实现**

**(a)** `core/content.py`：**删掉** `content_to_jsonable_refs_only` 及其 `__all__` 条目，
新增：

```python
async def content_to_event_jsonable(
    content: "str | list[ContentPart] | None",
    *,
    event_blob_store: "Any",
    ctx: "Any",
) -> "str | list[dict] | None":
    """事件载荷专用：**保 ref、绝不落字节**（spec §6）。

    用在五个参与状态重建的事件发射点。规则逐 part 判定：

    - 文本 part → 原样；
    - ``source_type == "ref"`` 的图 → **原样**，不重复 put（入口双写已保证 event store
      持有这份字节）；
    - 其余图（``base64`` / ``url``）→ put 进 event blob → 换成 ref。
      memory 侧无 blob 时入口不外部化、content 里仍是 inline base64，**只要 event blob
      可用，事件侧仍能独立完成 ref 化**——这是「所有 base64 变引用」在 memory 无 blob
      时也成立的关键。

    ⚠️ 与 ``redact_content_for_event`` 的分工：那个产出**一整个 str**（含截断预览），
    用于纯观测事件（`LLM_PROMPT_SENT` / `CAPABILITY_FINISHED`）的调试展示，不可回读；
    本函数产出 **jsonable 结构**，ref 完整、可经 ``content_from_jsonable`` 还原，
    用于参与状态重建的事件。别把两者互换。

    ⚠️ **不读也不写 ``MemoryEvent.blob_refs``**：那是 memory 侧 GC 的 mark 输入，与事件
    载荷是完全不同的载体（spec §6.1）。两者都叫「blob ref」但不相干。

    ``str`` / ``None`` 原样返回同一对象，纯文本路径零成本。
    """
    if content is None or isinstance(content, str):
        return content
    prepared: list[Any] = []
    for part in content:
        if _is_image_part(part) and _part_field(part, "source_type", "base64") == "base64":
            raw = base64.b64decode(str(_part_field(part, "data", "") or ""), validate=True)
            media_type = str(_part_field(part, "media_type", "") or "")
            ref = await event_blob_store.put(raw, media_type, ctx)
            prepared.append(dataclasses.replace(
                part, data=ref, source_type="ref", byte_size=len(raw)))
        else:
            prepared.append(part)
    return content_to_jsonable(prepared)
```

⚠️ `dataclasses.replace` 对 dict 形态的 part 会炸——本函数的入参来自已归一的
`MemoryEvent` / `Task` 字段，应当都是 dataclass。若你在实测中发现 dict 形态能到达这里，
**停下来报告**，不要自己加 dict 分支。

**(b)** 五个发射点改为 `await content_to_event_jsonable(...)`，`event_blob_store` 从
各自能拿到的地方取。**这五处的取法各不相同，请逐个读代码确认**：

- `session_manager.py:80` `:149` —— `SessionManager` 是否持有 registry？若无，需由
  `CtxWeftRuntime` 在构造时注入（参照 `HitlManager.set_content_normalizer` 的既有做法）。
- `task_manager.py:1185`（`_task_payload` 的调用处，**不是** `_task_payload` 内部——
  该函数是同步的且负责十余个与内容无关的字段，async 化会让所有调用方等一次 IO；
  把 `user_prompt` 的外部化提到 `await self._emit(...)` 之前完成再传进去）
- `task_manager.py:578` `:579`（`reopen_task`，已在 async 上下文）
- `hitl_manager.py:394`（已在 async 上下文）

**(c)** `reducers.py:167`：`serialize_view` 是**同步**的，改回
`content_to_jsonable(s.user_prompt)`——view 从事件还原，本就是 ref，无 base64 可外部化。
`:193` `:194` 保持不变。

**(d)** `tests/unit/test_session_prompt_refs_only.py`：删掉针对 `refs_only` 的 6 条纯函数
用例；3 条事件重放用例改挂新路径后保留（它们钉的是「session 事件重放出 parts 与 ref」
这个不随实现变的性质）。**文件可以重命名**为更贴切的名字，若重命名请在报告里说明。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_event_blob_store.py tests/unit/test_session_prompt_refs_only.py`
Expected: 全绿

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py src/ctx_weft/core/orchestrator/ src/ctx_weft/core/control/reducers.py tests/unit/test_event_blob_store.py tests/unit/test_session_prompt_refs_only.py
git commit -m "feat(events): 五个发射点改用 content_to_event_jsonable，事件库恒不含字节"
```

---

### Task 4: 严格默认——携图必须有 EventBlobStore

**Files:**
- Modify: `src/ctx_weft/core/errors.py`（+ `BlobStoreRequiredError`）
- Modify: `src/ctx_weft/core/content.py`（`validate_content` 加第三道门控）
- Modify: `src/ctx_weft/core/runtime.py`（把 event blob store 传进 validate）
- Modify: **7 个测试文件**接桩（见下）
- Modify: `README.md`（迁移说明）
- Test: `tests/unit/test_content_validation.py`（新增反向用例）

**⚠️ 这是破坏性变更**：任何携图会话在未注册 `EventBlobStore` 时直接失败。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_content_validation.py` 追加（**先读该文件既有风格再动手**）：

```python
async def test_image_requires_event_blob_store() -> None:
    """携图会话未注册 EventBlobStore → 入口即拒（spec §7）。

    口径统一：事件库恒不含字节、恒可回读，没有例外分支。
    """
    from ctx_weft.core.errors import BlobStoreRequiredError
    from ctx_weft.protocols.events import NullEventBlobStore

    with pytest.raises(BlobStoreRequiredError):
        validate_content(
            [ImagePart(data=_VALID_B64, media_type="image/png")],
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
```

⚠️ `_VALID_B64` / `_VisionClient` 按该文件既有的桩来；若名字不同，用它实际有的。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_content_validation.py -k event_blob`
Expected: FAIL — `ImportError: cannot import name 'BlobStoreRequiredError'`

- [ ] **Step 3: 实现**

**(a)** `core/errors.py` 加（放在 `VisionNotSupportedError` 之后）：

```python
class BlobStoreRequiredError(CtxWeftError):
    """携图内容要求宿主注册 EventBlobStore（spec 2026-08-27 双 blob store §7）。

    事件库的口径是**恒不含字节、恒可回读**，没有例外分支——没有 event blob store 就
    无处放字节，只能在入口拒绝。纯文本会话不受影响。
    """

    code = "BLOB_STORE_REQUIRED"
```

**(b)** `core/content.py::validate_content` 加参数 `event_blob_store: "Any" = None`，
在 `supports_vision` 门控**之后**追加第三道：

```python
    # 第三道：event blob 门控（spec §7）。放在最后，与前两道同理——畸形/不被支持的
    # 内容不该因为「没有 blob store」而报一个误导性的错。
    # 严格默认：拿不到可外部化的 store 就拒绝。event_blob_store=None 的调用点
    # （未接线的旧调用方）不做此门控，与 llm=None 时不做视觉门控同构。
    if event_blob_store is not None and not event_blob_store.can_externalize:
        raise BlobStoreRequiredError(
            "携带图片的内容需要宿主注册 EventBlobStore（事件库恒不落字节）。"
            "请调用 ProviderRegistry.register_event_blob_store()。"
        )
```

**(c)** `core/runtime.py::_validate_and_normalize_content` 的 `validate_content` 调用处
传上 `event_blob_store=self.providers.get_event_blob_store()`（两个分支都要传）。

**(d)** **7 个测试文件接桩**。实测受影响的（含 `ImagePart` 且经三个入口之一）：

```
tests/integration/test_media_fold_replay_e2e.py
tests/integration/test_multimodal_end_to_end.py
tests/unit/test_content_validation.py
tests/unit/test_hitl_multimodal_validation.py
tests/unit/test_memory_conformance.py
tests/unit/test_multimodal_entry.py
tests/unit/test_normalize_content.py
```

**先跑一遍全量确认实际红的是哪几个**——上面是写计划时的静态分析，实际可能多或少。
给红的那些注册一个 event blob store 桩。**桩放哪里**：优先看这些文件是否已有共用的
runtime/registry fixture 可以扩展；**⚠️ 不得修改 `tests/unit/conftest.py`**（那里有
调用者的未提交改动）——若需要共用 fixture，新建一个模块级 helper 或放在各文件内。

**(e)** `README.md` 加迁移说明，与既有的「升级须知（多模态 Phase 3a）」那条**并列**
（先读那条的写法再照style写）：说明携图会话现在需要 `register_event_blob_store()`，
纯文本不受影响。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_content_validation.py`
Expected: 全绿

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`——**必须回到基线**。若还有红，说明还有测试文件没接桩。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/errors.py src/ctx_weft/core/content.py src/ctx_weft/core/runtime.py README.md tests/unit/test_content_validation.py tests/unit/test_hitl_multimodal_validation.py tests/unit/test_memory_conformance.py tests/unit/test_multimodal_entry.py tests/unit/test_normalize_content.py tests/integration/test_media_fold_replay_e2e.py tests/integration/test_multimodal_end_to_end.py
git commit -m "feat(entry): 携图会话必须注册 EventBlobStore——严格默认，入口即拒"
```

（若实际接桩的文件与上面不同，按实际调整 `git add` 列表；**仍然不要用 `-A`**。）

---

### Task 5: `media:get_image` 条件可见

**Files:**
- Modify: `src/ctx_weft/core/media/capability.py`（`list()` + `describe()`）
- Test: `tests/unit/test_media_get_image.py`（追加）

**Interfaces:**
- Consumes: Task 1 的注册面

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_media_get_image.py`（**先读该文件既有的 registry 构造方式**）：

```python
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
    assert [c.id for c in caps] == ["media:get_image"]
    assert (await provider.describe(_pctx())).capability_count == 1
```

⚠️ `_MemoryBlobStub` / `_pctx` 按该文件既有的桩；工具 id 的确切字符串请读
`MediaCapabilityProvider.capability` 确认，不要照抄我写的。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_media_get_image.py -k hidden`
Expected: FAIL — `assert [ToolCapability(...)] == []`

- [ ] **Step 3: 实现**

`core/media/capability.py`：

```python
    def _blob_available(self) -> bool:
        """memory blob store 是否可用——决定 media:get_image 是否对模型可见。

        用 **memory** 侧判据而非 event 侧：本工具取的是 L0.5 占位里的 ref，而 L0.5 是
        memory 侧的机制（`compact._media_enabled` 用的是同一个判据，保持一致）。

        在**调用时**解析而不是构造时：注册发生在 `Runtime.__init__`，而 host 完全可能
        先构造 Runtime 再 `register_memory()`——构造期判定会让工具永远缺席，即使后来
        接上了 memory（见 `ProviderRegistry.get_memory_blob_store` docstring 记的同款坑）。
        """
        try:
            return bool(self._providers.get_memory_blob_store().can_externalize)
        except Exception:      # registry 尚未接线完毕不该让 list() 炸
            return False

    async def list(self, ctx: ProviderContext) -> list[ToolCapability]:
        # 条件可见（spec §8）：没有可用 blob 时不把工具暴露给模型——它此刻取不回任何
        # 东西（视图里不会有 L0.5 占位），暴露出来只会占 prompt 位置并诱导无效调用。
        # 用 list() 而非 Runtime.__init__ 里条件注册，理由见 _blob_available。
        return [self.capability] if self._blob_available() else []
```

`describe()` 的 `capability_count` 跟着变：`1 if self._blob_available() else 0`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_media_get_image.py`
Expected: 全绿

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/media/capability.py tests/unit/test_media_get_image.py
git commit -m "feat(media): get_image 条件可见——没有可用 blob 时不暴露给模型"
```

---

## Self-Review

**Spec 覆盖：** §0 前提刷新 → 「已核实的实施前提」逐条对应 ✓ · §3 协议 → Task 1 ✓ ·
§4 显式注册不自动回落 → Task 1 的 `test_registry_does_not_fall_back_to_memory_provider` ✓ ·
§5 入口双写（含 5.1 的备选方案否决理由） → Task 2 ✓ · §6 事件外部化 + 五个发射点 →
Task 3 ✓ · §6.1 与 `blob_refs` 无交集 → Task 3 的函数 docstring ✓ · §7 严格默认 + 7.1
破坏性变更 → Task 4 ✓ · §8 条件可见（含 describe→list 的订正） → Task 5 ✓ ·
§9 生命周期归 host → Task 1 的 `EventBlobStore` docstring ✓ · §10 测试矩阵 → 分散在
五个任务的测试里 ✓ · §11 不做什么 → Global Constraints ✓

**类型一致性：** `EventBlobStore` / `NullEventBlobStore` 在 Task 1 定义，Task 2/3/4/5
消费；`normalize_content(..., event_blob_store=...)` 的参数名在 Task 2 定义、Task 4 的
runtime 接线沿用；`content_to_event_jsonable(content, *, event_blob_store, ctx)` 在
Task 3 定义并在同任务的五个发射点消费。

**留给执行者的判断（已在正文标注，不是占位）：**
1. Task 1 — `ProviderRegistry()` 构造签名需先读代码确认。
2. Task 3(b) — 五个发射点各自怎么拿到 event blob store，取法不同，需逐个读代码。
   `SessionManager` 可能需要新的注入路径。
3. Task 4(d) — 实际红的测试文件以跑出来的为准，静态分析的 7 个只是起点；桩放哪里要看
   各文件既有的 fixture，**且不得动 `conftest.py`**。
4. Task 5 — 工具 id 与既有桩名需读文件确认。
