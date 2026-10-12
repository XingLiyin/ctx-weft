# providers 目录重组 + SQL EventStore + blob 归属收口 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `providers/` 按「领域 → 协议 → 变体」重组，补齐 `EventStore` 的 SQL 实现与两个订阅者，并把 blob 字节从 RDBMS 移出、只在 SQL 侧保留引用边。

**Architecture:** 三段推进。① 纯搬运的目录迁移（`git mv` + 机械 sed，零逻辑改动）；② blob 归属——删 `SqlMemoryProvider` 的字节存储、补 `live_blob_refs()` 让宿主能喂给 `FsBlobStore.collect()`、registry 解析退回两级；③ events 新增件——`EventPersister` / `SnapshotWriter` / `SqlEventStore`，其中 `list_active_session_ids` 的活跃判据抽成两个 store 共用的状态机，口径分叉在结构上不可能发生。

**Tech Stack:** Python 3.11+ / asyncio / SQLAlchemy 2.0 async（可选依赖 `[sql]`，SQLite 后端另需 aiosqlite）/ pytest + pytest-asyncio（`asyncio_mode = auto`）/ hatchling。

**Spec:** `docs/superpowers/specs/2026-08-29-providers-layout-and-sql-event-store-design.md`

## Global Constraints

- **纯文本 / 无图会话逐字节不受影响。** 本次改动不得改变任何纯文本路径的行为。
- **不动 `protocols/` 里的协议定义。** `MemoryBlobStore` / `EventBlobStore` / `EventStore` / `EventBus` 的方法签名一个不改（docstring 可改，见 Task 6）。
- **不留兼容 shim。** 旧 provider 路径直接消失（spec §7）。`core/state/event_store.py` 与 `core/events/*` 的**现有** shim 保留，只改内部指向。
- **`sql` 子包不被任何上层包 eager import。** `import ctx_weft` / `import ctx_weft.providers` / `import ctx_weft.providers.memory` / `import ctx_weft.providers.events` 在缺 sqlalchemy 时必须照常工作。
- **目录迁移与功能改动分开 commit**（spec §9 风险 3）。
- 测试命令一律 `uv run pytest`。全量回归：`uv run pytest -q`。
- 提交信息用中文、遵循仓内 conventional commits 风格（`feat(scope):` / `refactor(scope):` / `docs(scope):`）。

---

## File Structure

**新建：**

| 路径 | 职责 |
|---|---|
| `src/ctx_weft/providers/_sqlalchemy.py` | 两个 SQL 包共用的 `UtcDateTime` 与 `make_session_factory`。私有，不进 `providers/__init__.py` |
| `src/ctx_weft/providers/events/_lifecycle.py` | 会话活跃状态机（`TERMINAL_STATUSES` / `LIFECYCLE_EVENT_TYPES` / `apply_lifecycle` / `replay_lifecycle`）。两个 store 共用 |
| `src/ctx_weft/providers/events/persister.py` | `EventPersister` + `attach_persistence` |
| `src/ctx_weft/providers/events/snapshot.py` | `SnapshotWriter` |
| `src/ctx_weft/providers/events/store/sql/models.py` | `Base` / `EventModel` / `SnapshotModel` |
| `src/ctx_weft/providers/events/store/sql/store.py` | `SqlEventStore` + `open_sqlite_event_store` |
| `tests/unit/test_event_store_conformance.py` | EventStore 协议一致性套，参数化跑两个实现 |
| `tests/unit/test_event_persistence_wiring.py` | `EventPersister` / `SnapshotWriter` / `attach_persistence` 单测 |
| `tests/unit/test_blob_gc_integration.py` | `live_blob_refs()` × `FsBlobStore.collect()` 联动 GC |

**移动（`git mv`，内容除 import 外不改）：**

| 旧 | 新 |
|---|---|
| `providers/memory_blackboard/in_memory.py` | `providers/memory/in_memory/provider.py` |
| `providers/memory_sql/*` | `providers/memory/sql/*` |
| `providers/events/bus.py` | `providers/events/bus/in_process/bus.py` |
| `providers/events/store.py` | `providers/events/store/in_memory/store.py` |
| `providers/blob_fs/store.py` | `providers/blob/fs/store.py` |

**改：**

| 路径 | 改什么 |
|---|---|
| `providers/memory/sql/models.py` | 删 `MemoryBlobModel`；`UtcDateTime` 改从 `_sqlalchemy` 引 |
| `providers/memory/sql/provider.py` | 删 `put`/`get`/`collect_blobs`/两个 blob 基类；补 `live_blob_refs()` |
| `providers/events/store/in_memory/store.py` | 去自订阅、去瞬态过滤、改用共用状态机 |
| `core/runtime.py` | `get_memory_blob_store()` 退回两级；event store 接线改用 `EventPersister` |
| `protocols/memory.py` | `MemoryBlobStore` docstring 口径订正 |
| `README.md` | 升级须知 |
| `docs/host-migration-to-sql-memory.md` | 按新接线重写 |

---

## Task 1: memory/ 目录重组

**Files:**
- Move: `src/ctx_weft/providers/memory_blackboard/in_memory.py` → `src/ctx_weft/providers/memory/in_memory/provider.py`
- Move: `src/ctx_weft/providers/memory_sql/{__init__,models,provider}.py` → `src/ctx_weft/providers/memory/sql/`
- Create: `src/ctx_weft/providers/memory/__init__.py`, `src/ctx_weft/providers/memory/in_memory/__init__.py`
- Delete: `src/ctx_weft/providers/memory_blackboard/__init__.py`
- Test: 全量回归（无新测试；本任务是纯搬运）

**Interfaces:**
- Consumes: 无
- Produces: `ctx_weft.providers.memory.InMemoryMemoryProvider`；`ctx_weft.providers.memory.sql.{SqlMemoryProvider, make_session_factory, open_sqlite_memory, normalize_tenant, Base, MemoryEventModel, MemoryBlobModel, MemoryBlobRefModel, MemorySubscriptionModel}`

- [ ] **Step 1: 建目录并 git mv**

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p src/ctx_weft/providers/memory/in_memory
git mv src/ctx_weft/providers/memory_sql src/ctx_weft/providers/memory/sql
git mv src/ctx_weft/providers/memory_blackboard/in_memory.py src/ctx_weft/providers/memory/in_memory/provider.py
git rm -q src/ctx_weft/providers/memory_blackboard/__init__.py
rm -rf src/ctx_weft/providers/memory_blackboard
```

- [ ] **Step 2: 写两个新 `__init__.py`**

`src/ctx_weft/providers/memory/in_memory/__init__.py`：

```python
"""纯内存 MemoryProvider——测试 / 单进程 demo 用。

**不实现 `MemoryBlobStore`**（裁定 D6）：不接 blob 的宿主行为与改造前逐字节一致。
"""

from ctx_weft.providers.memory.in_memory.provider import InMemoryMemoryProvider

__all__ = ["InMemoryMemoryProvider"]
```

`src/ctx_weft/providers/memory/__init__.py`：

```python
"""MemoryProvider 的实现们：`in_memory`（纯内存）与 `sql`（SQLAlchemy async）。

**本模块只 re-export 无可选依赖的实现。** `sql` 子包需要 ``sqlalchemy>=2.0``
（SQLite 后端另需 ``aiosqlite``），装法 ``pip install ctx-weft[sql]``，
故**刻意不在这里 re-export、也不做 PEP 562 惰性 `__getattr__`**——让路径本身说明
依赖边界：``from ctx_weft.providers.memory.sql import SqlMemoryProvider`` 一眼看出
要 extras。惰性绑定会让 ``providers.memory.SqlMemoryProvider`` 这个名字看起来存在、
访问时才炸。

不变量：`import ctx_weft.providers.memory` 在缺 sqlalchemy 时照常工作。
"""

from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

__all__ = ["InMemoryMemoryProvider"]
```

- [ ] **Step 3: 改 `memory/sql/__init__.py` 的内部 import 路径**

把该文件里的 `ctx_weft.providers.memory_sql.models` / `.provider` 全部改成 `ctx_weft.providers.memory.sql.models` / `.provider`。docstring 里的包名一并更新。

```bash
sed -i 's/ctx_weft\.providers\.memory_sql/ctx_weft.providers.memory.sql/g' \
  src/ctx_weft/providers/memory/sql/__init__.py \
  src/ctx_weft/providers/memory/sql/models.py \
  src/ctx_weft/providers/memory/sql/provider.py
```

- [ ] **Step 4: 全仓机械替换旧路径**

```bash
# in_memory：模块路径变了（in_memory.py → in_memory/provider.py），两种写法都要覆盖
grep -rl "providers\.memory_blackboard" src/ tests/ docs/ README.md ARCHITECTURE.md 2>/dev/null \
  | xargs sed -i \
    -e 's/ctx_weft\.providers\.memory_blackboard\.in_memory/ctx_weft.providers.memory.in_memory/g' \
    -e 's/ctx_weft\.providers\.memory_blackboard/ctx_weft.providers.memory.in_memory/g'
# sql
grep -rl "providers\.memory_sql\|providers/memory_sql" src/ tests/ docs/ README.md ARCHITECTURE.md 2>/dev/null \
  | xargs sed -i \
    -e 's/ctx_weft\.providers\.memory_sql/ctx_weft.providers.memory.sql/g' \
    -e 's#providers/memory_sql#providers/memory/sql#g'
```

- [ ] **Step 5: 清掉残留并核对**

```bash
grep -rn "memory_blackboard\|memory_sql" src/ tests/ README.md ARCHITECTURE.md --include=*.py --include=*.md
```
Expected: 无输出。若 `docs/superpowers/plans/` 下的历史计划文档仍有旧名，**不要改**——那是历史记录，改了会篡改当时的事实。上面的 grep 刻意不含 `docs/`。

- [ ] **Step 6: 删掉过期的 docstring 断言**

`src/ctx_weft/providers/memory/in_memory/provider.py` 的模块 docstring 若提到 `StructuredBlackboardMemoryProvider`，删掉那句——该类在代码里不存在。

- [ ] **Step 7: 全量回归**

Run: `uv run pytest -q`
Expected: 与迁移前完全相同的通过数，0 失败。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(providers): memory 实现收进 providers/memory/{in_memory,sql}

memory_blackboard 与 memory_sql 是同一个 MemoryProvider 协议的两个实现，平铺在
providers/ 下看不出同源。顺带正名：memory_blackboard 的 docstring 声称有
StructuredBlackboardMemoryProvider，该类在代码里并不存在。

纯搬运，无逻辑改动。sql 子包仍不被任何上层包 eager import。"
```

---

## Task 2: events/ 目录重组

**Files:**
- Move: `src/ctx_weft/providers/events/bus.py` → `src/ctx_weft/providers/events/bus/in_process/bus.py`
- Move: `src/ctx_weft/providers/events/store.py` → `src/ctx_weft/providers/events/store/in_memory/store.py`
- Create: `providers/events/bus/__init__.py`, `providers/events/bus/in_process/__init__.py`, `providers/events/store/__init__.py`, `providers/events/store/in_memory/__init__.py`
- Modify: `src/ctx_weft/providers/events/__init__.py`, `src/ctx_weft/core/events/bus.py`, `src/ctx_weft/core/state/event_store.py`

**Interfaces:**
- Consumes: 无
- Produces: `ctx_weft.providers.events.{InProcessEventBus, InMemoryEventStore}`（顶层名字不变）；新增可直达路径 `ctx_weft.providers.events.bus.in_process.InProcessEventBus`、`ctx_weft.providers.events.store.in_memory.InMemoryEventStore`

- [ ] **Step 1: 建目录并 git mv**

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p src/ctx_weft/providers/events/bus/in_process
mkdir -p src/ctx_weft/providers/events/store/in_memory
git mv src/ctx_weft/providers/events/bus.py src/ctx_weft/providers/events/bus/in_process/bus.py
git mv src/ctx_weft/providers/events/store.py src/ctx_weft/providers/events/store/in_memory/store.py
```

⚠️ `bus.py` → `bus/` 是「文件变同名目录」。若 `git mv` 报路径冲突，先 `git mv providers/events/bus.py providers/events/_bus_tmp.py`，建目录后再 `git mv providers/events/_bus_tmp.py providers/events/bus/in_process/bus.py`。

- [ ] **Step 2: 写四个新 `__init__.py`**

`providers/events/bus/in_process/__init__.py`：

```python
"""EventBus 的进程内实现。"""

from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus

__all__ = ["InProcessEventBus"]
```

`providers/events/bus/__init__.py`：

```python
"""EventBus 协议的实现们。当前只有 in_process；多进程部署换 Redis Streams 等外部总线。"""

from ctx_weft.providers.events.bus.in_process import InProcessEventBus

__all__ = ["InProcessEventBus"]
```

`providers/events/store/in_memory/__init__.py`：

```python
"""EventStore 的单进程内存实现。"""

from ctx_weft.providers.events.store.in_memory.store import InMemoryEventStore

__all__ = ["InMemoryEventStore"]
```

`providers/events/store/__init__.py`：

```python
"""EventStore 协议的实现们：`in_memory`（开发/测试）与 `sql`（持久化）。

**本模块只 re-export 无可选依赖的实现**——`sql` 子包需要 ``ctx-weft[sql]``，
理由与 `providers/memory/__init__.py` 同：让路径本身说明依赖边界。
"""

from ctx_weft.providers.events.store.in_memory import InMemoryEventStore

__all__ = ["InMemoryEventStore"]
```

- [ ] **Step 3: 改 `providers/events/__init__.py`**

```python
"""event 体系的内置实现。

目录按**协议**分层，而不是把所有实现平铺：`events/` 下装着两个不同的协议——
`EventBus`（传输/扇出）与 `EventStore`（持久化）。不分层就会把 `in_process`
（一个 bus）与 `in_memory` / `sql`（两个 store）摆成平级三兄弟，读起来像同一个
东西的三个变体。

    bus/in_process/     ← InProcessEventBus
    store/in_memory/    ← InMemoryEventStore
    store/sql/          ← SqlEventStore（需 ctx-weft[sql]，故不在此 re-export）

本模块只 re-export 无可选依赖的名字。
"""

from ctx_weft.providers.events.bus import InProcessEventBus
from ctx_weft.providers.events.store import InMemoryEventStore

__all__ = ["InProcessEventBus", "InMemoryEventStore"]
```

- [ ] **Step 4: 改两处现有 shim 的内部指向**

`src/ctx_weft/core/events/bus.py` 与 `src/ctx_weft/core/state/event_store.py` 里的
`from ctx_weft.providers.events.bus import InProcessEventBus` /
`from ctx_weft.providers.events.store import InMemoryEventStore` 现在仍然成立
（新的 `bus/__init__.py` 与 `store/__init__.py` 都导出了同名符号），**无需改动**。
执行这一步只做确认：

```bash
grep -rn "providers\.events" src/ctx_weft/core/
```
逐条确认每个 import 的符号仍能从新路径解析。

- [ ] **Step 5: 回归**

Run: `uv run pytest -q`
Expected: 与迁移前相同，0 失败。

Run: `uv run python -c "import ctx_weft.providers.events as m; print(m.InProcessEventBus, m.InMemoryEventStore)"`
Expected: 打印两个类，无异常。

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(providers): events 按协议分层为 bus/ 与 store/

events/ 下装着两个不同的协议（EventBus 传输、EventStore 持久化）。原先 bus.py 与
store.py 平铺，加上待新增的 sql store 会变成 in_process / in_memory / sql 三个
平级目录——读起来像同一个东西的三个变体，实际是 1 个 bus + 2 个 store。

顶层 providers.events 的导出名不变，纯搬运。"
```

---

## Task 3: blob/ 目录重组

**Files:**
- Move: `src/ctx_weft/providers/blob_fs/store.py` → `src/ctx_weft/providers/blob/fs/store.py`
- Create: `providers/blob/__init__.py`, `providers/blob/fs/__init__.py`
- Delete: `providers/blob_fs/__init__.py`

**Interfaces:**
- Consumes: 无
- Produces: `ctx_weft.providers.blob.fs.FsBlobStore`（同时也从 `ctx_weft.providers.blob` 导出）

- [ ] **Step 1: 建目录并 git mv**

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p src/ctx_weft/providers/blob/fs
git mv src/ctx_weft/providers/blob_fs/store.py src/ctx_weft/providers/blob/fs/store.py
git rm -q src/ctx_weft/providers/blob_fs/__init__.py
rm -rf src/ctx_weft/providers/blob_fs
```

- [ ] **Step 2: 写两个 `__init__.py`**

`providers/blob/fs/__init__.py`：

```python
"""文件系统内容寻址 blob 实现。"""

from ctx_weft.providers.blob.fs.store import FsBlobStore

__all__ = ["FsBlobStore"]
```

`providers/blob/__init__.py`：

```python
"""blob 存储的实现们。

`FsBlobStore` **同时**满足 `MemoryBlobStore` 与 `EventBlobStore` 两个契约——这是
「实现可以偷懒」的形态，不等于两个契约可以合并（它们各自定义、类型无关，回收锚点
不同：memory 侧是记录 is_superseded，event 侧是事件保留策略）。共用一个实例时
`collect()` 的 live_refs 必须同时含两侧的活引用，见该类 docstring。
"""

from ctx_weft.providers.blob.fs import FsBlobStore

__all__ = ["FsBlobStore"]
```

- [ ] **Step 3: 全仓替换**

```bash
grep -rl "providers\.blob_fs\|providers/blob_fs" src/ tests/ README.md ARCHITECTURE.md 2>/dev/null \
  | xargs sed -i -e 's/ctx_weft\.providers\.blob_fs/ctx_weft.providers.blob.fs/g' \
                 -e 's#providers/blob_fs#providers/blob/fs#g'
grep -rn "blob_fs" src/ tests/ README.md ARCHITECTURE.md --include=*.py --include=*.md
```
Expected: 无输出。

- [ ] **Step 4: 回归**

Run: `uv run pytest tests/unit/test_blob_fs_store.py -q`
Expected: 9 passed。

Run: `uv run pytest -q`
Expected: 0 失败。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(providers): blob_fs 收进 providers/blob/fs

与 memory/ events/ 同一命名风格：领域一个目录，内部按变体分包。纯搬运。"
```

---

## Task 4: 抽出 `providers/_sqlalchemy.py`

**Files:**
- Create: `src/ctx_weft/providers/_sqlalchemy.py`
- Modify: `src/ctx_weft/providers/memory/sql/models.py`（删 `UtcDateTime` 定义，改为引入）
- Modify: `src/ctx_weft/providers/memory/sql/provider.py`（删 `make_session_factory` 定义，改为引入并 re-export）
- Test: `tests/unit/test_sql_shared_types.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `ctx_weft.providers._sqlalchemy.UtcDateTime`（`TypeDecorator`，`impl = DateTime(timezone=True)`，`cache_ok = True`）
  - `ctx_weft.providers._sqlalchemy.make_session_factory(url: str, **engine_kwargs) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]`
  - `ctx_weft.providers.memory.sql.make_session_factory` 仍可导入（re-export，签名不变）

- [ ] **Step 1: 写失败的测试**

`tests/unit/test_sql_shared_types.py`：

```python
"""providers/_sqlalchemy.py：两个 SQL 包共用的类型与工厂。

这里只钉两件事：UtcDateTime 的时区保真（SQLite 会丢 tzinfo），以及共享模块不被
上层包 eager import（sqlalchemy 是可选依赖）。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ctx_weft.providers._sqlalchemy import UtcDateTime, make_session_factory


def test_utc_datetime_bind_converts_aware_to_utc():
    td = UtcDateTime()
    shanghai = timezone(timedelta(hours=8))
    got = td.process_bind_param(datetime(2026, 8, 29, 20, 0, tzinfo=shanghai), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_bind_treats_naive_as_utc():
    td = UtcDateTime()
    got = td.process_bind_param(datetime(2026, 8, 29, 12, 0), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_result_restores_tzinfo():
    """SQLite 回读得到 naive datetime——不补 tzinfo 的话等值比较恒 False，
    而排序照常工作，所以这个丢失只会在等值断言上炸一条。"""
    td = UtcDateTime()
    got = td.process_result_value(datetime(2026, 8, 29, 12, 0), None)
    assert got == datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_utc_datetime_handles_none():
    td = UtcDateTime()
    assert td.process_bind_param(None, None) is None
    assert td.process_result_value(None, None) is None


def test_make_session_factory_returns_engine_and_factory(tmp_path):
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    assert engine is not None
    assert callable(factory)


def test_shared_module_not_eager_imported_by_providers():
    """providers/__init__.py 不得拉 sqlalchemy——缺可选依赖的宿主要能 import providers。"""
    mod = importlib.import_module("ctx_weft.providers")
    assert not hasattr(mod, "UtcDateTime")
    assert not hasattr(mod, "make_session_factory")
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_sql_shared_types.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ctx_weft.providers._sqlalchemy'`

- [ ] **Step 3: 建 `providers/_sqlalchemy.py`**

把 `providers/memory/sql/models.py` 里 `UtcDateTime` 的**整个类连同 docstring 原样搬过来**，再把 `provider.py` 里的 `make_session_factory` 搬过来：

```python
"""两个 SQL provider 包（`memory/sql` 与 `events/store/sql`）共用的 SQLAlchemy 基建。

私有模块，**不进 `providers/__init__.py`**——`providers/` 下的可选依赖边界靠「谁也不
eager import 它」维持：`import ctx_weft.providers` 在缺 sqlalchemy 时必须照常工作。
先例见 `providers/_encoding.py` / `_tooldecl.py` / `_script_runner.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

__all__ = ["UtcDateTime", "make_session_factory"]


class UtcDateTime(TypeDecorator):
    """时区保真的 DateTime：入库转 UTC，出库补回 ``tzinfo=UTC``。

    为什么必须自己包一层：**SQLite 没有时区类型**。SQLAlchemy 的 SQLite 方言把
    aware datetime 的 tzinfo 直接丢掉、回读得到 naive datetime——于是
    ``rec.timestamp == 写入时的 aware datetime`` 恒为 False（naive 与 aware 不相等），
    而排序又照常工作，所以这个丢失**不会**在任何排序类断言上暴露，只会在等值比较上
    炸一条。postgres 侧 `TIMESTAMP WITH TIME ZONE` 本就保真，本装饰器在那边是恒等
    变换（bind 时值已是 UTC aware，result 时已带 tzinfo 不再补）。

    naive 输入按 UTC 解释（仓内事件与 memory 记录的时间戳统一来自 ``datetime.now(UTC)``）。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def make_session_factory(url: str, **engine_kwargs: Any) -> Any:
    """``(engine, session_factory)``。url 例：``sqlite+aiosqlite:///path/app.db``。

    两个 SQL 包共用同一个 engine 是受支持的部署形态（单文件 SQLite）——各自的
    ``Base.metadata.create_all`` 跑两次即可，`Base` 刻意不共用（见各包 models 的注释）。
    """
    engine = create_async_engine(url, **engine_kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 4: 改 `memory/sql/models.py` 引入共享类型**

删掉整个 `UtcDateTime` 类定义，并从 sqlalchemy 的 import 列表里删掉 `DateTime` 与 `TypeDecorator`（确认没有别处用到）。新增：

```python
from ctx_weft.providers._sqlalchemy import UtcDateTime
```

- [ ] **Step 5: 改 `memory/sql/provider.py` 引入并 re-export**

删掉 `make_session_factory` 的函数定义，改为在文件顶部 import：

```python
from ctx_weft.providers._sqlalchemy import make_session_factory
```

并在模块的 `__all__`（若有）与 `memory/sql/__init__.py` 里保持 `make_session_factory` 可导入——**既有 API 不能断**。同时删掉 `provider.py` 中因此不再使用的 `create_async_engine` / `async_sessionmaker` import（若 `open_sqlite_memory` 之外无其他用途）。

- [ ] **Step 6: 运行测试**

Run: `uv run pytest tests/unit/test_sql_shared_types.py -q`
Expected: 6 passed

Run: `uv run pytest tests/unit/test_memory_conformance.py tests/unit/test_sql_blob_store.py -q`
Expected: 全绿（本任务不改行为）

- [ ] **Step 7: 确认可选依赖边界没破**

Run: `uv run python -c "import ctx_weft, ctx_weft.providers, ctx_weft.providers.memory, ctx_weft.providers.events; print('ok')"`
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(providers): 抽出 _sqlalchemy 共享 UtcDateTime 与 make_session_factory

events 的 SQL store 要用同一个时区保真装饰器（SQLite 丢 tzinfo 这个坑两边一样）。
私有模块，不进 providers/__init__.py，可选依赖边界不变。"
```

---

## Task 5: SqlMemoryProvider 去 blob 字节存储、补 live_blob_refs

**Files:**
- Modify: `src/ctx_weft/providers/memory/sql/models.py`（删 `MemoryBlobModel`）
- Modify: `src/ctx_weft/providers/memory/sql/provider.py`（删 `put`/`get`/`collect_blobs`/两个 blob 基类；加 `live_blob_refs`）
- Modify: `src/ctx_weft/providers/memory/sql/__init__.py`（导出列表去掉 `MemoryBlobModel`）
- Modify: `tests/unit/test_sql_blob_store.py`（重写）
- Modify: `tests/unit/test_memory_conformance.py`（`_supports_blobs` 探测相关用例）

**Interfaces:**
- Consumes: Task 4 的 `providers._sqlalchemy`
- Produces: `SqlMemoryProvider.live_blob_refs() -> set[str]`（元素形如 `"blob:<sha64>"`，跨全部租户）
- 移除：`SqlMemoryProvider.put` / `.get` / `.collect_blobs`；`SqlMemoryProvider` 不再是 `MemoryBlobStore` / `EventBlobStore` 的实例

- [ ] **Step 1: 写失败的测试**

把 `tests/unit/test_sql_blob_store.py` 整个替换为下面这份（文件改名为 `tests/unit/test_sql_blob_refs.py`，旧文件 `git rm`）：

```python
"""SqlMemoryProvider 的 blob **引用边**（字节已不在 RDBMS，见
docs/superpowers/specs/2026-08-29-providers-layout-and-sql-event-store-design.md §5）。

本 provider 只负责两件事：ingest 时把引用边与事件行写进同一个事务；把活引用吐出来
（live_blob_refs）供宿主喂给 FsBlobStore.collect() 做 mark-sweep 的 mark 输入。
字节的存取与回收都归 blob store，本文件不测那些。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from ctx_weft.protocols import (
    EventBlobStore,
    MemoryAddress,
    MemoryBlobStore,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.context import ImagePart, TextPart
from ctx_weft.providers.memory.sql import SqlMemoryProvider, open_sqlite_memory

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_REF_A = f"blob:{_SHA_A}"
_REF_B = f"blob:{_SHA_B}"


@asynccontextmanager
async def _provider(tmp_path):
    async with open_sqlite_memory(tmp_path / "mem.db") as m:
        yield m


def _ctx(tenant: str = "default") -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id=tenant)


def _event(ref: str | None = None, *, blob_refs: list[str] | None = None) -> MemoryEvent:
    content = [TextPart(text="hi")]
    if ref is not None:
        content.append(ImagePart(data=ref, media_type="image/png", source_type="ref"))
    return MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        content=content,
        timestamp=datetime.now(UTC),
        role="user",
        blob_refs=list(blob_refs or []),
    )


async def test_provider_is_no_longer_a_blob_store(tmp_path):
    """字节离开 RDBMS：本 provider 不再实现任何 blob 协议（spec §5.2）。"""
    async with _provider(tmp_path) as m:
        assert not isinstance(m, MemoryBlobStore)
        assert not isinstance(m, EventBlobStore)
        assert not hasattr(m, "put")
        assert not hasattr(m, "collect_blobs")


async def test_live_blob_refs_collects_structured_ref_parts(tmp_path):
    async with _provider(tmp_path) as m:
        await m.ingest(_event(_REF_A), _ctx())
        assert await m.live_blob_refs() == {_REF_A}


async def test_live_blob_refs_collects_declared_refs(tmp_path):
    """L0.5 降级把 ImagePart(ref) 换成文本占位，ref 只能靠 blob_refs 声明传递。"""
    async with _provider(tmp_path) as m:
        await m.ingest(_event(None, blob_refs=[_REF_B]), _ctx())
        assert await m.live_blob_refs() == {_REF_B}


async def test_live_blob_refs_drops_superseded(tmp_path):
    """被 fold 掉的记录不再构成活引用——这正是 GC 的 mark 判据。"""
    async with _provider(tmp_path) as m:
        rid = await m.ingest(_event(_REF_A), _ctx())
        assert await m.live_blob_refs() == {_REF_A}
        await m.fold([rid], [], _ctx())
        assert await m.live_blob_refs() == set()


async def test_live_blob_refs_spans_all_tenants(tmp_path):
    """内容寻址天然跨租户共享一份字节；只看单租户会删掉别人还在用的图。"""
    async with _provider(tmp_path) as m:
        await m.ingest(_event(_REF_A), _ctx("tenant-a"))
        await m.ingest(_event(_REF_B), _ctx("tenant-b"))
        assert await m.live_blob_refs() == {_REF_A, _REF_B}


async def test_live_blob_refs_survives_partial_supersede(tmp_path):
    """同一 sha 被两条记录引用，fold 掉一条 → 仍是活引用。"""
    async with _provider(tmp_path) as m:
        rid1 = await m.ingest(_event(_REF_A), _ctx())
        await m.ingest(_event(_REF_A), _ctx())
        await m.fold([rid1], [], _ctx())
        assert await m.live_blob_refs() == {_REF_A}


async def test_live_blob_refs_empty_on_pure_text(tmp_path):
    async with _provider(tmp_path) as m:
        await m.ingest(_event(None), _ctx())
        assert await m.live_blob_refs() == set()
```

- [ ] **Step 2: 运行，确认失败**

```bash
git rm -q tests/unit/test_sql_blob_store.py
uv run pytest tests/unit/test_sql_blob_refs.py -q
```
Expected: FAIL —— `test_provider_is_no_longer_a_blob_store` 因 provider 仍是 `MemoryBlobStore` 而失败；`live_blob_refs` 相关全部 `AttributeError`。

- [ ] **Step 3: 删 `MemoryBlobModel`**

在 `src/ctx_weft/providers/memory/sql/models.py`：
- 删掉整个 `MemoryBlobModel` 类
- 从 sqlalchemy import 列表里删掉 `LargeBinary`
- 更新模块 docstring：表清单从「`memory_events` / `memory_subscriptions` / `memory_blobs` / `memory_blob_refs`」改为去掉 `memory_blobs`，并加一句说明：

```
**字节不在本库**（spec 2026-08-29 §5）：``memory_blob_refs`` 只是引用边——它需要与
ingest 同事务，故留在这里；字节归 blob store（``providers/blob/fs`` 或宿主自己的对象
存储）。裁定 D4「blob 与 ingest/fold 同事务」论证的正是这张引用边表，不是字节表。
```

同步更新 `MemoryBlobRefModel` 里任何指向 `MemoryBlobModel` 或 `collect_blobs` 的注释。

- [ ] **Step 4: 删 provider 的 blob 实现，加 `live_blob_refs`**

在 `src/ctx_weft/providers/memory/sql/provider.py`：

1. 类声明改为 `class SqlMemoryProvider(MemoryProvider):`
2. 删 `put` / `get` / `collect_blobs` 三个方法
3. 删 `_DEFAULT_BLOB_GRACE` 常量、`__init__` 的 `blob_grace_period` 形参与 `self._blob_grace`、`open_sqlite_memory` 的同名形参
4. 删不再使用的 import：`hashlib`、`IntegrityError`、`delete`、`MemoryBlobModel`、`MemoryBlobStore`、`EventBlobStore`（`timedelta` / `UTC` / `update` / `BLOB_REF_PREFIX` 仍有其他用途，逐个确认后再删）
5. 更新类 docstring 最后那条「blob 并入 memory（裁定 D4，Task C3）」，改为：

```
- **blob 引用边**（spec 2026-08-29 §5）：本 provider **不存字节**——字节归 blob store。
  这里只在 ``memory_blob_refs`` 里维护「事件 → blob」的引用边，**与 ingest 同事务**，
  并经 ``live_blob_refs()`` 把活引用吐给宿主，供 ``FsBlobStore.collect()`` 做 mark。
```

6. 在 `describe()` 之前加新方法：

```python
    async def live_blob_refs(self) -> set[str]:
        """当前仍被活记录引用的全部 blob ref（``blob:<sha>``）。

        这是 mark-sweep 的 **mark 输出**：宿主把它喂给 ``FsBlobStore.collect(live_refs)``
        （或自己的对象存储清扫器）。本 provider 只维护引用边、不持有字节，故回收动作
        不在这里发生——`fold` 之后崩溃只会让字节被孤立（泄漏），**而不会产生悬空 ref**。

        **不按 tenant 过滤。** 内容寻址让同一份字节跨租户共享一行引用目标，只看单租户
        会把别的租户仍在引用的 sha 判成孤儿并删掉。判据与引用边的写侧
        （``collect_blob_refs``，归一层的 mark 单一真源）严格对应：``content`` 里的
        结构化 ref part ∪ ``event.blob_refs`` 的显式声明。

        ⚠️ **返回值是「此刻的活引用」，不含宽限期语义。** 「已 put、尚未 ingest」那个
        窗口里的字节在这里查不到（它还没有引用边），所以清扫器**必须**自己带宽限期——
        `FsBlobStore.collect` 的 mtime 判据就是干这个的。只按本方法的返回值删会把刚上传
        还没入库的图删掉。
        """
        stmt = (
            select(MemoryBlobRefModel.sha)
            .join(MemoryEventModel, MemoryEventModel.id == MemoryBlobRefModel.event_id)
            .where(MemoryEventModel.is_superseded.is_(False))
            .distinct()
        )
        async with self._factory() as db:
            result = await db.execute(stmt)
            return {f"{BLOB_REF_PREFIX}{sha}" for sha in result.scalars().all()}
```

- [ ] **Step 5: 改 `memory/sql/__init__.py`**

从 import 与 `__all__` 里删掉 `MemoryBlobModel`。

- [ ] **Step 6: 运行测试**

Run: `uv run pytest tests/unit/test_sql_blob_refs.py -q`
Expected: 7 passed

- [ ] **Step 7: 修 conformance 套里的 blob 探测**

`tests/unit/test_memory_conformance.py` 有 `_supports_blobs(m)` 探测与依赖它的用例。SQL provider 现在与 in_memory 一样都不是 blob store，故这些用例会全部跳过。**把探测与相关用例整体删掉**，并在模块 docstring 的探测清单里删掉 `_supports_blobs` 那一行，补一句：

```
（`_supports_blobs` 探测已于 2026-08-29 删除：字节离开 RDBMS 后，仓内没有任何
`MemoryProvider` 同时是 `MemoryBlobStore`，探测恒 False、无分辨力。blob 存取的
契约测试在 `test_blob_fs_store.py`，引用边在 `test_sql_blob_refs.py`。）
```

Run: `uv run pytest tests/unit/test_memory_conformance.py -q`
Expected: 全绿，无 skip。

- [ ] **Step 8: 全量回归**

Run: `uv run pytest -q`
Expected: 只剩 `test_l05_demotion_blob_lifecycle.py` 失败（它用 SqlMemoryProvider 当 blob store，Task 12 改写）。其余全绿。把失败清单记下来交给 Task 12。

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor(memory/sql): 字节移出 RDBMS，只留引用边 + live_blob_refs

裁定 D4「blob 存取回收应与 ingest/fold 同事务」论证的是 memory_blob_refs 引用边表
（_ingest_in_tx 里那段写入是无条件的、与字节存放位置无关），不是 memory_blobs 字节表。
字节归 blob store（providers/blob/fs 或宿主自己的对象存储）。

同时堵上一个现存的洞：FsBlobStore 当 memory blob store 时无法 GC——collect(live_refs)
要调用方喂活引用，而 MemoryProvider 协议此前没有任何接口能列出它们。

BREAKING: SqlMemoryProvider 不再实现 MemoryBlobStore / EventBlobStore。该包未发布过，
无数据迁移。"
```

---

## Task 6: registry 解析退回两级 + 协议文档口径

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`get_memory_blob_store`）
- Modify: `src/ctx_weft/protocols/memory.py`（`MemoryBlobStore` docstring）
- Modify: `src/ctx_weft/providers/blob/fs/store.py`（两处指向 `collect_blobs` 的注释）
- Test: `tests/unit/test_blob_store.py`

**Interfaces:**
- Consumes: Task 5
- Produces: `ProviderRegistry.get_memory_blob_store()` 语义变为「显式注册 > `NullMemoryBlobStore`」，与 `get_event_blob_store()` 对称

- [ ] **Step 1: 写失败的测试**

在 `tests/unit/test_blob_store.py` 末尾追加：

```python
async def test_memory_blob_store_does_not_auto_resolve_from_memory_provider():
    """两个 blob store 的解析规则对称：都只有「显式注册 > Null」两级（spec §5.3）。

    曾经 memory 侧有第三级——memory provider 若自己实现了 MemoryBlobStore 就直接用它。
    那一级的唯一服务对象是 SqlMemoryProvider 的字节存储，字节移出 RDBMS 后无对象可服务。
    """
    from ctx_weft.protocols import MemoryBlobStore, NullMemoryBlobStore
    from ctx_weft.core.runtime import ProviderRegistry

    class _MemoryThatIsAlsoBlobStore(MemoryBlobStore):
        name = "fake"

        async def put(self, data, media_type, ctx):
            return "blob:" + "0" * 64

        async def get(self, ref, ctx):
            return None

    reg = ProviderRegistry()
    reg.register_memory(_MemoryThatIsAlsoBlobStore())
    assert isinstance(reg.get_memory_blob_store(), NullMemoryBlobStore)


async def test_memory_blob_store_returns_explicit_registration():
    from ctx_weft.core.runtime import ProviderRegistry
    from ctx_weft.providers.blob.fs import FsBlobStore
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        store = FsBlobStore(Path(d))
        reg = ProviderRegistry()
        reg.register_memory_blob_store(store)
        assert reg.get_memory_blob_store() is store
```

- [ ] **Step 2: 运行，确认第一条失败**

Run: `uv run pytest tests/unit/test_blob_store.py -q`
Expected: `test_memory_blob_store_does_not_auto_resolve_from_memory_provider` FAIL（仍走自动解析，返回那个 fake provider）。

- [ ] **Step 3: 改 `get_memory_blob_store`**

`src/ctx_weft/core/runtime.py`，把整个方法替换为：

```python
    def get_memory_blob_store(self) -> "MemoryBlobStore":
        """取 memory 侧 blob store。**只有两级：显式注册 > NullMemoryBlobStore。**

        与 `get_event_blob_store()` 完全对称。曾经这里有第三级——「memory provider 自己
        实现了 MemoryBlobStore 且 can_externalize 就用它」——那一级的唯一服务对象是
        `SqlMemoryProvider` 的字节存储；字节已移出 RDBMS（spec 2026-08-29 §5），
        该级无对象可服务，一并删除。

        自动解析删掉之后，「字节放哪」这件事只由接线代码表达，不再藏在解析规则里：
        宿主要 blob 能力就显式 `register_memory_blob_store(FsBlobStore(...))`。
        不注册就是 `NullMemoryBlobStore`，行为与不接 blob 的宿主逐字节一致。

        `NullMemoryBlobStore` 实例只建一次，重复调用返回同一对象。
        """
        if self._blob_store is not None:
            return self._blob_store
        if self._null_blob_store is None:
            from ctx_weft.protocols import NullMemoryBlobStore
            self._null_blob_store = NullMemoryBlobStore()
        return self._null_blob_store
```

- [ ] **Step 4: 运行测试**

Run: `uv run pytest tests/unit/test_blob_store.py tests/unit/test_event_blob_store.py -q`
Expected: 全绿

- [ ] **Step 5: 订正 `MemoryBlobStore` 的 docstring**

`src/ctx_weft/protocols/memory.py`。当前那段论证「保持独立 ABC 而不并入 `MemoryProvider`，因为自动解析判据会恒真、失去分辨力」已失效（自动解析没了）。**结论不变**，理由替换为：

```
    保持独立 ABC 而不并入 ``MemoryProvider``，是因为两者的能力**正交**：字节放在哪
    （文件系统 / 对象存储 / 数据库）与记忆怎么存是两个独立的部署决策，宿主换其中一个
    不应被迫换另一个。仓内实现见 ``ctx_weft.providers.blob.fs.FsBlobStore``
    （它同时也满足 ``EventBlobStore``，那是实现层的方便，不是协议层的关系）。

    ⚠️ 历史：Phase 3b 曾把本协议挂在 ``FilesystemToolsProvider`` 上（裁定 D5 移除），
    随后 ``SqlMemoryProvider`` 曾同时实现它、由 ``ProviderRegistry`` 自动解析
    （spec 2026-08-29 §5 移除——D4 论证的是引用边该与 ingest 同事务，不是字节该进 RDBMS）。
    现在字节侧只由显式注册决定。
```

同时删掉该 docstring 里「仓内实现见 `ctx_weft.providers.memory_sql.SqlMemoryProvider`」这一句。

- [ ] **Step 6: 改 `blob/fs/store.py` 的两处指向**

把引用 `SqlMemoryProvider.collect_blobs` 作为宽限期论据的注释改为指向本 spec：

```
           理由见 spec 2026-08-29 §5：`put` 与真正建立引用之间必然存在一个窗口
           （中间隔着 HITL park、重试、崩溃后重放），只按活引用判会在窗口内把还没
           用上的图删掉。宿主用 `SqlMemoryProvider.live_blob_refs()` 取活引用集合，
           **宽限期由本方法自己把关**——那个方法返回的是「此刻的活引用」，不含窗口语义。
```

- [ ] **Step 7: 全量回归**

Run: `uv run pytest -q`
Expected: 仍只有 `test_l05_demotion_blob_lifecycle.py` 失败（留给 Task 12）。

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(runtime): memory blob store 解析退回两级，与 event 侧对称

自动解析的唯一服务对象是 SqlMemoryProvider 的字节存储，字节移出 RDBMS 后它无对象
可服务。删掉之后「字节放哪」只由接线代码表达，不再藏在解析规则里。

顺带订正 MemoryBlobStore 协议 docstring 里那段已失效的论证（结论不变，理由从
「自动解析判据会恒真」换成「blob 能力与 memory 能力正交」）。"
```

---

## Task 7: EventPersister + InMemoryEventStore 去自订阅

**Files:**
- Create: `src/ctx_weft/providers/events/_lifecycle.py`
- Create: `src/ctx_weft/providers/events/persister.py`
- Modify: `src/ctx_weft/providers/events/store/in_memory/store.py`
- Modify: `src/ctx_weft/providers/events/__init__.py`
- Modify: `src/ctx_weft/core/runtime.py:497-498`
- Test: `tests/unit/test_event_persistence_wiring.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `providers.events._lifecycle.TERMINAL_STATUSES: frozenset[str]`
  - `providers.events._lifecycle.LIFECYCLE_EVENT_TYPES: tuple[str, ...]`
  - `providers.events._lifecycle.apply_lifecycle(active: set[str], event) -> None`
  - `providers.events.EventPersister(event_store, event_bus=None)`，方法 `async on_event(event)`、`async detach()`
  - `providers.events.attach_persistence(event_bus, event_store, *, snapshot_every_n=0) -> PersistenceHandle`（本任务只接 persister，snapshot 在 Task 8 接上）
  - `InMemoryEventStore()` 不再接受 `event_bus=` 参数

- [ ] **Step 1: 写失败的测试**

`tests/unit/test_event_persistence_wiring.py`：

```python
"""EventPersister / attach_persistence 的接线契约（spec 2026-08-29 §6.4）。

订阅策略（丢弃瞬态 delta）归 persister，不归 store——store 变成「让存什么就存什么」，
一致性测试才能直接测 append/read 往返而不被 store 悄悄吃掉测试事件。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.protocols.events import TRANSIENT_EVENT_TYPES, Event
from ctx_weft.providers.events import (
    EventPersister,
    InMemoryEventStore,
    InProcessEventBus,
    attach_persistence,
)


def _ev(type_: str, seq: int = 1, session: str = "s1") -> Event:
    return Event(
        id=f"evt_{seq:04d}",
        run_id="r1",
        sequence=seq,
        session_id=session,
        type=type_,
        timestamp=datetime.now(UTC),
    )


async def test_store_no_longer_accepts_event_bus():
    """自订阅已抽出成 EventPersister——store 不再自己订阅。"""
    with pytest.raises(TypeError):
        InMemoryEventStore(event_bus=InProcessEventBus())


async def test_store_append_no_longer_filters_transient():
    """行为变化（spec §6.4）：过滤是订阅策略，归 persister。"""
    transient = next(iter(TRANSIENT_EVENT_TYPES))
    store = InMemoryEventStore()
    await store.append(_ev(transient))
    assert len(await store.read_by_session("s1")) == 1


async def test_persister_drops_transient():
    transient = next(iter(TRANSIENT_EVENT_TYPES))
    store = InMemoryEventStore()
    p = EventPersister(store)
    await p.on_event(_ev(transient, 1))
    await p.on_event(_ev("SessionCreated", 2))
    stored = await store.read_by_session("s1")
    assert [e.type for e in stored] == ["SessionCreated"]


async def test_persister_subscribes_when_given_a_bus():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    EventPersister(store, bus)
    await bus.emit(_ev("SessionCreated"))
    assert len(await store.read_by_session("s1")) == 1


async def test_persister_swallows_store_errors():
    """append 失败不得掀掉 loop——bus handler 在 emit 里内联执行。"""
    class _Boom:
        async def append(self, event):
            raise RuntimeError("db down")

    p = EventPersister(_Boom())
    await p.on_event(_ev("SessionCreated"))  # 不抛即通过


async def test_detach_stops_receiving():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    p = EventPersister(store, bus)
    await p.detach()
    await bus.emit(_ev("SessionCreated"))
    assert await store.read_by_session("s1") == []


async def test_attach_persistence_wires_persister():
    bus = InProcessEventBus()
    store = InMemoryEventStore()
    handle = attach_persistence(bus, store)
    await bus.emit(_ev("SessionCreated"))
    assert len(await store.read_by_session("s1")) == 1
    await handle.detach()
    await bus.emit(_ev("SessionFinished", 2))
    assert len(await store.read_by_session("s1")) == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_event_persistence_wiring.py -q`
Expected: FAIL — `ImportError: cannot import name 'EventPersister'`

- [ ] **Step 3: 建 `_lifecycle.py`**

```python
"""会话活跃判据——**两个 EventStore 实现共用的同一台状态机**。

`list_active_session_ids()` 决定崩溃恢复要捞哪些会话。两个实现各写一遍判据必然分叉，
而分叉的表现是「重启后某些会话不弹恢复」或「已结束的会话反复被恢复」——都极难归因。
故判据只此一份，`in_memory` 增量调用它，`sql` 把查出来的生命周期事件重放一遍。

**为什么 SQL 侧可以「先把所有 session 置为 active，再重放生命周期事件」**：
`in_memory` 是在每个 session 的**首次出现**时把它加进 active 的。由于操作只有
add/discard 且逐 session 独立，「在 -∞ 处 add」与「在该 session 首个事件处 add」
对最终结果完全等价——首个事件必然先于该 session 的其余事件。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "LIFECYCLE_EVENT_TYPES",
    "TERMINAL_STATUSES",
    "apply_lifecycle",
    "replay_lifecycle",
]

#: `SessionStatusChanged.payload["new_status"]` 取这些值时视为会话已终结。
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"})

#: 会改变活跃性的事件类型。SQL 侧据此收窄查询范围；`SessionCreated` 本身在
#: `apply_lifecycle` 里是 no-op（它的效果由「该 session 出现过」这个种子覆盖），
#: 但仍列在这里——收窄查询时漏掉它没有坏处，列上它让集合的语义自解释。
LIFECYCLE_EVENT_TYPES = (
    "SessionCreated",
    "SessionResumed",
    "SessionFinished",
    "SessionStatusChanged",
)


def apply_lifecycle(active: set[str], event: Any) -> None:
    """把一条事件应用到活跃集合上。非生命周期事件一律 no-op。

    ⚠️ **不负责「首次出现即 active」那条种子规则**——那条由调用方提供：
    `in_memory` 在首次见到某 session 时 add，`sql` 用 `SELECT DISTINCT session_id`
    一次性 add。两者等价，见模块 docstring。
    """
    sid = event.session_id
    t = event.type
    if t == "SessionFinished":
        active.discard(sid)
    elif t == "SessionResumed":
        # 多轮会话每轮结束发 SessionFinished、下一条消息发 SessionResumed 重新激活；
        # 不重新计入的话崩溃恢复会漏掉所有已对话过的会话。
        active.add(sid)
    elif t == "SessionStatusChanged":
        if (event.payload or {}).get("new_status", "") in TERMINAL_STATUSES:
            active.discard(sid)


def replay_lifecycle(session_ids: Iterable[str], events: Iterable[Any]) -> set[str]:
    """种子（全部出现过的 session）+ 按 id 升序的生命周期事件 → 活跃集合。"""
    active = set(session_ids)
    for event in events:
        apply_lifecycle(active, event)
    return active
```

- [ ] **Step 4: 建 `persister.py`**

```python
"""把事件灌进 EventStore 的 EventBus 订阅者，以及按正确顺序接线的便利函数。

抽出成独立组件（而不是像从前那样塞进 `InMemoryEventStore.__init__`）的理由：
换掉内存 store 的宿主否则必须自己重写一遍订阅逻辑——参考宿主
`IpMasterCoworkPy` 里的 `EventPersister` 正是这么来的。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ctx_weft.protocols.events import TRANSIENT_EVENT_TYPES

if TYPE_CHECKING:
    from ctx_weft.protocols.events import Event, EventBus, EventStore

logger = logging.getLogger(__name__)

__all__ = ["EventPersister", "PersistenceHandle", "attach_persistence"]


class EventPersister:
    """订阅 EventBus，把非瞬态事件 append 进任意 `EventStore`。

    **瞬态过滤在这里，不在 store 里**（spec 2026-08-29 §6.4）：每 token 一个的流式
    delta 只为实时流而发，落库会让事件表无界膨胀、且被 `read_by_session` /
    `reduce_events` 全量回放（真相由 `LLMResponseFinished` 承载）。这是**订阅策略**，
    不是存储策略——`EventStore.append` 因此是「让存什么就存什么」，一致性测试才能
    直接测往返而不被 store 悄悄吃掉测试事件。

    ⚠️ `on_event` 吞掉 store 异常**不是**防止掀掉 loop 的唯一防线——
    `InProcessEventBus.emit` 已经捕获 handler 异常并 `logger.exception`。这里再捕一次
    是为了把 event id / type 写进日志（bus 那层只知道 subscriber id）。改动前先看
    那一处，别以为删掉这里就没人兜底了。
    """

    def __init__(self, event_store: "EventStore", event_bus: "EventBus | None" = None) -> None:
        self._store = event_store
        self._subscription = event_bus.subscribe(None, self.on_event) if event_bus else None

    async def on_event(self, event: "Event") -> None:
        if event.type in TRANSIENT_EVENT_TYPES:
            return
        try:
            await self._store.append(event)
        except Exception:
            logger.exception(
                "EventPersister: failed to append event %s (%s)", event.id, event.type
            )

    async def detach(self) -> None:
        """停止订阅。宿主把 runtime 默认的内存 store 换成持久实现时必须调——
        否则旧实例作为孤儿订阅者继续在内存里堆积事件。"""
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None


class PersistenceHandle:
    """`attach_persistence` 的返回值：一起 detach 掉它接上的全部订阅者。"""

    def __init__(self, persister: EventPersister, snapshot_writer: Any = None) -> None:
        self.persister = persister
        self.snapshot_writer = snapshot_writer

    async def detach(self) -> None:
        if self.snapshot_writer is not None:
            await self.snapshot_writer.detach()
        await self.persister.detach()


def attach_persistence(
    event_bus: "EventBus",
    event_store: "EventStore",
    *,
    snapshot_every_n: int = 0,
) -> PersistenceHandle:
    """按**正确顺序**接线 EventPersister（+ `snapshot_every_n > 0` 时的 SnapshotWriter）。

    顺序不是可选项：`SnapshotWriter` 要用 `rebuild_view` 折出当前状态，而那条事件必须
    **已经**被 persister 落库，否则 view 里没有它、`last_event_id` 与 view 对不上。
    把顺序封进本函数，接反从此不可达。

    `snapshot_every_n=0`（默认）时不接 SnapshotWriter——`CtxWeftRuntime` 走的就是这条，
    现有行为零变化。
    """
    persister = EventPersister(event_store, event_bus)
    writer = None
    if snapshot_every_n > 0:
        from ctx_weft.providers.events.snapshot import SnapshotWriter
        writer = SnapshotWriter(event_store, event_bus, every_n_events=snapshot_every_n)
    return PersistenceHandle(persister, writer)
```

- [ ] **Step 5: 改 `InMemoryEventStore`**

`src/ctx_weft/providers/events/store/in_memory/store.py`：

1. `__init__` 删掉 `event_bus` 形参与 `self._subscription`；删掉 `detach()` 方法（搬去 `EventPersister`）
2. `append()` 删掉开头的 `if event.type in TRANSIENT_EVENT_TYPES: return`，并把注释改成：

```python
        # **不过滤瞬态事件**（spec 2026-08-29 §6.4）：那是订阅策略，归 EventPersister。
        # 本方法「让存什么就存什么」——一致性测试因此能直接测 append/read 往返。
```

3. 活跃集合的维护改用共用状态机：

```python
        async with self._lock:
            sid = event.session_id
            if sid not in self._events:
                self._events[sid] = []
                self._active.add(sid)      # 种子：该 session 出现过即 active
            self._events[sid].append(event)
            apply_lifecycle(self._active, event)
```

4. 删掉本文件的 `_TERMINAL_STATUSES` 常量，改 `from ctx_weft.providers.events._lifecycle import apply_lifecycle`
5. 删掉不再使用的 `TRANSIENT_EVENT_TYPES` 与 `EventBus` import
6. 类 docstring 里「传入 event_bus= 时自动订阅」那句改成「订阅由 `EventPersister` 负责，见 `providers/events/persister.py`」

- [ ] **Step 6: 导出新符号**

`providers/events/__init__.py` 加上：

```python
from ctx_weft.providers.events.persister import (
    EventPersister,
    PersistenceHandle,
    attach_persistence,
)
```
并补进 `__all__`。

- [ ] **Step 7: 改 runtime 接线**

`src/ctx_weft/core/runtime.py:497-498`：

```python
        from ctx_weft.providers.events import EventPersister, InMemoryEventStore
        self.event_store = event_store or InMemoryEventStore()
        # 订阅从 store 里抽了出来（spec 2026-08-29 §6.4）。SnapshotWriter **默认不接**：
        # 宿主要快照 + 增量回放就自己 attach_persistence(bus, store, snapshot_every_n=50)。
        self._event_persister = EventPersister(self.event_store, self._event_bus)
```

⚠️ 宿主传入自己的 `event_store` 时也走同一条 persister——与改造前「宿主自带 store 就没人订阅」不同，这是修正而非变化：改造前宿主必须自己再接一个订阅者。

- [ ] **Step 8: 运行测试**

Run: `uv run pytest tests/unit/test_event_persistence_wiring.py -q`
Expected: 7 passed

- [ ] **Step 9: 全量回归**

Run: `uv run pytest -q`
Expected: 除 `test_l05_demotion_blob_lifecycle.py` 外全绿。若有测试直接构造 `InMemoryEventStore(event_bus=...)`，改成 `InMemoryEventStore()` + `EventPersister(store, bus)`。

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "feat(events): 抽出 EventPersister，活跃判据收成两侧共用的状态机

订阅此前塞在 InMemoryEventStore.__init__ 里，换掉内存 store 的宿主必须自己重写一遍
（参考宿主的 EventPersister 正是这么来的）。抽出后对任意 EventStore 通用。

瞬态过滤跟着搬到 persister：那是订阅策略不是存储策略，append 变成「让存什么就存
什么」之后，一致性测试才能直接测往返。

_lifecycle.py 把会话活跃判据收成一份，为 SqlEventStore 做准备——两个实现各写一遍
必然分叉，而分叉表现为「重启后会话不弹恢复」，极难归因。

BREAKING: InMemoryEventStore 不再接受 event_bus= 参数；append 不再过滤瞬态事件。"
```

---

## Task 8: SnapshotWriter

**Files:**
- Create: `src/ctx_weft/providers/events/snapshot.py`
- Modify: `src/ctx_weft/providers/events/__init__.py`
- Test: `tests/unit/test_event_persistence_wiring.py`（追加）

**Interfaces:**
- Consumes: Task 7 的 `attach_persistence` / `PersistenceHandle`
- Produces: `providers.events.SnapshotWriter(event_store, event_bus=None, *, every_n_events=50)`，方法 `async on_event(event)`、`async detach()`；模块常量 `DEFAULT_SNAPSHOT_EVERY_N_EVENTS = 50`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/test_event_persistence_wiring.py`：

```python
# ── SnapshotWriter ──────────────────────────────────────────────────────────


async def test_snapshot_written_on_session_finished():
    from ctx_weft.providers.events import SnapshotWriter

    bus, store = InProcessEventBus(), InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=1)
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("SessionFinished", 2))
    snap = await store.load_latest_snapshot("s1")
    assert snap is not None
    assert snap.snapshot_reason == "session_finished"
    assert snap.last_event_id == "evt_0002"


async def test_snapshot_periodic_on_run_finished():
    bus, store = InProcessEventBus(), InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=2)
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("RunFinished", 2))       # n=2 达阈值
    snap = await store.load_latest_snapshot("s1")
    assert snap is not None
    assert snap.snapshot_reason == "periodic"


async def test_snapshot_not_written_before_threshold():
    bus, store = InProcessEventBus(), InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=50)
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("RunFinished", 2))
    assert await store.load_latest_snapshot("s1") is None


async def test_snapshot_sees_the_triggering_event():
    """顺序契约：persister 必须先落库，snapshot 才折得到这条事件（spec §6.5）。"""
    bus, store = InProcessEventBus(), InMemoryEventStore()
    attach_persistence(bus, store, snapshot_every_n=1)
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("SessionFinished", 2))
    snap = await store.load_latest_snapshot("s1")
    stored = await store.read_by_session("s1")
    assert snap.last_event_id == stored[-1].id
    assert snap.last_event_sequence == stored[-1].sequence


async def test_snapshot_writer_not_attached_by_default():
    bus, store = InProcessEventBus(), InMemoryEventStore()
    handle = attach_persistence(bus, store)          # snapshot_every_n 默认 0
    assert handle.snapshot_writer is None
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("SessionFinished", 2))
    assert await store.load_latest_snapshot("s1") is None


async def test_snapshot_writer_swallows_errors():
    from ctx_weft.providers.events import SnapshotWriter

    class _Boom(InMemoryEventStore):
        async def save_snapshot(self, snapshot):
            raise RuntimeError("db down")

    bus, store = InProcessEventBus(), _Boom()
    attach_persistence(bus, store, snapshot_every_n=1)
    await bus.emit(_ev("SessionCreated", 1))
    await bus.emit(_ev("SessionFinished", 2))   # 不抛即通过
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest tests/unit/test_event_persistence_wiring.py -q -k snapshot`
Expected: FAIL — `ImportError: cannot import name 'SnapshotWriter'`

- [ ] **Step 3: 写 `snapshot.py`**

```python
"""SnapshotWriter——会话存活期间定期写状态快照。

崩溃恢复针对的是**没有终态**的 session。若快照只在 `SessionFinished` 写，恢复时永远
没有快照可用，`rebuild_view` 只能 O(全部事件) 全量回放。定期写之后，恢复退化成
「最新快照 + 增量 delta」，回放量被限制在约一个阈值窗口内。

⚠️ **本类的 `on_event` 在 `EventBus.emit()` 里内联执行，不是后台任务。** 所以写入路径
必须用 `rebuild_view`（快照 + 增量，O(delta)）而不是全量 reduce——任何 O(全部事件) 的
读取都会直接阻塞 loop 主路径。

⚠️ **必须在 EventPersister 之后订阅**，否则 `rebuild_view` 看不到当前这条事件。
用 `attach_persistence()` 接线，顺序由它保证。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ctx_weft.protocols.events import TRANSIENT_EVENT_TYPES

if TYPE_CHECKING:
    from ctx_weft.protocols.events import Event, EventBus, EventStore

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_SNAPSHOT_EVERY_N_EVENTS", "SnapshotWriter"]

#: RunFinished 边界上，距上次快照累计多少事件后写一张。
DEFAULT_SNAPSHOT_EVERY_N_EVENTS = 50


class SnapshotWriter:
    """EventBus 订阅者：会话存活期间定期 + 结束时写 `RunSnapshot`。

    只在 `RunFinished`（一次 loop run 收尾、状态稳定的恢复边界）与 `SessionFinished`
    两个点落快照——中途落快照会把一个跑到一半的 run 的状态固化下来，恢复时反而更难处理。
    """

    def __init__(
        self,
        event_store: "EventStore",
        event_bus: "EventBus | None" = None,
        *,
        every_n_events: int = DEFAULT_SNAPSHOT_EVERY_N_EVENTS,
    ) -> None:
        self._store = event_store
        self._every_n = max(1, every_n_events)
        self._since_snapshot: dict[str, int] = {}
        self._subscription = event_bus.subscribe(None, self.on_event) if event_bus else None

    async def detach(self) -> None:
        if self._subscription is not None:
            await self._subscription.unsubscribe()
            self._subscription = None

    async def on_event(self, event: "Event") -> None:
        session_id = event.session_id
        if not session_id:
            return
        # 瞬态 delta 不计入阈值，使「每 N 事件」按有意义的持久化事件计数。
        if event.type in TRANSIENT_EVENT_TYPES:
            return
        try:
            if event.type == "SessionFinished":
                await self._write(session_id, event, reason="session_finished")
                self._since_snapshot.pop(session_id, None)
                return
            n = self._since_snapshot.get(session_id, 0) + 1
            if event.type == "RunFinished" and n >= self._every_n:
                await self._write(session_id, event, reason="periodic")
                self._since_snapshot[session_id] = 0
            else:
                self._since_snapshot[session_id] = n
        except Exception:
            logger.exception("SnapshotWriter: failed for session %s", session_id)

    async def _write(self, session_id: str, event: "Event", reason: str) -> None:
        from ctx_weft.core.control.reducers import rebuild_view, serialize_view
        from ctx_weft.core.utils import generate_id, now_utc
        from ctx_weft.protocols.events import RunSnapshot

        # rebuild_view = 上一张快照 + delta（无快照时全量）。本事件此刻**已被先注册的
        # EventPersister 落库**（attach_persistence 保证顺序），故 view 已包含它，
        # last_event_id=event.id 与 view 一致。
        view = await rebuild_view(self._store, session_id)
        if not view.session_id:
            return  # 该 session 尚无任何已持久化事件，跳过
        snapshot = RunSnapshot(
            id=generate_id("snp"),
            run_id=event.run_id or "",
            session_id=session_id,
            last_event_id=event.id,
            last_event_sequence=event.sequence,
            state_blob=serialize_view(view),
            snapshot_reason=reason,
            snapshot_at=now_utc(),
        )
        await self._store.save_snapshot(snapshot)
        logger.info(
            "SnapshotWriter: snapshot %s written for session %s (reason=%s, seq=%d)",
            snapshot.id, session_id, reason, event.sequence,
        )
```

- [ ] **Step 4: 导出**

`providers/events/__init__.py` 加 `from ctx_weft.providers.events.snapshot import SnapshotWriter` 并补进 `__all__`。

- [ ] **Step 5: 运行测试**

Run: `uv run pytest tests/unit/test_event_persistence_wiring.py -q`
Expected: 13 passed

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(events): 补 SnapshotWriter，快照 + 增量回放开箱可用

save_snapshot / load_latest_snapshot / read_after 三个协议方法此前在仓内全是死代码
——没有任何东西会写快照，rebuild_view 的「快照 + 增量」优化对开箱即用的宿主不生效。

默认不接进 CtxWeftRuntime（现有行为零变化），宿主一行 attach_persistence 开启。
顺序陷阱（必须晚于 EventPersister 订阅）由 attach_persistence 封死，不靠注释。"
```

---

## Task 9: EventStore 一致性测试套（先只跑 in_memory）

**Files:**
- Create: `tests/unit/test_event_store_conformance.py`

**Interfaces:**
- Consumes: Task 7 的 `InMemoryEventStore()`（无 bus 参数）
- Produces: `_STORE_FACTORIES` 字典——Task 10 在这里加一行接入 `SqlEventStore`

- [ ] **Step 1: 写测试套（此刻应当全绿）**

```python
"""EventStore 协议一致性测试套（spec 2026-08-29 §8）。

**面向协议、不面向实现。** 每条用例只经 `protocols/events.py` 声明的方法操作 store，
不碰任何实现内部字段（`_events` / `_active` / `_snapshots`）。

═══ 接入点 ═══════════════════════════════════════════════════════════════════
新 store 接进来只需在 `_STORE_FACTORIES` 里加一行 + 一个工厂：

    _STORE_FACTORIES = {
        "in_memory": _make_in_memory,
        "sql": _make_sql,          # ← Task 10 加这一行
    }

工厂签名 `(tmp_path) -> AsyncIterator[EventStore]`（asynccontextmanager）。
═════════════════════════════════════════════════════════════════════════════

本套存在的直接理由：`list_active_session_ids` 的判据决定崩溃恢复捞哪些会话，两个实现
分叉的表现是「重启后某些会话不弹恢复」或「已结束的会话反复被恢复」——生产里极难归因。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.protocols.events import Event, EventStore, RunSnapshot
from ctx_weft.providers.events import InMemoryEventStore


@asynccontextmanager
async def _make_in_memory(tmp_path) -> AsyncIterator[EventStore]:
    yield InMemoryEventStore()


_STORE_FACTORIES = {
    "in_memory": _make_in_memory,
}


@pytest.fixture(params=sorted(_STORE_FACTORIES))
async def store(request, tmp_path) -> AsyncIterator[EventStore]:
    async with _STORE_FACTORIES[request.param](tmp_path) as s:
        yield s


_T0 = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def _ev(
    seq: int,
    type_: str = "RunStarted",
    *,
    session: str = "s1",
    payload: dict | None = None,
    **kw,
) -> Event:
    """事件 id 用 ULID 字典序等价的零填充串——read_after 的排序契约靠它。"""
    return Event(
        id=f"evt_{seq:04d}",
        run_id=kw.pop("run_id", "r1"),
        sequence=seq,
        session_id=session,
        type=type_,
        timestamp=_T0 + timedelta(seconds=seq),
        payload=payload or {},
        **kw,
    )


# ── append / read 往返 ───────────────────────────────────────────────────────


async def test_append_read_roundtrip_preserves_every_field(store):
    """逐字段往返。schema_version 与 causation_id 尤其容易在 SQL 映射里被漏掉。"""
    ev = Event(
        id="evt_0001",
        run_id="r7",
        sequence=42,
        session_id="s1",
        type="RunStarted",
        timestamp=_T0,
        tenant_id="tenant-x",
        task_id="t1",
        agent_id="a1",
        payload={"k": "v", "n": 1},
        metadata={"m": True},
        causation_id="evt_0000",
        schema_version=3,
    )
    await store.append(ev)
    (got,) = await store.read_by_session("s1")
    for field in (
        "id", "run_id", "sequence", "session_id", "type", "tenant_id",
        "task_id", "agent_id", "payload", "metadata", "causation_id",
        "schema_version",
    ):
        assert getattr(got, field) == getattr(ev, field), field
    assert got.timestamp == _T0          # 时区保真：naive 回读会让这条恒 False


async def test_read_by_session_is_ordered(store):
    for seq in (3, 1, 2):
        await store.append(_ev(seq))
    assert [e.sequence for e in await store.read_by_session("s1")] == [1, 2, 3]


async def test_read_by_session_isolates_sessions(store):
    await store.append(_ev(1, session="s1"))
    await store.append(_ev(2, session="s2"))
    assert [e.session_id for e in await store.read_by_session("s1")] == ["s1"]


async def test_read_by_session_unknown_returns_empty(store):
    assert await store.read_by_session("nope") == []


# ── read_after ───────────────────────────────────────────────────────────────


async def test_read_after_returns_strictly_later(store):
    for seq in (1, 2, 3):
        await store.append(_ev(seq))
    got = await store.read_after("s1", "evt_0001")
    assert [e.sequence for e in got] == [2, 3]


async def test_read_after_last_event_returns_empty(store):
    await store.append(_ev(1))
    assert await store.read_after("s1", "evt_0001") == []


# ── read_session_events_of_types ─────────────────────────────────────────────


async def test_read_of_types_filters(store):
    await store.append(_ev(1, "RunStarted"))
    await store.append(_ev(2, "RunFinished"))
    await store.append(_ev(3, "SessionFinished"))
    got = await store.read_session_events_of_types("s1", ("RunFinished", "SessionFinished"))
    assert [e.type for e in got] == ["RunFinished", "SessionFinished"]


async def test_read_of_types_empty_tuple(store):
    await store.append(_ev(1))
    assert await store.read_session_events_of_types("s1", ()) == []


# ── 快照 ─────────────────────────────────────────────────────────────────────


def _snap(sid: str = "s1", *, last_id: str, seq: int, reason: str = "periodic") -> RunSnapshot:
    return RunSnapshot(
        id=f"snp_{last_id}",
        run_id="r1",
        session_id=sid,
        last_event_id=last_id,
        last_event_sequence=seq,
        state_blob={"session_id": sid, "n": seq},
        snapshot_reason=reason,
        snapshot_at=_T0,
    )


async def test_snapshot_roundtrip(store):
    s = _snap(last_id="evt_0005", seq=5)
    await store.save_snapshot(s)
    got = await store.load_latest_snapshot("s1")
    assert got is not None
    for field in (
        "id", "run_id", "session_id", "last_event_id",
        "last_event_sequence", "state_blob", "snapshot_reason",
    ):
        assert getattr(got, field) == getattr(s, field), field


async def test_load_latest_snapshot_returns_newest(store):
    await store.save_snapshot(_snap(last_id="evt_0001", seq=1))
    await store.save_snapshot(_snap(last_id="evt_0009", seq=9))
    got = await store.load_latest_snapshot("s1")
    assert got.last_event_sequence == 9


async def test_load_latest_snapshot_none_when_absent(store):
    assert await store.load_latest_snapshot("s1") is None


# ── list_active_session_ids：会话生命周期状态机 ──────────────────────────────


async def test_active_after_session_created(store):
    await store.append(_ev(1, "SessionCreated"))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_inactive_after_session_finished(store):
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    assert set(await store.list_active_session_ids()) == set()


async def test_reactivated_by_session_resumed(store):
    """多轮会话：每轮结束发 SessionFinished，下一条消息发 SessionResumed。
    不重新计入的话崩溃恢复会漏掉所有已对话过的会话。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "SessionResumed"))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_inactive_after_terminal_status_changed(store):
    """参考实现的纯 SQL 判据完全忽略 SessionStatusChanged——这条钉住它。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionStatusChanged", payload={"new_status": "FAILED"}))
    assert set(await store.list_active_session_ids()) == set()


async def test_non_terminal_status_changed_keeps_active(store):
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionStatusChanged", payload={"new_status": "RUNNING"}))
    assert set(await store.list_active_session_ids()) == {"s1"}


async def test_non_terminal_status_does_not_resurrect(store):
    """已 finished 的会话不该被一条非终态状态事件重新拉活。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "SessionStatusChanged", payload={"new_status": "RUNNING"}))
    assert set(await store.list_active_session_ids()) == set()


async def test_ordinary_event_does_not_resurrect(store):
    """普通事件既不激活也不停用——只有四类生命周期事件改变活跃性。"""
    await store.append(_ev(1, "SessionCreated"))
    await store.append(_ev(2, "SessionFinished"))
    await store.append(_ev(3, "RunStarted"))
    assert set(await store.list_active_session_ids()) == set()


async def test_active_sessions_are_independent(store):
    await store.append(_ev(1, "SessionCreated", session="s1"))
    await store.append(_ev(2, "SessionCreated", session="s2"))
    await store.append(_ev(3, "SessionFinished", session="s1"))
    assert set(await store.list_active_session_ids()) == {"s2"}
```

- [ ] **Step 2: 运行，确认全绿**

Run: `uv run pytest tests/unit/test_event_store_conformance.py -q`
Expected: 19 passed（全部对 `in_memory` 跑）。

若 `test_active_after_session_created` 之外的某条失败，说明 Task 7 的状态机重构改了行为——**先修 Task 7，不要改测试**。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_event_store_conformance.py
git commit -m "test(events): 新增 EventStore 协议一致性测试套

面向协议、不面向实现。当前只跑 InMemoryEventStore，SqlEventStore 接入即复用整套。

重点覆盖 list_active_session_ids 的会话生命周期状态机——参考宿主的纯 SQL 判据完全
忽略 SessionStatusChanged，两个实现分叉的表现是「重启后会话不弹恢复」，生产里极难
归因，必须由一致性套钉死。"
```

---

## Task 10: events SQL 表模型 + 接进一致性套

**Files:**
- Create: `src/ctx_weft/providers/events/store/sql/__init__.py`
- Create: `src/ctx_weft/providers/events/store/sql/models.py`
- Modify: `tests/unit/test_event_store_conformance.py`（加 `_make_sql` 工厂）

**Interfaces:**
- Consumes: Task 4 的 `providers._sqlalchemy.{UtcDateTime, make_session_factory}`
- Produces: `providers.events.store.sql.models.{Base, EventModel, SnapshotModel}`

- [ ] **Step 1: 写 `models.py`**

```python
"""SQLAlchemy 表模型：``events`` / ``event_snapshots``。

**`Base` 与 `memory/sql` 的刻意不共用。** 共用会让只想建 events 表的宿主被迫连 memory
表一起建，而两个包的可选依赖边界本就是分开的。单文件 SQLite 部署照样可以共享同一个
engine——各自跑一次 ``Base.metadata.create_all`` 即可。

表名与列名与参考宿主
（``IpMasterCoworkPy/src/ipmastercowork/persistence/postgres/models.py``）保持一致，
但**补了两列**（那边漏了、导致往返有损）：

- ``events.schema_version`` —— `Event.schema_version` 是给 reducer 分支用的。不存的话
  第一次 bump 版本时会静默把新事件读成旧版本。
- ``event_snapshots.run_id`` —— 参考实现的 ``load_latest_snapshot`` 硬编码 ``run_id=""``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ctx_weft.providers._sqlalchemy import UtcDateTime


class Base(DeclarativeBase):
    """本包自带的 declarative base（宿主可把这两张表映射进自己的 Base）。"""


class EventModel(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    type: Mapped[str] = mapped_column(String(128), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 存量行（参考宿主写的）没有这一列 → 读侧给默认值 1，零迁移。
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())

    __table_args__ = (
        # list_active_session_ids 按 (session_id, type) 收窄，再按 id 升序重放。
        Index("ix_events_session_type", "session_id", "type"),
    )


class SnapshotModel(Base):
    __tablename__ = "event_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="")
    last_event_id: Mapped[str] = mapped_column(String(64))
    last_event_sequence: Mapped[int] = mapped_column(Integer)
    state_blob_json: Mapped[str] = mapped_column(Text, default="{}")
    snapshot_reason: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
```

- [ ] **Step 2: 写占位 `__init__.py`**

```python
"""SQL-backed EventStore（SQLAlchemy async；默认 SQLite，postgres 同源）。

**可选依赖**：需要 ``sqlalchemy>=2.0``（SQLite 后端另需 ``aiosqlite``），
装法 ``pip install ctx-weft[sql]``。没有任何上层包 eager import 本子包。
"""

from ctx_weft.providers.events.store.sql.models import Base, EventModel, SnapshotModel
from ctx_weft.providers.events.store.sql.store import (
    SqlEventStore,
    open_sqlite_event_store,
)

__all__ = [
    "Base",
    "EventModel",
    "SnapshotModel",
    "SqlEventStore",
    "open_sqlite_event_store",
]
```

- [ ] **Step 3: 把 SQL store 接进一致性套**

在 `tests/unit/test_event_store_conformance.py` 的工厂区加：

```python
@asynccontextmanager
async def _make_sql(tmp_path) -> AsyncIterator[EventStore]:
    from ctx_weft.providers.events.store.sql import open_sqlite_event_store

    async with open_sqlite_event_store(tmp_path / "events.db") as s:
        yield s


_STORE_FACTORIES = {
    "in_memory": _make_in_memory,
    "sql": _make_sql,
}
```

- [ ] **Step 4: 运行，确认 SQL 侧全红**

Run: `uv run pytest tests/unit/test_event_store_conformance.py -q`
Expected: `in_memory` 19 passed；`sql` 19 errors（`ImportError: cannot import name 'SqlEventStore'`）。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(events/sql): 表模型 events / event_snapshots，接进一致性套（红）

补了参考宿主漏掉的两列：events.schema_version（reducer 分支用，不存的话第一次
bump 版本会静默读成旧版本）与 event_snapshots.run_id（参考实现的 load_latest_snapshot
硬编码 run_id=\"\"）。两处都是往返有损。

Base 与 memory/sql 刻意不共用——只想建 events 表的宿主不该被迫连 memory 表一起建。"
```

---

## Task 11: SqlEventStore 实现

**Files:**
- Create: `src/ctx_weft/providers/events/store/sql/store.py`

**Interfaces:**
- Consumes: Task 10 的 models；Task 7 的 `_lifecycle.{LIFECYCLE_EVENT_TYPES, replay_lifecycle}`
- Produces:
  - `SqlEventStore(session_factory, *, keep_snapshots: int = 3)`，实现 `EventStore` 全部七个方法
  - `open_sqlite_event_store(db_path) -> AsyncIterator[SqlEventStore]`（`asynccontextmanager`）

- [ ] **Step 1: 写 `store.py`**

```python
"""SqlEventStore：SQLAlchemy async 的 EventStore 实现（默认 SQLite，postgres 同源）。

协议面七个方法齐备。与 `providers/events/store/in_memory` 是同一套契约的两个实现，
`tests/unit/test_event_store_conformance.py` 对两者跑同一套用例。

设计要点：

- **活跃判据不自己写。** `list_active_session_ids` 把生命周期事件捞出来，交给
  `providers/events/_lifecycle` 那台**两个实现共用**的状态机重放——见该方法 docstring。
- **`append` 不过滤瞬态事件**（spec 2026-08-29 §6.4）：那是订阅策略，归 `EventPersister`。
  与 `InMemoryEventStore` 同口径，否则一致性套没法用同一份用例跑两边。
- **快照剪枝**：每个 session 只留最新 `keep_snapshots` 张。恢复只取最新一张，
  定期写入会让旧快照无界累积。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx_weft.protocols.events import Event, EventStore, RunSnapshot
from ctx_weft.providers._sqlalchemy import make_session_factory
from ctx_weft.providers.events._lifecycle import (
    LIFECYCLE_EVENT_TYPES,
    replay_lifecycle,
)
from ctx_weft.providers.events.store.sql.models import Base, EventModel, SnapshotModel

logger = logging.getLogger(__name__)

__all__ = ["SqlEventStore", "open_sqlite_event_store"]


class SqlEventStore(EventStore):
    """SQLAlchemy-backed event store。"""

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession]",
        *,
        keep_snapshots: int = 3,
    ) -> None:
        self._factory = session_factory
        self._keep_snapshots = max(1, keep_snapshots)

    # ── 写 ────────────────────────────────────────────────────────────────────

    async def append(self, event: Event) -> None:
        async with self._factory() as db, db.begin():
            db.add(EventModel(
                id=event.id,
                run_id=event.run_id,
                session_id=event.session_id,
                task_id=event.task_id,
                agent_id=event.agent_id,
                tenant_id=event.tenant_id,
                type=event.type,
                sequence=event.sequence,
                payload_json=json.dumps(event.payload),
                metadata_json=json.dumps(event.metadata),
                causation_id=event.causation_id,
                schema_version=event.schema_version,
                timestamp=event.timestamp,
            ))

    # ── 读 ────────────────────────────────────────────────────────────────────

    async def read_by_session(self, session_id: str) -> list[Event]:
        """按 id（ULID，字典序即时间序）升序返回该 session 的全部事件。"""
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(EventModel.session_id == session_id)
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def read_after(self, session_id: str, after_event_id: str) -> list[Event]:
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(
                    EventModel.session_id == session_id,
                    EventModel.id > after_event_id,
                )
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def read_session_events_of_types(
        self, session_id: str, types: "tuple[str, ...]",
    ) -> list[Event]:
        if not types:
            return []
        async with self._factory() as db:
            result = await db.execute(
                select(EventModel)
                .where(
                    EventModel.session_id == session_id,
                    EventModel.type.in_(tuple(str(t) for t in types)),
                )
                .order_by(EventModel.id)
            )
            return [_row_to_event(r) for r in result.scalars().all()]

    async def list_active_session_ids(self) -> list[str]:
        """有开启边界、未被终结的 session（崩溃恢复用）。

        **判据不在这里，在 `providers/events/_lifecycle`** ——与 `InMemoryEventStore`
        共用同一台状态机。各写一遍必然分叉，而分叉表现为「重启后某些会话不弹恢复」或
        「已结束的会话反复被恢复」，生产里极难归因。

        **为什么不做成纯 SQL 表达式。** 终态判据藏在 `payload` JSON 里
        （`SessionStatusChanged.payload["new_status"]`），提取要方言分叉
        （SQLite `json_extract` vs Postgres `->>`）。参考宿主那版纯 SQL 判据
        （`max(opened.id) > max(finished.id)`）**完全忽略了 `SessionStatusChanged`**——
        经它终结的会话会被永远报成 active。

        **两步查询的等价性**：先 `SELECT DISTINCT session_id` 把所有出现过的 session 置
        为 active（种子），再按 id 升序重放生命周期事件。`InMemoryEventStore` 是在每个
        session 的**首次出现**处 add 的；由于操作只有 add/discard 且逐 session 独立，
        「在 -∞ 处 add」与「在首个事件处 add」结果完全一致——首个事件必然先于该 session
        的其余事件。

        代价是行数 = 会话数 × 每会话几条生命周期事件，且只在启动时调一次。真的大到不能
        接受时，正确的下一步是加一张 session 状态投影表，**而不是**把判据塞回 SQL 表达式。
        """
        async with self._factory() as db:
            seen = await db.execute(select(EventModel.session_id).distinct())
            # 在 session 内就物化成 Event：ORM 行出了 session 就是 detached 实例，
            # 靠「属性已加载所以还能读」是脆的，且与 memory/sql provider 的既有写法不一致。
            session_ids = list(seen.scalars().all())
            lifecycle = await db.execute(
                select(EventModel)
                .where(EventModel.type.in_(LIFECYCLE_EVENT_TYPES))
                .order_by(EventModel.id)
            )
            events = [_row_to_event(r) for r in lifecycle.scalars().all()]
        return list(replay_lifecycle(session_ids, events))

    # ── 快照 ──────────────────────────────────────────────────────────────────

    async def save_snapshot(self, snapshot: RunSnapshot) -> None:
        async with self._factory() as db, db.begin():
            db.add(SnapshotModel(
                id=snapshot.id,
                session_id=snapshot.session_id,
                run_id=snapshot.run_id or "",
                last_event_id=snapshot.last_event_id,
                last_event_sequence=snapshot.last_event_sequence,
                state_blob_json=json.dumps(snapshot.state_blob),
                snapshot_reason=snapshot.snapshot_reason,
                created_at=snapshot.snapshot_at,
            ))
            await db.flush()          # 让新行参与下面的「保留最新」排序
            await self._prune_snapshots(db, snapshot.session_id)

    async def _prune_snapshots(self, db: AsyncSession, session_id: str) -> None:
        """删除该 session 除最新 keep_snapshots 张之外的旧快照。

        恢复只取最新一张（`load_latest_snapshot`），逐 RunFinished 定期写入会让旧快照
        无界累积，故每次写入后顺手清理。按 id（ULID，时间可排序）取最新 N 个保留；
        子查询带 LIMIT，Postgres / SQLite 均支持。
        """
        keep_ids = (
            select(SnapshotModel.id)
            .where(SnapshotModel.session_id == session_id)
            .order_by(SnapshotModel.id.desc())
            .limit(self._keep_snapshots)
        )
        await db.execute(
            delete(SnapshotModel).where(
                SnapshotModel.session_id == session_id,
                SnapshotModel.id.not_in(keep_ids),
            )
        )

    async def load_latest_snapshot(self, session_id: str) -> RunSnapshot | None:
        async with self._factory() as db:
            result = await db.execute(
                select(SnapshotModel)
                .where(SnapshotModel.session_id == session_id)
                .order_by(SnapshotModel.created_at.desc(), SnapshotModel.id.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return RunSnapshot(
                id=row.id,
                run_id=row.run_id,
                session_id=row.session_id,
                last_event_id=row.last_event_id,
                last_event_sequence=row.last_event_sequence,
                state_blob=json.loads(row.state_blob_json),
                snapshot_reason=row.snapshot_reason,
                snapshot_at=row.created_at,
            )


def _row_to_event(row: EventModel) -> Event:
    return Event(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        session_id=row.session_id,
        type=row.type,
        timestamp=row.timestamp,
        tenant_id=row.tenant_id,
        task_id=row.task_id,
        agent_id=row.agent_id,
        payload=json.loads(row.payload_json),
        metadata=json.loads(row.metadata_json),
        causation_id=row.causation_id,
        # 存量行（参考宿主写的）该列为 NULL → 回落 1，零迁移。
        schema_version=row.schema_version if row.schema_version is not None else 1,
    )


@asynccontextmanager
async def open_sqlite_event_store(
    db_path: str | Path,
    *,
    keep_snapshots: int = 3,
) -> AsyncIterator[SqlEventStore]:
    """开一个 SQLite backed 的 event store（建表 → yield → dispose）。

    测试与单机部署用。宿主接 postgres 时自带 engine / migration，直接构造
    ``SqlEventStore(session_factory)`` 即可，不必走这里。
    """
    engine, factory = make_session_factory(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield SqlEventStore(factory, keep_snapshots=keep_snapshots)
    finally:
        await engine.dispose()
```

- [ ] **Step 2: 运行一致性套**

Run: `uv run pytest tests/unit/test_event_store_conformance.py -q`
Expected: 38 passed（两个实现各 19）。

若 `test_snapshot_roundtrip` 的 `snapshot_at` 断言失败，检查 `UtcDateTime` 是否用在了 `created_at` 上。若 `test_read_after_*` 失败，检查是否误用了 `sequence` 排序而非 `id`。

- [ ] **Step 3: 确认可选依赖边界**

Run: `uv run python -c "import ctx_weft.providers.events as m; print(m.__all__)"`
Expected: 输出里**没有** `SqlEventStore`（它只能从 `providers.events.store.sql` 显式导入）。

- [ ] **Step 4: 全量回归**

Run: `uv run pytest -q`
Expected: 除 `test_l05_demotion_blob_lifecycle.py` 外全绿。

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(events/sql): SqlEventStore，七个协议方法齐备

list_active_session_ids 不走纯 SQL 表达式：终态判据藏在 payload JSON 里，提取要方言
分叉，而参考宿主那版纯 SQL 判据完全忽略了 SessionStatusChanged。改成「DISTINCT
session_id 做种子 + 按 id 重放生命周期事件」，判据复用 _lifecycle 里两个实现共用的
状态机——与内存版一致是结构性的，不是靠对齐维护的。

一致性测试套现在两个实现各跑 19 条。"
```

---

## Task 12: blob GC 联动测试 + L0.5 生命周期测试改写

**Files:**
- Create: `tests/unit/test_blob_gc_integration.py`
- Modify: `tests/unit/test_l05_demotion_blob_lifecycle.py`

**Interfaces:**
- Consumes: Task 5 的 `SqlMemoryProvider.live_blob_refs()`；`FsBlobStore.collect(live_refs, now=)`
- Produces: 无（纯测试）

- [ ] **Step 1: 写联动 GC 测试**

`tests/unit/test_blob_gc_integration.py`：

```python
"""mark-sweep 联动：SqlMemoryProvider.live_blob_refs() × FsBlobStore.collect()。

这是**真实的生产组合**——引用边在 SQL（与 ingest 同事务），字节在文件系统。
两者靠 ref 串起来，谁都不知道对方的存在。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.content import normalize_content, rehydrate_content
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.context import ImagePart, TextPart
from ctx_weft.providers.blob.fs import FsBlobStore
from ctx_weft.providers.memory.sql import open_sqlite_memory

_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64
_LATER = datetime.now(UTC) + timedelta(days=2)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _event(content) -> MemoryEvent:
    return MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s1", task_id="t1", agent_id="a1"),
        content=content,
        timestamp=datetime.now(UTC),
        role="user",
    )


async def test_live_blob_survives_collection(tmp_path):
    blobs = FsBlobStore(tmp_path / "blobs")
    async with open_sqlite_memory(tmp_path / "mem.db") as mem:
        ref = await blobs.put(_PNG, "image/png", _ctx())
        await mem.ingest(_event([ImagePart(data=ref, media_type="image/png",
                                          source_type="ref")]), _ctx())
        assert await blobs.collect(await mem.live_blob_refs(), now=_LATER) == 0
        assert await blobs.get(ref, _ctx()) is not None


async def test_orphaned_blob_is_collected_after_fold(tmp_path):
    """记录被 fold → 活引用归零 → 过宽限期被回收 → rehydrate 降级成占位。"""
    blobs = FsBlobStore(tmp_path / "blobs")
    async with open_sqlite_memory(tmp_path / "mem.db") as mem:
        ref = await blobs.put(_PNG, "image/png", _ctx())
        content = [ImagePart(data=ref, media_type="image/png", source_type="ref")]
        rid = await mem.ingest(_event(content), _ctx())

        await mem.fold([rid], [], _ctx())
        assert await mem.live_blob_refs() == set()
        assert await blobs.collect(await mem.live_blob_refs(), now=_LATER) == 1

        out = await rehydrate_content(content, blob_store=blobs, ctx=_ctx())
        assert len(out) == 1
        assert out[0].text == "[image unavailable: image/png]"


async def test_grace_period_protects_the_put_to_ingest_window(tmp_path):
    """已 put、尚未 ingest 的窗口——只按活引用删会把刚上传的图删掉。"""
    blobs = FsBlobStore(tmp_path / "blobs")
    async with open_sqlite_memory(tmp_path / "mem.db") as mem:
        ref = await blobs.put(_PNG, "image/png", _ctx())
        assert await mem.live_blob_refs() == set()      # 还没有引用边
        assert await blobs.collect(await mem.live_blob_refs()) == 0   # 真实 now：宽限期内
        assert await blobs.get(ref, _ctx()) is not None


async def test_entry_normalize_then_gc_roundtrip(tmp_path):
    """入口外部化 → 落库 → 取回，全链路走真实组合。"""
    import base64

    blobs = FsBlobStore(tmp_path / "blobs")
    async with open_sqlite_memory(tmp_path / "mem.db") as mem:
        raw = [TextPart(text="看这个"),
               ImagePart(data=base64.b64encode(_PNG).decode(), media_type="image/png")]
        normalized = await normalize_content(raw, blob_store=blobs, ctx=_ctx())
        await mem.ingest(_event(normalized), _ctx())

        assert len(await mem.live_blob_refs()) == 1
        assert await blobs.collect(await mem.live_blob_refs(), now=_LATER) == 0

        back = await rehydrate_content(normalized, blob_store=blobs, ctx=_ctx())
        assert base64.b64decode(back[1].data) == _PNG
```

- [ ] **Step 2: 运行**

Run: `uv run pytest tests/unit/test_blob_gc_integration.py -q`
Expected: 4 passed

- [ ] **Step 3: 改写 L0.5 生命周期测试**

`tests/unit/test_l05_demotion_blob_lifecycle.py` 现在用 `SqlMemoryProvider` 兼当 blob store。改成 `SqlMemoryProvider` + `FsBlobStore` 的真实组合：

- 建 store：`blobs = FsBlobStore(tmp_path / "blobs")`，用它 `put` 字节
- `await mem.collect_blobs()` → `await blobs.collect(await mem.live_blob_refs(), now=_LATER)`
- `assert await mem.collect_blobs() == 0` → `assert await blobs.collect(await mem.live_blob_refs(), now=_LATER) == 0`
- 取字节的断言从 `mem.get(ref, ctx)` 改为 `blobs.get(ref, ctx)`
- 模块 docstring 更新：说明这条链现在跨两个组件（引用边在 SQL、字节在 FS），正是生产形态

**保持不变**：L0.5 降级本身的行为断言（占位含完整 ref、`blob_refs` 累积声明、`media:get_image` 能取回）。

- [ ] **Step 4: 运行**

Run: `uv run pytest tests/unit/test_l05_demotion_blob_lifecycle.py -q`
Expected: 全绿

- [ ] **Step 5: 全量回归**

Run: `uv run pytest -q`
Expected: **全绿，0 失败。**

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test(blob): mark-sweep 联动测试，L0.5 生命周期改用真实生产组合

live_blob_refs() × FsBlobStore.collect() 是引用边与字节分居两处之后的真实形态。
test_l05_demotion_blob_lifecycle 原先测的是「SQL 自己存自己收」，现在测跨组件的
那条链——覆盖的是宿主真正会跑的路径。"
```

---

## Task 13: README 升级须知 + 宿主迁移文档

**Files:**
- Modify: `README.md`
- Rewrite: `docs/host-migration-to-sql-memory.md`
- Modify: `ARCHITECTURE.md`（若含 provider 路径引用）

**Interfaces:**
- Consumes: Task 1–12 全部
- Produces: 无（纯文档）

- [ ] **Step 1: README 加升级须知**

在现有「升级须知」段落之后追加：

```markdown
## 升级须知（providers 目录重组）

**破坏性变更：provider 的 import 路径全部变了。** 按「领域 → 协议 → 变体」重组，
不留兼容 shim：

| 旧 | 新 |
|---|---|
| `ctx_weft.providers.memory_blackboard` | `ctx_weft.providers.memory.in_memory` |
| `ctx_weft.providers.memory_sql` | `ctx_weft.providers.memory.sql` |
| `ctx_weft.providers.blob_fs` | `ctx_weft.providers.blob.fs` |
| `ctx_weft.providers.events.bus` | `ctx_weft.providers.events.bus.in_process` |
| `ctx_weft.providers.events.store` | `ctx_weft.providers.events.store.in_memory` |

`ctx_weft.providers.events` 顶层的 `InProcessEventBus` / `InMemoryEventStore`
两个名字不变。

## 升级须知（blob 字节移出 memory）

- **破坏性变更：`SqlMemoryProvider` 不再实现 `MemoryBlobStore` / `EventBlobStore`。**
  它只维护 `memory_blob_refs` 引用边（那部分需要与 ingest 同事务），字节归 blob store。
  接线改成显式注册：

  ```python
  blobs = FsBlobStore(Path("/var/lib/app/blobs"))
  registry.register_memory_blob_store(blobs)
  registry.register_event_blob_store(blobs)   # 共用一个实例是允许的
  ```

- **`ProviderRegistry.get_memory_blob_store()` 不再自动回落到 memory provider。**
  现在与 `get_event_blob_store()` 一样只有两级：显式注册 > `NullMemoryBlobStore`。
  没注册就是「不接 blob」，携图会话会在入口被拒（`BlobStoreRequiredError`）。

- **回收方式变了。** 原先 `await memory.collect_blobs()` 一步搞定，现在是标准的
  mark-sweep 两步——mark 在 memory（它持有引用边），sweep 在 blob store（它持有字节）：

  ```python
  await blobs.collect(await memory.live_blob_refs())
  ```

  ⚠️ **共用一个 blob store 实例时，`live_refs` 必须同时含两侧的活引用**，只喂 memory
  侧会删掉事件流仍需要的字节。想省心就分开部署两个实例，各按各的策略回收。

## 升级须知（事件持久化）

- **`InMemoryEventStore` 不再接受 `event_bus=` 参数，`append()` 也不再过滤瞬态事件。**
  订阅与过滤都归新的 `EventPersister`。若你此前直接调 `event_store.append()`，注意
  每 token 一个的流式 delta 现在会真的落库——改用 `EventPersister` 或自己加过滤。

- **接线改用 `attach_persistence`：**

  ```python
  from ctx_weft.providers.events import attach_persistence

  handle = attach_persistence(runtime.event_bus, my_store, snapshot_every_n=50)
  # ...
  await handle.detach()
  ```

  `snapshot_every_n > 0` 时会一并接上 `SnapshotWriter`（**默认不接**，现有行为零变化）。
  接上之后崩溃恢复从 O(全部事件) 全量回放退化成「最新快照 + 增量」。
  顺序（persister 必须先于 snapshot writer）由这个函数保证，别手动分别 subscribe。

- **新增 SQL 事件持久化**（需 `ctx-weft[sql]`）：

  ```python
  from ctx_weft.providers.events.store.sql import open_sqlite_event_store

  async with open_sqlite_event_store("app-events.db") as store:
      attach_persistence(runtime.event_bus, store, snapshot_every_n=50)
  ```
```

- [ ] **Step 2: 更新 README 里的 provider 路径引用**

```bash
grep -n "providers/events/bus.py\|providers/events/store.py\|memory_blackboard\|memory_sql\|blob_fs" README.md ARCHITECTURE.md
```
逐条改成新路径。README 里那张组件表的行号引用（`src/ctx_weft/providers/events/bus.py:36` 等）也要更新——重新 grep 取实际行号，不要照抄。

- [ ] **Step 3: 重写 `docs/host-migration-to-sql-memory.md`**

现有内容教宿主「注册 `SqlMemoryProvider` 即自动获得 blob 能力」，整条路径已不存在。重写要点：

1. 包路径 `ctx_weft.providers.memory.sql`
2. blob 单独注册 `FsBlobStore`，两侧各注册一次（或分开两个实例）
3. 回收从 `collect_blobs()` 改成 `blobs.collect(await memory.live_blob_refs())`
4. 新增一节：事件持久化也可以用 `SqlEventStore`，与 memory 共享同一个 engine 的写法
   （两次 `create_all`，见 spec §6.1 的代码块）
5. `memory_subscriptions` 唯一索引那条既有的破坏性要求（须先 DROP 旧三列唯一索引）**保留**

- [ ] **Step 4: 核对文档里的代码块能跑**

把 README 与迁移文档里新增的每个代码块抄进一个临时脚本跑一遍：

```bash
uv run python - <<'PY'
import asyncio, tempfile
from pathlib import Path
from ctx_weft.providers.blob.fs import FsBlobStore
from ctx_weft.providers.events import attach_persistence, InProcessEventBus
from ctx_weft.providers.events.store.sql import open_sqlite_event_store

async def main():
    with tempfile.TemporaryDirectory() as d:
        blobs = FsBlobStore(Path(d) / "blobs")
        async with open_sqlite_event_store(Path(d) / "e.db") as store:
            h = attach_persistence(InProcessEventBus(), store, snapshot_every_n=50)
            await h.detach()
    print("ok")

asyncio.run(main())
PY
```
Expected: `ok`

- [ ] **Step 5: 全量回归 + lint**

Run: `uv run pytest -q`
Expected: 全绿

Run: `uv run ruff check src/ tests/`
Expected: 无错误（有的话修掉——多半是搬运后残留的未使用 import）

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: providers 重组 / blob 归属 / 事件持久化三段升级须知

blob 回收从一步 collect_blobs() 变成标准的 mark-sweep 两步——mark 在 memory（持有
引用边），sweep 在 blob store（持有字节）。宿主迁移文档按新接线重写。"
```

---

## Self-Review

**Spec coverage:**

| Spec 节 | 覆盖任务 |
|---|---|
| §4 目录布局 | Task 1 / 2 / 3 |
| §4 `__init__` 导出边界 | Task 1 Step 2、Task 2 Step 3 |
| §5.1–5.2 删字节 / 留引用边 / 补 `live_blob_refs` | Task 5 |
| §5.3 解析对称化 | Task 6 |
| §5.4 文档口径 | Task 6 Step 5–6、Task 13 Step 3 |
| §6.1 共享 SQL 基建 | Task 4 |
| §6.2 `SqlEventStore`（含补两列） | Task 10 / 11 |
| §6.3 `list_active_session_ids` | Task 7（`_lifecycle`）+ Task 11 + Task 9 的 6 条状态机用例 |
| §6.4 `EventPersister` | Task 7 |
| §6.5 `SnapshotWriter` + `attach_persistence` | Task 8 |
| §6.6 runtime 接线 | Task 7 Step 7 |
| §7 破坏性变更 8 条 | Task 13 Step 1 |
| §8 测试 | Task 9 / 12 + 各任务自带 |
| §9 风险 3（迁移独立 commit） | Task 1 / 2 / 3 各自成 commit |

无缺口。

**类型一致性核对：**
- `live_blob_refs() -> set[str]`：Task 5 定义，Task 12、Task 13 使用，均为 `set[str]`、元素带 `blob:` 前缀 ✓
- `apply_lifecycle(active, event) -> None` / `replay_lifecycle(session_ids, events) -> set[str]`：Task 7 定义，Task 11 使用 ✓
- `attach_persistence(bus, store, *, snapshot_every_n=0) -> PersistenceHandle`：Task 7 定义（snapshot 分支惰性 import Task 8 的 `SnapshotWriter`），Task 8/13 使用 ✓
- `SnapshotWriter(store, bus=None, *, every_n_events=50)`：Task 8 定义，Task 7 的 `attach_persistence` 按此调用 ✓
- `SqlEventStore(factory, *, keep_snapshots=3)`：Task 11 定义，Task 10 的 `__init__` 与 Task 11 的 `open_sqlite_event_store` 按此调用 ✓

**已知的跨任务时序约束（执行者必读）：**
- Task 7 Step 4 的 `attach_persistence` 里 import 了 Task 8 才创建的 `snapshot.py`——该 import 在**函数体内**，`snapshot_every_n=0`（默认）时不执行，故 Task 7 单独可跑通、其测试也全绿。Task 8 落地后该分支才被覆盖。
- Task 5 之后到 Task 12 之前，`test_l05_demotion_blob_lifecycle.py` 处于已知红色状态。执行者在这段区间做全量回归时，**只需确认失败集合恰好是这一个文件**。
