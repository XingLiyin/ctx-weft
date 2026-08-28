# 协议层划界——event 体系入 protocols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 host 必须实现的 event 契约（`Event` / `EventBus` / `EventStore`）从 `core/` 搬进 `protocols/`，并把 `BlobStore` 更名为 `MemoryBlobStore`、`BLOB_REF_PREFIX` 移到 `protocols/context.py`。

**Architecture:** 纯搬迁 + re-export 兼容层。定义移到 `protocols/events.py`，原模块收缩成 re-export，130+ 个既有 import 一个都不改。实现（`InProcessEventBus` / `InMemoryEventStore`）与 core 自用逻辑（`TASK_STATUS_BY_EVENT`）留在 core。改名是纯机械的词边界替换。

**Tech Stack:** Python 3.11 / pytest（asyncio auto 模式，异步测试直接 `async def`，**不加** `@pytest.mark.asyncio`）/ uv

**Spec:** `docs/superpowers/specs/2026-08-27-protocols-layer-event-contracts-design.md`

## Global Constraints

- **本次不改任何行为。** 这是纯搬迁 + 改名，验收标准是全量测试与基线**完全一致**：
  `3 failed / 5 skipped / 0 xfail`。三个既存失败与本工作无关，不得增减、不得试图修：
  - `tests/integration/test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`
  - `tests/unit/test_golden_conformance.py::test_golden_dir_present`
  - `tests/unit/test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`
  任何新出现的红都是真回归，不得以「重构难免」为由放行。
- **既有 import 一个都不改。** 130+ 个调用点（src 37 / tests 99）靠原模块的 re-export
  继续工作。若你发现自己在批量改 `from ctx_weft.core.events import ...`，那是走错了路。
- **划界判据**：host 要实现它或按它编程的 → protocols；core 自用的实现与逻辑 → 留 core。
- **`EventBlobStore` 不在本计划范围内。** 它属于配套特性设计
  （`2026-08-27-dual-blob-store-design.md`），本次只搬既有的东西 + 改名。
- **历史文档不改**：`docs/` 下既往 spec / plan 对当时决定的记述一律保留原样——它们记录的
  是彼时的事实，改写会让可追溯性失效。只改活代码与协议自身的 docstring。
- **`.superpowers/` 目录不动**（那是流程工作区，不是产品代码）。
- 注释与 docstring 用中文，解释「为什么」而非「是什么」。

## 已核实的实施前提（写计划时实测，实施前若发现不符请报告）

- `core/events/types.py` **只 import stdlib**（`dataclasses`/`datetime`/`enum`/`typing`）。
  `TaskStatus` 只在 `TYPE_CHECKING` 下作字符串注解用，运行时不导入 → 搬迁不会把 core
  拖成 protocols 的依赖。**这是整个搬迁成立的前提。**
- `core/events/bus.py` 依赖 stdlib + `core.events.types` 的 `Event` / `EventFilter`。
- `core/state/event_store.py` 依赖 stdlib + `core.events.types` 的 `TRANSIENT_EVENT_TYPES` / `Event`。
- `ctx_weft/__init__.py` 目前只导出 `InMemoryEventStore`（一个实现），`EventStore` 契约
  本身**没有**导出——这是 spec §1 列的问题之一。
- **README 完全没有提及 `BlobStore`**（实测 grep 零命中），改名不涉及 README。

---

## File Structure

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `src/ctx_weft/protocols/events.py` | **新建**。event 领域的全部 host-facing 契约 | Task 1 建、Task 2 扩充 |
| `src/ctx_weft/core/events/types.py` | 收缩为 re-export + `TASK_STATUS_BY_EVENT` | Task 1 |
| `src/ctx_weft/core/events/__init__.py` | re-export 来源改为 protocols | Task 1 |
| `src/ctx_weft/core/events/bus.py` | 收缩为 re-export + `InProcessEventBus` 实现 | Task 2 |
| `src/ctx_weft/core/state/event_store.py` | 收缩为 re-export + `InMemoryEventStore` 实现 | Task 2 |
| `src/ctx_weft/__init__.py` | 补导出 `EventStore` | Task 2 |
| `src/ctx_weft/protocols/context.py` | 接收 `BLOB_REF_PREFIX` | Task 3 |
| `src/ctx_weft/protocols/memory.py` | 交出 `BLOB_REF_PREFIX`；`BlobStore` 改名 | Task 3、4 |
| 约 20 个 src 文件 + 16 个 tests 文件 | 机械改名 | Task 4 |

---

### Task 1: `Event` 数据类型入 `protocols/events.py`

**Files:**
- Create: `src/ctx_weft/protocols/events.py`
- Modify: `src/ctx_weft/core/events/types.py`（收缩为 re-export + `TASK_STATUS_BY_EVENT`）
- Modify: `src/ctx_weft/core/events/__init__.py`（import 来源改为 protocols）
- Test: `tests/unit/test_protocols_events_relocation.py`（新建）

**Interfaces:**
- Produces: `ctx_weft.protocols.events` 模块，导出 `Event` / `EventFilter` / `EventType` /
  `EVENT_TYPES` / `TRANSIENT_EVENT_TYPES`。Task 2 往同一文件追加 `EventBus` / `EventStore`。
- 保持不变：`from ctx_weft.core.events.types import ...` 与 `from ctx_weft.core.events import ...`
  的全部既有用法。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_protocols_events_relocation.py`：

```python
"""event 契约搬进 protocols 之后，两条 import 路径必须指向同一批对象。

spec: docs/superpowers/specs/2026-08-27-protocols-layer-event-contracts-design.md

本文件钉的是「搬迁不改行为」——re-export 必须是同一个对象（`is`），不是同名副本。
同名副本会让 `isinstance` 与 `EventType.X is EventType.X` 在跨路径比较时静默失败。
"""

from __future__ import annotations


def test_event_types_are_the_same_objects() -> None:
    """两条路径拿到的必须是同一个类对象，不是各自定义的同名类。"""
    from ctx_weft.core.events.types import Event as CoreEvent
    from ctx_weft.core.events.types import EventFilter as CoreFilter
    from ctx_weft.core.events.types import EventType as CoreType
    from ctx_weft.protocols.events import Event, EventFilter, EventType

    assert CoreEvent is Event
    assert CoreFilter is EventFilter
    assert CoreType is EventType


def test_event_constant_sets_are_the_same_objects() -> None:
    from ctx_weft.core.events.types import EVENT_TYPES as CORE_TYPES
    from ctx_weft.core.events.types import TRANSIENT_EVENT_TYPES as CORE_TRANSIENT
    from ctx_weft.protocols.events import EVENT_TYPES, TRANSIENT_EVENT_TYPES

    assert CORE_TYPES is EVENT_TYPES
    assert CORE_TRANSIENT is TRANSIENT_EVENT_TYPES


def test_package_level_reexport_still_works() -> None:
    """`from ctx_weft.core.events import X` 是 99 个测试文件在用的路径，不能断。"""
    from ctx_weft.core.events import Event, EventFilter, EventType
    from ctx_weft.protocols.events import Event as PEvent

    assert Event is PEvent
    assert EventFilter is not None and EventType is not None


def test_task_status_map_stays_in_core() -> None:
    """TASK_STATUS_BY_EVENT 是 core 的投影逻辑（依赖 core 的 TaskStatus），不进 protocols。"""
    import ctx_weft.protocols.events as pe
    from ctx_weft.core.events.types import TASK_STATUS_BY_EVENT

    assert TASK_STATUS_BY_EVENT
    assert not hasattr(pe, "TASK_STATUS_BY_EVENT")


def test_protocols_events_does_not_import_core() -> None:
    """层序守卫：protocols 不得依赖 core。

    这条不变量一旦破掉，`protocols/context.py` 那个刻意的惰性绑定就白做了，
    且会在某些 import 顺序下变成真实的循环导入。
    """
    import inspect

    import ctx_weft.protocols.events as pe

    source = inspect.getsource(pe)
    assert "ctx_weft.core" not in source, "protocols/events.py 不得 import core"
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'ctx_weft.protocols.events'`

- [ ] **Step 3: 建 `protocols/events.py`，搬入数据类型**

把 `src/ctx_weft/core/events/types.py` 里的 `Event`、`EventFilter`、`EventType`、
`EVENT_TYPES`、`TRANSIENT_EVENT_TYPES` **整段剪切**到新文件
`src/ctx_weft/protocols/events.py`，连同它们各自的 docstring 与注释**逐字保留**。

新文件头部：

```python
"""Event 领域的 host-facing 契约：事件数据类型 + EventBus + EventStore。

划界判据（spec 2026-08-27-protocols-layer-event-contracts-design §2）：
**host 要实现它或按它编程的进 protocols；core 自用的实现与逻辑留 core。**

故本模块装：`Event` / `EventFilter` / `EventType` 与两个常量集（host 要构造事件、
要持久化、要按类型分派）、`EventBus` 协议（README 明说 host 可换 Redis Streams）、
`EventStore` 协议与 `RunSnapshot`（host 必须实现 append + read_by_session）。

**不装**：`InProcessEventBus` / `InMemoryEventStore`（实现，留 `core/`）、
`TASK_STATUS_BY_EVENT`（core 的投影逻辑，且依赖 core 的 `TaskStatus`）。

⚠️ **本模块不得 import `ctx_weft.core` 的任何东西。** protocols 是比 core 低的层；
反向依赖会让 `protocols/context.py` 那个刻意的惰性绑定失去意义，并在某些 import
顺序下变成真实的循环导入。`tests/unit/test_protocols_events_relocation.py` 钉住这条。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
```

⚠️ 原 `types.py` 的 `TYPE_CHECKING` 块（`from ctx_weft.core.state.models import TaskStatus`）
**不要搬**——它只服务于 `TASK_STATUS_BY_EVENT`，那个留在 core。

然后把 `core/events/types.py` 收缩成：

```python
"""事件类型的 re-export + core 侧的投影映射。

定义已搬到 `ctx_weft.protocols.events`（spec 2026-08-27 协议层划界）。本模块保留
是为了让 130+ 个既有 import 一个都不用改；新代码请直接从 protocols 导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.protocols.events import (
    EVENT_TYPES,
    TRANSIENT_EVENT_TYPES,
    Event,
    EventFilter,
    EventType,
)

if TYPE_CHECKING:
    from ctx_weft.core.state.models import TaskStatus

__all__ = [
    "EVENT_TYPES",
    "TRANSIENT_EVENT_TYPES",
    "Event",
    "EventFilter",
    "EventType",
    "TASK_STATUS_BY_EVENT",
]


# 留在 core 的理由：只被 `core/control/reducers.py` 消费，且值类型 `TaskStatus` 来自
# `core/state/models.py`——它是 core 的投影逻辑，不是 host 要按之编程的契约。
TASK_STATUS_BY_EVENT: dict[EventType, "TaskStatus"] = {
    EventType.TASK_STARTED: "ACTIVE",
    EventType.TASK_SUSPENDED: "SUSPENDED",
    EventType.TASK_FINISHED: "FINISHED",
    EventType.TASK_FAILED: "FAILED",
    EventType.TASK_CANCELED: "CANCELED",
    EventType.TASK_RESUMED: "ACTIVE",
    EventType.TASK_REQUEUED: "PENDING",
}
```

`core/events/__init__.py` 里 `from ctx_weft.core.events.types import (...)` 那段保持不变
——它现在从 re-export 层拿，对象仍是同一批。**本步不要改它。**

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: 5 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`，无新增失败

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/core/events/types.py tests/unit/test_protocols_events_relocation.py
git commit -m "refactor(protocols): Event 数据类型入 protocols/events.py，types.py 收为 re-export"
```

---

### Task 2: `EventBus` 与 `EventStore` 协议入 `protocols/events.py`

**Files:**
- Modify: `src/ctx_weft/protocols/events.py`（追加两个协议 + `RunSnapshot` + `SubscriptionHandle`）
- Modify: `src/ctx_weft/core/events/bus.py`（收缩为 re-export + `InProcessEventBus`）
- Modify: `src/ctx_weft/core/state/event_store.py`（收缩为 re-export + `InMemoryEventStore`）
- Modify: `src/ctx_weft/__init__.py`（补导出 `EventStore`）
- Test: `tests/unit/test_protocols_events_relocation.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `protocols/events.py`
- Produces: `protocols.events` 追加导出 `EventBus` / `SubscriptionHandle` / `EventStore` /
  `RunSnapshot`；顶层 `ctx_weft` 追加导出 `EventStore`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_protocols_events_relocation.py`：

```python
def test_bus_and_store_protocols_are_the_same_objects() -> None:
    from ctx_weft.core.events.bus import EventBus as CoreBus
    from ctx_weft.core.events.bus import SubscriptionHandle as CoreHandle
    from ctx_weft.core.state.event_store import EventStore as CoreStore
    from ctx_weft.core.state.event_store import RunSnapshot as CoreSnapshot
    from ctx_weft.protocols.events import (
        EventBus, EventStore, RunSnapshot, SubscriptionHandle,
    )

    assert CoreBus is EventBus
    assert CoreHandle is SubscriptionHandle
    assert CoreStore is EventStore
    assert CoreSnapshot is RunSnapshot


def test_implementations_stay_in_core() -> None:
    """实现不进 protocols——协议与实现分居是本次划界的全部意义。"""
    import ctx_weft.protocols.events as pe
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.state.event_store import InMemoryEventStore

    assert InProcessEventBus is not None and InMemoryEventStore is not None
    assert not hasattr(pe, "InProcessEventBus")
    assert not hasattr(pe, "InMemoryEventStore")


def test_implementations_still_satisfy_the_relocated_protocols() -> None:
    """re-export 若产生了同名副本，这条会红——runtime_checkable 认的是具体类对象。"""
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.state.event_store import InMemoryEventStore
    from ctx_weft.protocols.events import EventBus, EventStore

    assert isinstance(InProcessEventBus(), EventBus)
    assert isinstance(InMemoryEventStore(), EventStore)


def test_event_store_contract_is_exported_from_package_root() -> None:
    """host 必须实现 EventStore，却一直只能从 core 里 import 它（spec §1）。"""
    import ctx_weft
    from ctx_weft.protocols.events import EventStore

    assert ctx_weft.EventStore is EventStore
    assert "EventStore" in ctx_weft.__all__
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: FAIL — `ImportError: cannot import name 'EventBus' from 'ctx_weft.protocols.events'`

- [ ] **Step 3: 搬两个协议**

**(a)** 把 `core/events/bus.py` 的 `SubscriptionHandle` 与 `EventBus` **整段剪切**到
`protocols/events.py`（docstring 逐字保留），追加到 Task 1 搬入的内容之后。
`protocols/events.py` 的 import 相应补齐：

```python
from abc import abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, runtime_checkable
```

**(b)** 把 `core/state/event_store.py` 的 `RunSnapshot` 与 `EventStore` **整段剪切**到
`protocols/events.py`。

**(c)** `core/events/bus.py` 头部改为：

```python
from ctx_weft.protocols.events import (
    Event, EventBus, EventFilter, SubscriptionHandle,
)
```

并在模块 docstring 里加一句：

```
协议（`EventBus` / `SubscriptionHandle`）已搬到 `ctx_weft.protocols.events`
（spec 2026-08-27 协议层划界）；本模块保留 re-export 以免既有 import 断裂，
自身只留 `InProcessEventBus` 这个进程内实现。
```

保留 `_Subscriber`、`InProcessEventBus`、`_matches` 不动。

**(d)** `core/state/event_store.py` 同法：头部改为

```python
from ctx_weft.protocols.events import TRANSIENT_EVENT_TYPES, Event, EventStore, RunSnapshot
```

模块 docstring 加同样一句（`EventStore` / `RunSnapshot` 已搬走），保留
`_TERMINAL_STATUSES` 与 `InMemoryEventStore` 不动。

**(e)** `src/ctx_weft/__init__.py` 补导出：

```python
from ctx_weft.protocols.events import EventStore
```

并在 `__all__` 里 `"InMemoryEventStore"` 之前加 `"EventStore"`（契约在实现之前）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: 9 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`，无新增失败

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/events.py src/ctx_weft/core/events/bus.py src/ctx_weft/core/state/event_store.py src/ctx_weft/__init__.py tests/unit/test_protocols_events_relocation.py
git commit -m "refactor(protocols): EventBus/EventStore 协议入 protocols，顶层补导出 EventStore"
```

---

### Task 3: `BLOB_REF_PREFIX` 移到 `protocols/context.py`

**Files:**
- Modify: `src/ctx_weft/protocols/context.py`（接收常量）
- Modify: `src/ctx_weft/protocols/memory.py`（交出常量，改为从 context 导入）
- Modify: `src/ctx_weft/protocols/__init__.py`（导出来源改）
- Modify: `src/ctx_weft/core/content.py`、`src/ctx_weft/providers/memory_sql/provider.py`（import 来源改）
- Test: `tests/unit/test_protocols_events_relocation.py`（追加一条）

**Interfaces:**
- Produces: `ctx_weft.protocols.context.BLOB_REF_PREFIX`；`protocols.memory` 与
  `protocols/__init__` 仍可拿到同一个对象。

**为什么移**：`EventBlobStore`（下一份 spec）将与 `MemoryBlobStore` 共用同一个 ref 前缀
——内容寻址的 sha 口径两边必须逐字节一致。若前缀留在 `memory.py`，`events.py` 就要为
一个字符串常量 import 同层的 `memory.py`，在 protocols 内部制造无谓耦合。它属于
`context.py`：`blob:` 是 `ImagePart.source_type == "ref"` 时 `data` 字段的前缀，而
`ImagePart` 就定义在那里。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_protocols_events_relocation.py`：

```python
def test_blob_ref_prefix_lives_in_context() -> None:
    """前缀属于内容形态（ImagePart.data 的前缀），不属于 memory，也不属于 events。

    移动的动机是让两个 blob 协议共用它而不互相 import——见 spec §3。
    """
    from ctx_weft.protocols import BLOB_REF_PREFIX as PkgPrefix
    from ctx_weft.protocols.context import BLOB_REF_PREFIX
    from ctx_weft.protocols.memory import BLOB_REF_PREFIX as MemPrefix

    assert BLOB_REF_PREFIX == "blob:"
    assert MemPrefix is BLOB_REF_PREFIX, "memory 侧仍要能拿到（re-export），且是同一对象"
    assert PkgPrefix is BLOB_REF_PREFIX
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py -k blob_ref_prefix`
Expected: FAIL — `ImportError: cannot import name 'BLOB_REF_PREFIX' from 'ctx_weft.protocols.context'`

- [ ] **Step 3: 移动**

**(a)** `protocols/context.py`：在 `ImagePart` 定义之后、`ContentPart = Union[...]` 之前插入：

```python
# blob ref 的前缀。放在这里而不是某个 blob 协议里：它是**内容形态**的一部分——
# `ImagePart.source_type == "ref"` 时 `data` 字段就长这样——而 `ImagePart` 定义在本模块。
# 两个 blob 协议（memory 侧与 event 侧）共用它，且必须逐字节一致：内容寻址的 sha 口径
# 一旦分叉，同一份字节在两边会得到不同的 ref。放在中立的 context 层，两边各自取，
# 谁也不必 import 对方。
BLOB_REF_PREFIX = "blob:"
```

**(b)** `protocols/memory.py`：删掉自己的 `BLOB_REF_PREFIX = "blob:"` 定义，改为在文件顶部
的 import 里加：

```python
from ctx_weft.protocols.context import (
    BLOB_REF_PREFIX,          # re-export：既有 import 来源不断裂
    ContentPart,
    ProviderContext,
    normalize_content_parts,
)
```

若 `BLOB_REF_PREFIX` 在 `memory.py` 内没有其它使用，ruff 会报 F401——在该 import 行尾加
`# noqa: F401` 并附一句注释说明是**刻意的 re-export**（`protocols/__init__.py` 与既有
调用点仍从这里取）。

**(c)** `protocols/__init__.py`：`BLOB_REF_PREFIX` 的导入来源从 `protocols.memory` 改到
`protocols.context`。`__all__` 里把它从 Blob 分组挪到 Context 分组。

**(d)** `core/content.py` 第 20 行与 `providers/memory_sql/provider.py` 的 import：
来源改为 `ctx_weft.protocols.context`（provider 走的是包级 `from ctx_weft.protocols import`
则不用改——先 grep 确认）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: 10 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`

Run: `uv run ruff check --select I,F,E9 --output-format=concise src/ tests/`
Expected: 无**新增**问题（仓库有既存 I001，不是你引入的，不要修）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/ src/ctx_weft/core/content.py src/ctx_weft/providers/memory_sql/provider.py tests/unit/test_protocols_events_relocation.py
git commit -m "refactor(protocols): BLOB_REF_PREFIX 归 context——两个 blob 协议共用的内容形态常量"
```

---

### Task 4: `BlobStore` → `MemoryBlobStore` 全套改名

**Files:**
- Modify: 约 20 个 `src/` 文件、16 个 `tests/` 文件（下面给精确清单与命令）
- Test: `tests/unit/test_protocols_events_relocation.py`（追加一条）

**Interfaces:**
- Produces: `MemoryBlobStore` / `NullMemoryBlobStore` /
  `ProviderRegistry.register_memory_blob_store` / `.get_memory_blob_store`

**为什么改**：两个 blob 协议并存后（下一份 spec 引入 `EventBlobStore`），`BlobStore`
这个名字不再自明——读者无从判断它指哪一侧。四处改名使两侧对称。

**为什么零破坏**：`register_blob_store` / `get_blob_store` 在 master 上不存在
（`git log master -S register_blob_store -- src/` 零命中），是 `feat/multimodal` 分支
新增、尚未发布的 API。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_protocols_events_relocation.py`：

```python
def test_memory_blob_store_is_renamed() -> None:
    """两个 blob 协议并存后，`BlobStore` 这个名字不再自明——见 spec §6。"""
    import ctx_weft.protocols as p
    from ctx_weft.protocols import MemoryBlobStore, NullMemoryBlobStore

    assert issubclass(NullMemoryBlobStore, MemoryBlobStore)
    assert NullMemoryBlobStore().can_externalize is False
    assert not hasattr(p, "BlobStore"), "旧名不得残留，否则两个名字并存更糊涂"
    assert not hasattr(p, "NullBlobStore")


def test_registry_methods_are_renamed() -> None:
    from ctx_weft.core.runtime import ProviderRegistry

    reg = ProviderRegistry()
    assert hasattr(reg, "register_memory_blob_store")
    assert hasattr(reg, "get_memory_blob_store")
    assert not hasattr(reg, "register_blob_store")
    assert not hasattr(reg, "get_blob_store")
    assert reg.get_memory_blob_store().can_externalize is False
```

⚠️ `ProviderRegistry()` 的构造签名请先读 `core/runtime.py` 确认——若它需要参数，
按实际签名调整这条测试。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py -k rename`
Expected: FAIL — `ImportError: cannot import name 'MemoryBlobStore'`

- [ ] **Step 3: 机械替换**

改名是纯词边界替换，**顺序重要**：先改长的（`NullBlobStore`），再改短的（`BlobStore`），
否则 `NullBlobStore` 会被先替换成 `NullMemoryBlobStore` 之后再被二次替换。

在仓库根目录执行（Git Bash）：

```bash
FILES=$(grep -rlwE "BlobStore|NullBlobStore" --include=*.py src/ tests/; \
        grep -rlE "\b(register|get)_blob_store\b" --include=*.py src/ tests/)
FILES=$(echo "$FILES" | sort -u)
echo "$FILES"          # 先看清单，确认没有 .superpowers/ 或 docs/ 混入
for f in $FILES; do
  sed -i \
    -e 's/\bNullBlobStore\b/NullMemoryBlobStore/g' \
    -e 's/\bBlobStore\b/MemoryBlobStore/g' \
    -e 's/\bregister_blob_store\b/register_memory_blob_store/g' \
    -e 's/\bget_blob_store\b/get_memory_blob_store/g' \
    "$f"
done
```

⚠️ **执行后必须人工核对三处**，`sed` 不认语义：

1. `grep -rnw "MemoryMemoryBlobStore" src/ tests/` → 必须**零命中**（二次替换的信号）。
2. `grep -rnw "BlobStore" src/ tests/` → 必须零命中（漏网的旧名）。
3. `git diff --stat` → 确认只有 `src/` 与 `tests/` 下的 `.py`，**没有** `docs/` 或
   `.superpowers/`（历史文档不改，见 Global Constraints）。

然后修两处**语义**而非名字的文字（`sed` 改不了）：

- `src/ctx_weft/core/media/__init__.py` 的模块 docstring 里
  「`MemoryBlobStore` 协议归 `protocols/memory`（字节归 memory，裁定 D4）」——
  这句现在仍准确，确认即可，不必改。
- `src/ctx_weft/protocols/memory.py` 里 `MemoryBlobStore` 的类 docstring 提到
  「与 `protocols.filesystem.SpillSink` 同形」——仍准确，不必改。

若你在 diff 里发现某处改名后读起来不通顺（例如一句话变成「MemoryBlobStore 存储」这类
重复），就地修顺，并在报告里列出你改了哪些句子。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_protocols_events_relocation.py`
Expected: 12 passed

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`，无新增失败

Run: `uv run ruff check --select I,F,E9 --output-format=concise src/ tests/`
Expected: 无新增问题

- [ ] **Step 5: 提交**

```bash
git add src/ tests/
git commit -m "refactor(protocols): BlobStore 更名 MemoryBlobStore，与将来的 EventBlobStore 对称"
```

---

## Self-Review

**Spec 覆盖：** §2 划界表 → Task 1（数据类型）+ Task 2（两个协议）；留 core 的三项由
Task 1 的 `test_task_status_map_stays_in_core` 与 Task 2 的 `test_implementations_stay_in_core`
钉住 ✓ · §2.1 无反向依赖 → Task 1 的 `test_protocols_events_does_not_import_core` ✓ ·
§2.2 单文件落点 → Task 1 建、Task 2 扩充 ✓ · §3 `BLOB_REF_PREFIX` → Task 3 ✓ ·
§4 依赖方向 → Task 1 的层序守卫 ✓ · §5 re-export 兼容 → 每个任务的 `is` 断言 +
全量基线 ✓ · §5.1 验收标准 → Global Constraints + 每任务 Step 4 ✓ · §6 改名 → Task 4 ✓ ·
§6.1 历史文档不改 → Global Constraints + Task 4 Step 3 的核对项 3 ✓ ·
§7 实施顺序 → 本计划是前置，双 blob store 另出计划 ✓ · §8 不做什么 → Global Constraints
（`EventBlobStore` 不在范围）+ 未安排任何触及 `TASK_STATUS_BY_EVENT` 归属、
两个同名 `InMemoryEventStore`、既有 import 语句的步骤 ✓

**类型一致性：** `protocols/events.py` 在 Task 1 建、Task 2 追加，两任务的测试都用
`from ctx_weft.protocols.events import ...`，模块路径一致；Task 4 的新名
`MemoryBlobStore` / `NullMemoryBlobStore` / `register_memory_blob_store` /
`get_memory_blob_store` 在测试与 sed 命令中拼写一致。

**留给执行者的两处判断**（已在正文标注，不是占位）：
1. Task 3(d) —— `provider.py` 走包级 import 则无需改，需先 grep 确认。
2. Task 4 Step 1 —— `ProviderRegistry()` 的构造签名需先读代码确认。
