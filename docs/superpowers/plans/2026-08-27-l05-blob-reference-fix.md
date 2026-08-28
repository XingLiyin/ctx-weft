# L0.5 blob 引用缺陷修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 L0.5 降级后的 blob 不再被 GC 误删——ref 永远以结构化形式可采集，而不是只活在占位文本里。

**Architecture:** `MemoryEvent` / `MemoryRecord` 增加 `blob_refs: list[str]` 字段；L0.5 补偿记录把降级掉的 ref 显式带上；mark 判据收成单一函数 `collect_blob_refs`（结构化 ref part ∪ `blob_refs`），provider 的 ingest 改用它建引用边、load_view 从引用表反查还原。blob 协议、回收 SQL、宽限期均不动。

**Tech Stack:** Python 3.11 / SQLAlchemy 2.0 async / pytest（asyncio auto 模式，测试直接 `async def`，不加 `@pytest.mark.asyncio`）

**Spec:** `docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md`

## Global Constraints

- **纯文本路径逐字节不变**：所有新增字段默认空，不接 blob 的宿主行为零变化。这是全仓贯穿的硬约束。
- **不解析任何占位文案**：mark 判据只看结构化字段。`core/media/refs.py` 仍是本仓唯一知道占位长什么样的地方，`content.py` / provider 一律不得解析占位。
- **不改 blob 协议**：`MemoryBlobStore` 保持 `can_externalize` / `put` / `get` 三件套，不加 `retain` / `release`。
- **不改回收 SQL 与宽限期**：`SqlMemoryProvider.collect_blobs` 的 DELETE 语句与 `_blob_grace` 一个字都不改。
- **无存量迁移**：分支未上线，不写迁移脚本。
- **判据冻结**：图片 part 的判据仍是 `not hasattr(p, "text")`（spec §13 冻结），本次不解冻。
- **基线**：全量测试 `3 failed / 5 skipped / 1 xfail`。三个既存失败（`test_compact_flow_e2e` / `test_golden_conformance` / `test_observe_outcomes`）与本工作无关，不得增减。任务 5 结束后 xfail 应归零。

---

## File Structure

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `src/ctx_weft/protocols/memory.py` | `MemoryEvent` / `MemoryRecord` 协议 | 各加一个 `blob_refs` 字段 + 契约说明 |
| `src/ctx_weft/core/content.py` | 内容形态归一层 | 新增 `collect_blob_refs`（mark 判据单一真源） |
| `src/ctx_weft/core/media/fold.py` | L0.5 降级执行 | `_rebuild` 填 `blob_refs`（累积旧的 + 本次的） |
| `src/ctx_weft/providers/memory_sql/provider.py` | SQL provider | ingest 改用 `collect_blob_refs`；load_view 反查还原 `blob_refs` |
| `tests/unit/test_l05_demotion_blob_lifecycle.py` | 缺陷复现（已存在） | 删 `xfail` 标记 |
| `tests/unit/test_collect_blob_refs.py` | mark 判据单测 | 新建 |

---

### Task 1: `blob_refs` 字段进协议

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（`MemoryEvent` 与 `MemoryRecord` 两个 dataclass）
- Test: `tests/unit/test_collect_blob_refs.py`（新建，本任务只放字段默认值用例）

**Interfaces:**
- Produces: `MemoryEvent.blob_refs: list[str]`、`MemoryRecord.blob_refs: list[str]`，默认空列表。后续任务全部依赖这两个字段名。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_collect_blob_refs.py`：

```python
"""blob_refs 字段 + collect_blob_refs（mark 判据）。

缺陷记录：docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryEvent, MemoryKind, MemoryRecord,
    MemoryScope, TextPart,
)


def _addr() -> MemoryAddress:
    return MemoryAddress(session_id="ses_1", agent_id="agt_1", task_id="tsk_1")


def _event(**kw) -> MemoryEvent:
    base = dict(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc), content="hi",
    )
    base.update(kw)
    return MemoryEvent(**base)


def test_event_blob_refs_defaults_empty() -> None:
    assert _event().blob_refs == []


def test_record_blob_refs_defaults_empty() -> None:
    rec = MemoryRecord(id="rec_1", type="conversation_turn", content="hi")
    assert rec.blob_refs == []


def test_blob_refs_is_not_shared_between_instances() -> None:
    """default_factory 而非可变默认值——共享 list 会让一条记录的 ref 污染另一条。"""
    a, b = _event(), _event()
    a.blob_refs.append("blob:aaa")
    assert b.blob_refs == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_collect_blob_refs.py`
Expected: FAIL — `TypeError: MemoryEvent.__init__() got an unexpected keyword argument` 之前先是 `AttributeError: 'MemoryEvent' object has no attribute 'blob_refs'`

- [ ] **Step 3: 加字段**

`src/ctx_weft/protocols/memory.py`，`MemoryEvent` 里 `metadata` 字段之后加：

```python
    # GC 的 mark 输入（缺陷 2026-08-27）：本事件引用了哪些 blob，但**没有**以结构化
    # ImagePart(source_type="ref") 形式出现在 content 里。
    #
    # 唯一的填写方——L0.5 降级（core/media/fold.py）：它把 ImagePart(ref) 换成文本
    # 占位，ref 就此掉进自由文本，provider 再也扫不出来 → 引用归零 → 字节被回收 →
    # media:get_image 取不回，L0.5 承诺的「可逆」失效。
    #
    # 普通写侧**不必填**：content 里结构化的 ref part 由 collect_blob_refs 自动采集。
    # provider 建引用边时一律走 core.content.collect_blob_refs，不得自行判断。
    blob_refs: list[str] = field(default_factory=list)
```

`MemoryRecord` 里 `metadata` 之后加：

```python
    # 与 MemoryEvent.blob_refs 对称的读侧回显（缺陷 2026-08-27）。
    # provider 必须还原它——否则一条被降级过两次的记录，第一次降的 ref 在第二次
    # 重建补偿事件时就没人认领了（core/media/fold.py::_rebuild 要累积它）。
    blob_refs: list[str] = field(default_factory=list)
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_collect_blob_refs.py`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/protocols/memory.py tests/unit/test_collect_blob_refs.py
git commit -m "feat(protocols): MemoryEvent/MemoryRecord 加 blob_refs，供 GC 的 mark 采集"
```

---

### Task 2: `collect_blob_refs` —— mark 判据单一真源

**Files:**
- Modify: `src/ctx_weft/core/content.py`（`extract_blob_refs` 之后新增；`__all__` 登记）
- Test: `tests/unit/test_collect_blob_refs.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `MemoryEvent.blob_refs`
- Produces: `collect_blob_refs(event: Any) -> list[str]` —— 返回去重后的 ref 列表，顺序为「content 里结构化的（视图顺序）在前，`blob_refs` 声明的在后」。Task 4 的 provider 调它。

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_collect_blob_refs.py`：

```python
from ctx_weft.core.content import collect_blob_refs

_REF_A = "blob:aaaaaaaa"
_REF_B = "blob:bbbbbbbb"


def _img(ref: str) -> ImagePart:
    return ImagePart(data=ref, media_type="image/png", source_type="ref")


def test_collects_structural_ref_parts() -> None:
    ev = _event(content=[TextPart(text="看图"), _img(_REF_A)])
    assert collect_blob_refs(ev) == [_REF_A]


def test_collects_declared_blob_refs() -> None:
    """L0.5 之后的形态：content 里只剩文本占位，ref 靠 blob_refs 声明。"""
    ev = _event(content=[TextPart(text="[image blob:aaaaaaaa media_type=image/png]")],
                blob_refs=[_REF_A])
    assert collect_blob_refs(ev) == [_REF_A]


def test_unions_both_sources_without_duplicates() -> None:
    ev = _event(content=[_img(_REF_A)], blob_refs=[_REF_A, _REF_B])
    assert collect_blob_refs(ev) == [_REF_A, _REF_B]


def test_plain_text_yields_nothing() -> None:
    assert collect_blob_refs(_event(content="纯文本")) == []
    assert collect_blob_refs(_event(content=None)) == []


def test_does_not_parse_placeholder_text() -> None:
    """占位文案不是判据——没有 blob_refs 声明就采不到，这是刻意的。

    解析文案会把占位格式知识泄进归一层，而 core/media/refs.py 是本仓唯一
    知道占位长什么样的地方。声明式采集正是为了避免那种耦合。
    """
    ev = _event(content=[TextPart(text=f"[image {_REF_A} media_type=image/png]")])
    assert collect_blob_refs(ev) == []


def test_ignores_non_ref_image_parts() -> None:
    """inline base64 不是 blob 引用。"""
    ev = _event(content=[ImagePart(data="iVBORw0KGgo=", media_type="image/png")])
    assert collect_blob_refs(ev) == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_collect_blob_refs.py`
Expected: FAIL — `ImportError: cannot import name 'collect_blob_refs'`

- [ ] **Step 3: 实现**

`src/ctx_weft/core/content.py`，紧跟 `extract_blob_refs` 之后：

```python
def collect_blob_refs(event: Any) -> list[str]:
    """GC 的 **mark 判据**：一个 MemoryEvent 引用了哪些 blob。provider 建引用边只走这里。

    两个来源的并集，都**只看结构化字段**：

    1. ``content`` 里的 ref part（``extract_blob_refs``，与 ``rehydrate_content``
       会去 get 的那批逐一对应）；
    2. ``event.blob_refs`` 的显式声明——L0.5 降级把 ``ImagePart(ref)`` 换成文本占位后，
       ref 只能靠这条传递（缺陷 2026-08-27）。

    **绝不解析占位文案。** 占位格式的唯一真源是 ``core/media/refs.py``；让 mark 判据
    去解析它，等于把文案格式变成 GC 正确性的一部分——文案一改，图就开始被误删，而且
    要到一个宽限期之后才看得出来。声明式采集把这个耦合彻底切断。

    去重保序：content 里的在前（视图顺序），声明的在后。顺序不影响正确性，只为让
    引用表的写入顺序稳定、便于比对。
    """
    seen: dict[str, None] = {}
    for ref in extract_blob_refs(getattr(event, "content", None)):
        seen.setdefault(ref, None)
    for ref in getattr(event, "blob_refs", None) or ():
        text = str(ref or "")
        if text.startswith(BLOB_REF_PREFIX):
            seen.setdefault(text, None)
    return list(seen)
```

`__all__` 里 `"extract_blob_refs"` 之后加 `"collect_blob_refs"`。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_collect_blob_refs.py`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/content.py tests/unit/test_collect_blob_refs.py
git commit -m "feat(content): collect_blob_refs —— mark 判据收成单一真源，不解析占位文案"
```

---

### Task 3: L0.5 补偿记录带上 `blob_refs`

**Files:**
- Modify: `src/ctx_weft/core/media/fold.py`（`_rebuild`）
- Test: `tests/unit/test_media_fold.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `MemoryEvent.blob_refs` / `MemoryRecord.blob_refs`
- Produces: `_rebuild` 产出的 `MemoryEvent.blob_refs` = 原记录已有的 + 本次降级掉的（去重保序）

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_media_fold.py`（沿用该文件既有的 import 与桩；若无 `_rebuild` 的直接测试，按下面新建一节）：

```python
from ctx_weft.core.media.fold import _rebuild
from ctx_weft.protocols import ImagePart, MemoryScope, TextPart


def test_rebuild_declares_demoted_refs() -> None:
    """降级掉的 ref 必须进 blob_refs——否则 GC 采不到，图过宽限期被删。"""
    rec = _make_record(content=[
        TextPart(text="看图"),
        ImagePart(data="blob:aaaa", media_type="image/png", source_type="ref"),
    ])
    ev = _rebuild(rec, (1,), MemoryScope.TASK)
    assert ev is not None
    assert ev.blob_refs == ["blob:aaaa"]
    assert not hasattr(ev.content[1], "data"), "降级后该 part 应是 TextPart 占位"


def test_rebuild_accumulates_previously_declared_refs() -> None:
    """两次降级：第一次降的 ref 不能在第二次重建时丢掉。"""
    rec = _make_record(
        content=[
            TextPart(text="[image blob:aaaa media_type=image/png]"),
            ImagePart(data="blob:bbbb", media_type="image/png", source_type="ref"),
        ],
        blob_refs=["blob:aaaa"],
    )
    ev = _rebuild(rec, (1,), MemoryScope.TASK)
    assert ev is not None
    assert ev.blob_refs == ["blob:aaaa", "blob:bbbb"]


def test_rebuild_without_demotion_keeps_existing_refs() -> None:
    """同刻组里被原样重写的记录：不降级，但已有的声明要照抄。"""
    rec = _make_record(content=[TextPart(text="纯文本")], blob_refs=["blob:aaaa"])
    ev = _rebuild(rec, (), MemoryScope.TASK)
    assert ev is not None
    assert ev.blob_refs == ["blob:aaaa"]
```

若该文件没有 `_make_record` 辅助，在文件内新增：

```python
def _make_record(*, content, blob_refs=None):
    from datetime import datetime, timezone
    from ctx_weft.protocols import MemoryAddress, MemoryKind, MemoryRecord, MemoryScope
    return MemoryRecord(
        id="rec_1", type="conversation_turn", kind=MemoryKind.CONVERSATION_TURN,
        scope=MemoryScope.TASK, content=content, role="user",
        timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc),
        address=MemoryAddress(session_id="ses_1", agent_id="agt_1", task_id="tsk_1"),
        blob_refs=list(blob_refs or []),
    )
```

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_media_fold.py -k rebuild`
Expected: FAIL — `assert [] == ['blob:aaaa']`

- [ ] **Step 3: 实现**

`src/ctx_weft/core/media/fold.py::_rebuild`，把 `if indices:` 那段改成同时收集 ref，并在构造 `MemoryEvent` 时传入：

```python
    demoted: list[str] = []
    if indices:
        new = list(content)
        for i in indices:
            ref = demotable_ref(content[i])
            if ref is None:            # 计划与内容对不上（不该发生）→ 整条放弃，别写坏占位
                return None
            new[i] = TextPart(text=encode_image_placeholder(ref, _media_type(content[i])))
            demoted.append(ref)
        content = new
    # 降级掉的 ref 必须显式声明（缺陷 2026-08-27）：占位是 TextPart，provider 的
    # mark 判据只看结构化字段，扫不出文本里的 ref。不声明 → 引用归零 → 字节过宽限期
    # 被回收 → media:get_image 取不回，L0.5 的「可逆」失效。
    # 累积而非覆盖：本记录可能已被降级过一轮，那一轮的 ref 只在它的 blob_refs 里。
    prior = list(getattr(rec, "blob_refs", None) or ())
    blob_refs = list(dict.fromkeys([*prior, *demoted]))
```

`MemoryEvent(...)` 的参数列表里加一行 `blob_refs=blob_refs,`。

同时把模块 docstring 里「`causation_id` 不在 `MemoryRecord` 上，无从照抄」那段之后补一句：

```
    ``blob_refs`` 是**累积**的（旧的 + 本次降级的），不是照抄——见字段本身的注释。
```

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_media_fold.py`
Expected: 全部 passed（既有用例不得回归）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/media/fold.py tests/unit/test_media_fold.py
git commit -m "fix(media): L0.5 补偿记录显式声明降级掉的 ref，GC 不再采不到"
```

---

### Task 4: provider 用新判据建引用边，并还原 `blob_refs`

**Files:**
- Modify: `src/ctx_weft/providers/memory_sql/provider.py`（`ingest` 的引用边循环；`_row_to_record` 及其两个调用点 `load_view` / `recall_topic`）
- Test: `tests/unit/test_sql_blob_store.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `collect_blob_refs`
- Produces: `_row_to_record(row, blob_refs: list[str] | None = None) -> MemoryRecord`；`load_view` / `recall_topic` 返回的 `MemoryRecord.blob_refs` 被填上

- [ ] **Step 1: 写失败测试**

追加到 `tests/unit/test_sql_blob_store.py`（沿用该文件既有的 provider fixture）：

```python
async def test_ingest_registers_declared_blob_refs(sql_memory) -> None:
    """blob_refs 声明的 ref 也要建引用边——只扫 content 会漏掉 L0.5 的占位。"""
    ctx = ProviderContext(session_id="ses_1", tenant_id="default")
    ref = await sql_memory.put(b"bytes-here", "image/png", ctx)
    await sql_memory.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc),
        content=[TextPart(text=f"[image {ref} media_type=image/png]")],
        blob_refs=[ref],
    ), ctx)
    assert await sql_memory.collect_blobs() == 0, "有声明的引用，不该回收"
    assert await sql_memory.get(ref, ctx) is not None


async def test_load_view_restores_blob_refs(sql_memory) -> None:
    """读侧回显——否则第二次降级时第一次的 ref 无人认领。"""
    ctx = ProviderContext(session_id="ses_1", tenant_id="default")
    ref = await sql_memory.put(b"bytes-here", "image/png", ctx)
    await sql_memory.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc),
        content=[TextPart(text=f"[image {ref} media_type=image/png]")],
        blob_refs=[ref],
    ), ctx)
    recs = await sql_memory.load_view(_addr(), MemoryScope.TASK, ctx,
                                      kinds=[MemoryKind.CONVERSATION_TURN])
    assert recs[0].blob_refs == [ref]


async def test_structural_refs_are_not_duplicated_in_blob_refs(sql_memory) -> None:
    """结构化 ref 已在 content 里，回显时不再重复塞进 blob_refs。"""
    ctx = ProviderContext(session_id="ses_1", tenant_id="default")
    ref = await sql_memory.put(b"bytes-here", "image/png", ctx)
    await sql_memory.ingest(MemoryEvent(
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK, address=_addr(),
        role="user", timestamp=datetime.now(timezone.utc),
        content=[ImagePart(data=ref, media_type="image/png", source_type="ref")],
    ), ctx)
    recs = await sql_memory.load_view(_addr(), MemoryScope.TASK, ctx,
                                      kinds=[MemoryKind.CONVERSATION_TURN])
    assert recs[0].blob_refs == []
    assert collect_blob_refs(recs[0]) == [ref], "仍能从 content 采到"
```

文件顶部按需补 import：`from ctx_weft.core.content import collect_blob_refs`、`ImagePart`、`TextPart`。若该文件没有 `_addr()` 与 `sql_memory` fixture，照 `tests/unit/test_l05_demotion_blob_lifecycle.py` 的写法建。

- [ ] **Step 2: 运行，确认失败**

Run: `uv run pytest -q tests/unit/test_sql_blob_store.py -k blob_refs`
Expected: FAIL — 第一条报 `assert 1 == 0`（引用边没建，blob 被回收），第二条报 `assert [] == ['blob:…']`

- [ ] **Step 3: 实现**

**(a) ingest 换判据**（`provider.py` 约 371 行）：

```python
        # blob 引用边：**与事件行同一个事务**。判据走 collect_blob_refs——它是 GC 的
        # mark 单一真源（结构化 ref part ∪ event.blob_refs）。不在这里另写 isinstance，
        # 也不解析占位文案：判据一旦分叉，「哪些 blob 还活着」就会和「出网时哪些 part
        # 会被 rehydrate」对不上，而那正好是「回收删掉了还在用的图」的成因。
        for ref in collect_blob_refs(event):
            db.add(MemoryBlobRefModel(
                event_id=event_id, sha=ref[len(BLOB_REF_PREFIX):]))
```

import 从 `extract_blob_refs` 换成 `collect_blob_refs`（若 `extract_blob_refs` 在本文件已无其它调用方，一并从 import 列表删除）。

**(b) `_row_to_record` 接受回显**（约 170 行）：

```python
def _row_to_record(row: MemoryEventModel,
                   blob_refs: list[str] | None = None) -> MemoryRecord:
```

`MemoryRecord(...)` 参数里加 `blob_refs=list(blob_refs or []),`。

**(c) 两个调用点批量反查**。在模块内新增：

```python
async def _declared_refs(db: Any, rows: Sequence[MemoryEventModel]) -> dict[str, list[str]]:
    """按 event_id 取「**没有**出现在 content 结构化字段里」的那部分引用。

    引用表是 mark 结果的物化，它不区分来源；而 `MemoryRecord.blob_refs` 的语义是
    「声明的、content 里看不见的那些」——两者相减才对得上写侧。不相减的话，
    `collect_blob_refs` 会把结构化 ref 数两遍（去重后无害，但语义漂移，且
    `_rebuild` 的累积会把它们写进补偿记录的 blob_refs，越滚越多）。
    """
    ids = [r.id for r in rows]
    if not ids:
        return {}
    result = await db.execute(
        select(MemoryBlobRefModel.event_id, MemoryBlobRefModel.sha)
        .where(MemoryBlobRefModel.event_id.in_(ids))
    )
    all_refs: dict[str, list[str]] = {}
    for event_id, sha in result.all():
        all_refs.setdefault(event_id, []).append(f"{BLOB_REF_PREFIX}{sha}")
    out: dict[str, list[str]] = {}
    for row in rows:
        refs = all_refs.get(row.id)
        if not refs:
            continue
        structural = set(extract_blob_refs(_row_content(row.content, row.content_format)))
        declared = [r for r in refs if r not in structural]
        if declared:
            out[row.id] = declared
    return out
```

`load_view`（约 421 行）与 `recall_topic`（约 442 行）两处改成：

```python
        declared = await _declared_refs(db, rows)
        return normalize_view([_row_to_record(r, declared.get(r.id)) for r in rows])
```

（`recall_topic` 那处不经 `normalize_view`，照其原样式改：`records = [_row_to_record(r, declared.get(r.id)) for r in rows]`。两处都要在仍持有 `db` 会话的作用域内调用 `_declared_refs`。）

本文件需保留 `extract_blob_refs` 的 import（`_declared_refs` 用它做相减）。

- [ ] **Step 4: 运行，确认通过**

Run: `uv run pytest -q tests/unit/test_sql_blob_store.py tests/unit/test_memory_conformance.py`
Expected: 全部 passed（一致性测试不得回归）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/memory_sql/provider.py tests/unit/test_sql_blob_store.py
git commit -m "fix(memory_sql): 引用边改用 collect_blob_refs，load_view 回显 blob_refs"
```

---

### Task 5: 拆掉 xfail，端到端验收

**Files:**
- Modify: `tests/unit/test_l05_demotion_blob_lifecycle.py`（删 `xfail` 标记与 `pytest` import）
- Modify: `docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md`（状态改已修复）

**Interfaces:**
- Consumes: Task 3 + Task 4 的全部改动

- [ ] **Step 1: 先跑一次，确认缺陷用例已经 XPASS**

Run: `uv run pytest -q -rxX tests/unit/test_l05_demotion_blob_lifecycle.py`
Expected: **FAIL** —— `[XPASS(strict)]`。这是好消息：`strict=True` 在修复后主动转红，正是它的用途。

- [ ] **Step 2: 删掉标记**

删除 `test_demoted_image_survives_blob_collection` 上方整个 `@pytest.mark.xfail(...)` 装饰器；若 `pytest` 至此无其它用途，一并删掉 `import pytest`。

模块 docstring 末尾把「现有测试没覆盖这条链」那段改为：

```
本文件钉住的缺陷已于 2026-08-27 修复（补偿记录经 MemoryEvent.blob_refs 显式声明
降级掉的 ref，provider 的 mark 判据 collect_blob_refs 据此建引用边）。用例保留为
回归防线：任何让 ref 重新只存在于占位文本里的改动，都会让它转红。
```

- [ ] **Step 3: 运行，确认三条全绿**

Run: `uv run pytest -q tests/unit/test_l05_demotion_blob_lifecycle.py`
Expected: 3 passed

- [ ] **Step 4: 全量回归**

Run: `uv run pytest -q`
Expected: `3 failed / 5 skipped`，**xfail 归零**，无新增失败。三个既存失败必须仍是
`test_compact_flow_e2e` / `test_golden_conformance` / `test_observe_outcomes`。

Run: `uv run ruff check --select I,F,E9 --output-format=concise src/ tests/`
Expected: 与改动前一致（既有 I001 不计）

- [ ] **Step 5: 更新缺陷文档状态并提交**

文档首行状态改为：

```
> 状态：**已修复**（2026-08-27）
```

§7「待实施」改为「§7 已实施」，并记下实际落点（`protocols/memory.py` 两个字段、
`content.collect_blob_refs`、`fold._rebuild` 的累积、`memory_sql` 的
`_declared_refs` 反查）。

```bash
git add tests/unit/test_l05_demotion_blob_lifecycle.py docs/superpowers/specs/2026-08-27-l05-demotion-drops-blob-reference.md
git commit -m "test(media): L0.5 引用缺陷已修复，拆掉 xfail 转为回归防线"
```

---

## Self-Review

**Spec 覆盖：** §5.1 blob 协议不动（全局约束，无任务改它）✓ · §5.2 `blob_refs` 字段 → Task 1 ✓ · §5.3 mark 函数 → Task 2 ✓ · §5.4 SQL 与宽限期不动（全局约束）✓ · §5.5 event 侧同构 → 不在本计划范围（属双 blob store 那份 spec，本计划只修 memory 侧）· §5.6 无迁移 ✓ · §6 复现测试已存在 → Task 5 拆标记 ✓

**类型一致性：** `blob_refs: list[str]` 在 Task 1 定义，Task 3（`_rebuild`）、Task 4（`_row_to_record`）、测试中用法一致；`collect_blob_refs(event) -> list[str]` 在 Task 2 定义，Task 4 消费，签名一致。

**已知需要执行者留意的一处：** Task 4(c) 的 `_declared_refs` 必须在持有 `db` 会话的 `async with` 作用域内调用——`load_view` / `recall_topic` 的现有代码在 `async with self._factory() as db` 块里取 `rows`，插入位置要在块内，出块后 session 已关闭。
