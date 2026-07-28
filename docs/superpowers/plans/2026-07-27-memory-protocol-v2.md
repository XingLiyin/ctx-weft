# Memory Protocol v2 落地实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-07-06-memory-protocol-v2-design.md`（含 2026-07-27 record-id 增补）把 memory 协议从 v1（12 类型 × 11 方法）迁移到 v2（4 kind × 8 方法），全程绿灯、每阶段独立可合。

**Architecture:** 四阶段：P1 预清场（杀死写点）→ P2 协议扩容（新词汇 + load_view/fold 落 provider，旧方法变兼容 wrapper，不动调用点）→ P3 调用点迁移（先读后写，策展政策上移框架）→ P4 日落（测试迁移 + 旧方法删除 + 终名切换）。过渡期靠"归一化三元组匹配"让新旧词汇行互通，48 个直调旧方法的测试文件保持绿到 P4。

**Tech Stack:** Python 3.11+，dataclasses，pytest（asyncio auto mode），Windows 环境。

## Global Constraints

- 分支：`feat/memory-protocol-v2`（已建，含 record-id 前置 commit 46feec5）。
- 每个 Task 一个 commit，中文 conventional 风格（`feat(memory): ...` / `refactor(memory): ...`），结尾 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- TDD：新模块先写失败测试；迁移类 task 以既有测试套件为回归网，golden 测试族（test_dispatch_fold_golden / test_cross_layer_fold / capsule 一族）是 compact 语义的主安全网。
- 存量环境失败基线（与本改造无关，验证时排除）：`test_bash_exec_liveness.py::test_bash_exec_idle_timeout_reports_error`、`test_golden_conformance.py::test_golden_dir_present`、`test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`、`test_script_runner.py` 3 例。
- `MemoryEventType` 枚举成员**永不物理删除**（设计 §5.0）：写侧死掉的类型保留为 legacy 词汇。
- postgres provider 在 host 仓，不在本仓改。**host 升级门槛**：P4 合入后 `load_view`/`fold` 为 abstract，host 必须先在 postgres provider 实现二者再升级 ctx-weft（设计文档 §7 本就计划 postgres 侧删分组/count 查询；ingest 幂等 = `ON CONFLICT DO NOTHING`，fold = 单事务）。
- 过渡命名（防同名换义的静默破坏）：
  - P2 引入 `MemoryAddress`（原 MemoryScope 数据类），模块级别名 `MemoryScope = MemoryAddress` 保留到 P4；
  - scope 枚举沿用 `MemoryLayer` 名到 P4 最后一步才改名 `MemoryScope`；两次改名之间隔一个"名字真空"commit，任何漏网引用是 loud NameError 而非静默错型。
- 顺序硬约束：**Task 6（读侧迁移）必须先于 Task 7（写侧词汇切换）**——写侧一旦产出 kind 词汇行，未迁移的按 type 过滤的读点会漏读。

---

### Task 1（P1）: 杀 OBSERVER_SUMMARY 写点 + 估算清单剔除死类型

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/suspend.py:55-64`（删 OBSERVER_SUMMARY ingest 块）
- Modify: `src/ctx_weft/core/runtime.py:195-220`（删 tracking flush 的 OBSERVER_SUMMARY ingest；保留 `agent.fetched_tracking_ids.add(tid)` 记账——先读全函数确认无其他副作用）
- Modify: `src/ctx_weft/core/loop/steps/act.py:302-310`（count_recent 类型清单删 OBSERVER_SUMMARY）
- Modify: `src/ctx_weft/core/loop/steps/prepare.py:245-252`（recall 估算清单删 OBSERVER_SUMMARY）
- Test: `tests/unit/test_observer_summary_dead.py`（新建）

**Interfaces:**
- Produces: 全仓（除 protocols/memory.py 枚举定义与读侧兼容）不再出现 OBSERVER_SUMMARY 引用。

- [x] **Step 1: 写失败测试**——suspend step 执行后 memory 里没有 OBSERVER_SUMMARY 记录：

```python
"""OBSERVER_SUMMARY 写侧已死（v2 设计 §8 P1）：suspend / tracking flush 不再写入。"""
from __future__ import annotations
from ctx_weft.protocols import MemoryEventType

def test_no_observer_summary_writes_in_src() -> None:
    """写侧死透：src 里除枚举定义/EVENT_LAYER 外无 OBSERVER_SUMMARY 引用。"""
    import pathlib
    root = pathlib.Path("src/ctx_weft")
    offenders = []
    for p in root.rglob("*.py"):
        if p.name == "memory.py" and "protocols" in str(p):
            continue  # 枚举定义 + EVENT_LAYER 兜底映射合法保留
        if "OBSERVER_SUMMARY" in p.read_text(encoding="utf-8"):
            offenders.append(str(p))
    assert offenders == []
```

- [x] **Step 2: 跑测试确认失败**：`pytest tests/unit/test_observer_summary_dead.py -q`，预期 FAIL（4 个文件出现在 offenders）。
- [x] **Step 3: 实施删除**——suspend.py 删除 `await ctx.memory.ingest(MemoryEvent(type=MemoryEventType.OBSERVER_SUMMARY, ...))` 整块（summary 变量仍用于 TASK_SUSPENDED 事件 payload，保留）；runtime.py 删 `memory.ingest(...OBSERVER_SUMMARY...)` 的 try/except 块（content 拼接一并删，`fetched_tracking_ids.add` 保留）；act.py / prepare.py 清单里删该成员。
- [x] **Step 4: 跑新测试 + 全量回归**：`pytest tests/unit/test_observer_summary_dead.py tests/unit -q`。若有测试断言 suspend 写 OBSERVER_SUMMARY，改为断言不写。
- [x] **Step 5: Commit** `refactor(memory): P1 预清场——杀 OBSERVER_SUMMARY 两个写点`

---

### Task 2（P2a）: 归一化模块 memory_compat——新词汇 + 三元组映射 + 视图归一化

**Files:**
- Create: `src/ctx_weft/protocols/memory_compat.py`
- Modify: `src/ctx_weft/protocols/memory.py`（加 MemoryKind、`MemoryAddress = MemoryScope` 正名别名——注意方向：新名指向旧类，旧类改名 P4 才做）
- Modify: `src/ctx_weft/protocols/__init__.py`（导出 MemoryKind / MemoryAddress / memory_compat 公开函数）
- Test: `tests/unit_protocols/test_memory_compat.py`（新建）

**Interfaces:**
- Produces（后续所有 task 依赖的精确签名）:
  - `class MemoryKind(StrEnum)`: `CONVERSATION_TURN = "conversation_turn"`, `SUMMARY = "summary"`, `TOOL_AUDIT = "tool_audit"`, `PUBLICATION = "publication"`
  - `MemoryAddress`：`MemoryScope` 数据类的别名（P4c 完成实体互换）
  - `LEGACY_TRIPLE: dict[MemoryEventType, tuple[MemoryKind, MemoryLayer, str | None]]`——旧 type → (kind, layer, role 约束)；role None = 无约束
  - `kind_of(type_: MemoryEventType | None, kind: MemoryKind | None) -> MemoryKind`
  - `layer_of(type_: MemoryEventType | None, layer: MemoryLayer | None) -> MemoryLayer`
  - `matches_legacy_type(record_type, record_kind, record_layer, record_role, wanted: MemoryEventType) -> bool`
  - `normalize_view(records: list[MemoryRecord]) -> list[MemoryRecord]`（吸收 legacy_dispatch 配对；重打 kind/layer）

- [x] **Step 1: 写失败测试**：

```python
"""memory_compat：v2 词汇 + 旧词汇归一化（v2 设计 §2/§6）。"""
from __future__ import annotations
from ctx_weft.protocols import MemoryEventType, MemoryLayer
from ctx_weft.protocols.memory_compat import (
    LEGACY_TRIPLE, MemoryKind, kind_of, layer_of, matches_legacy_type,
)

def test_legacy_triple_covers_all_live_types() -> None:
    # OBSERVER_SUMMARY 刻意不映射（不进视图）；其余 11 类型全覆盖
    unmapped = {MemoryEventType.OBSERVER_SUMMARY}
    assert set(LEGACY_TRIPLE) == set(MemoryEventType) - unmapped

def test_kind_of_prefers_explicit_kind() -> None:
    assert kind_of(MemoryEventType.USER_PROMPT, None) is MemoryKind.CONVERSATION_TURN
    assert kind_of(None, MemoryKind.SUMMARY) is MemoryKind.SUMMARY
    assert kind_of(MemoryEventType.USER_PROMPT, MemoryKind.SUMMARY) is MemoryKind.SUMMARY

def test_layer_of_falls_back_to_event_layer() -> None:
    assert layer_of(MemoryEventType.AGENT_COMPACT_SUMMARY, None) is MemoryLayer.AGENT
    assert layer_of(None, MemoryLayer.TASK) is MemoryLayer.TASK

def test_matches_legacy_type_bridges_vocabularies() -> None:
    # v2 行（type=None, kind+layer+role）匹配旧类型请求
    assert matches_legacy_type(None, MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "user",
                               MemoryEventType.USER_PROMPT)
    assert not matches_legacy_type(None, MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "assistant",
                                   MemoryEventType.USER_PROMPT)
    # 旧行按 type 精确匹配（不跨型误配）
    assert matches_legacy_type(MemoryEventType.LLM_RESPONSE, None, None, "assistant",
                               MemoryEventType.LLM_RESPONSE)
    assert not matches_legacy_type(MemoryEventType.LLM_RESPONSE, None, None, "assistant",
                                   MemoryEventType.USER_PROMPT)
```

- [x] **Step 2: 确认失败**（模块不存在 → ImportError）。
- [x] **Step 3: 实现** `memory_compat.py`：

```python
"""v2 词汇 + 旧词汇读侧归一化（v2 设计 §2/§6）。全仓唯一认识旧 type 词汇的地方。"""
from __future__ import annotations
from enum import StrEnum
from typing import TYPE_CHECKING
from ctx_weft.protocols.memory import EVENT_LAYER, MemoryEventType, MemoryLayer
if TYPE_CHECKING:
    from ctx_weft.protocols.memory import MemoryRecord

class MemoryKind(StrEnum):
    CONVERSATION_TURN = "conversation_turn"
    SUMMARY = "summary"
    TOOL_AUDIT = "tool_audit"
    PUBLICATION = "publication"

# 旧 type → (kind, layer, role 约束)。role=None 表示该词汇不含 role 约束。
LEGACY_TRIPLE: dict[MemoryEventType, tuple[MemoryKind, MemoryLayer, str | None]] = {
    MemoryEventType.USER_PROMPT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "user"),
    MemoryEventType.LLM_RESPONSE: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "assistant"),
    MemoryEventType.TOOL_RESULT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "tool"),
    MemoryEventType.TOOL_INVOCATION: (MemoryKind.TOOL_AUDIT, MemoryLayer.TASK, None),
    MemoryEventType.TASK_COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.TASK, None),
    MemoryEventType.AGENT_COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.AGENT, None),
    MemoryEventType.AGENT_CONVERSATION_TURN: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, None),
    MemoryEventType.BLACKBOARD_PUBLISH: (MemoryKind.PUBLICATION, MemoryLayer.SESSION, None),
    MemoryEventType.TASK_DISPATCH: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, "assistant"),
    MemoryEventType.TASK_DISPATCH_RESULT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, "tool"),
    MemoryEventType.COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.AGENT, None),
}

def kind_of(type_: MemoryEventType | None, kind: "MemoryKind | None") -> "MemoryKind":
    if kind is not None:
        return kind
    if type_ is None:
        raise ValueError("memory event carries neither type nor kind")
    triple = LEGACY_TRIPLE.get(type_)
    if triple is None:
        raise ValueError(f"legacy type {type_} has no v2 mapping (dead type)")
    return triple[0]

def layer_of(type_: MemoryEventType | None, layer: MemoryLayer | None) -> MemoryLayer:
    if layer is not None:
        return layer
    if type_ is None:
        raise ValueError("memory event carries neither type nor layer")
    return EVENT_LAYER[type_]

def matches_legacy_type(record_type, record_kind, record_layer, record_role, wanted) -> bool:
    """过渡期桥接：一条记录（新旧词汇皆可）是否命中一个旧 type 请求。"""
    if record_type is not None:
        return record_type == wanted
    triple = LEGACY_TRIPLE.get(wanted)
    if triple is None:
        return False
    k, lyr, role = triple
    if record_kind != k or record_layer != lyr:
        return False
    return role is None or record_role == role
```

`normalize_view` 在本 task 先做签名 + 直通（`return records`），配对逻辑 Task 4 落 load_view 时从 `legacy_dispatch.normalize_legacy_dispatch` 委托复用（不复制代码：`from ctx_weft.core.loop.steps.legacy_dispatch import normalize_legacy_dispatch` 会造成 protocols→core 反向依赖，**改为把 legacy_dispatch.py 整体移动到 `ctx_weft/protocols/_legacy_dispatch.py`**，原位置留 `from ctx_weft.protocols._legacy_dispatch import *` 薄转发；两处旧调用点不动）。
- [x] **Step 4: 跑测试通过 + 全量单元回归。**
- [x] **Step 5: Commit** `feat(memory): P2a 归一化模块 memory_compat——v2 词汇 + 三元组映射`

---

### Task 3（P2b）: MemoryEvent / MemoryRecord 过渡字段

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（MemoryEvent 加 kind/layer；MemoryRecord 加 kind/layer/address）
- Test: `tests/unit_protocols/test_memory_event_transitional.py`（新建）

**Interfaces:**
- Produces:
  - `MemoryEvent`：全字段带默认值（既有调用点全部关键字构造——迁移前先 `grep -n "MemoryEvent("` 确认无位置参数构造）；新字段 `kind: MemoryKind | None = None`、`layer: MemoryLayer | None = None`；`__post_init__` 校验：type 与 kind 至少其一、scope/content/timestamp 必给（缺 → ValueError）。
  - `MemoryRecord`：新字段 `kind: MemoryKind | None = None`、`layer: MemoryLayer | None = None`、`address: MemoryAddress | None = None`（来源回显）。`type` 过渡期继续填（legacy 行原样、v2 行为 None）。

- [x] **Step 1: 写失败测试**：

```python
from datetime import datetime, timezone
import pytest
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryEventType, MemoryLayer
from ctx_weft.protocols.memory_compat import MemoryKind

_ADDR = MemoryAddress(session_id="s1", task_id="t1", agent_id="a1")
_TS = datetime.now(timezone.utc)

def test_v2_native_event_needs_no_type() -> None:
    ev = MemoryEvent(kind=MemoryKind.CONVERSATION_TURN, layer=MemoryLayer.TASK,
                     scope=_ADDR, content="hi", timestamp=_TS, role="user")
    assert ev.type is None and ev.kind is MemoryKind.CONVERSATION_TURN

def test_event_requires_type_or_kind() -> None:
    with pytest.raises(ValueError):
        MemoryEvent(scope=_ADDR, content="x", timestamp=_TS)

def test_legacy_event_unchanged() -> None:
    ev = MemoryEvent(type=MemoryEventType.USER_PROMPT, scope=_ADDR, content="x", timestamp=_TS)
    assert ev.kind is None  # 不主动补全：归一化在读侧/provider 侧做，写入原样保存
```

- [x] **Step 2: 确认失败。**
- [x] **Step 3: 实现**——MemoryEvent 字段全给默认值（`type: MemoryEventType | None = None`、`scope: MemoryScope | None = None`、`content: str | list[ContentPart] = ""`、`timestamp: datetime | None = None`，其余现状），`__post_init__` 三条校验各自 `raise ValueError`。**不做 kind 自动补全**（写入保真，归一化统一在读侧）。
- [x] **Step 4: 跑测试 + 全量回归**（重点：所有既有 MemoryEvent 构造仍合法）。
- [x] **Step 5: Commit** `feat(memory): P2b MemoryEvent/MemoryRecord 过渡字段——kind/layer/address`

---

### Task 4（P2c）: load_view 落协议与 in-memory provider；recall_recent 变兼容 wrapper

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（协议加 abstract `load_view`；`normalize_view` 由 memory_compat 提供）
- Modify: `src/ctx_weft/protocols/memory_compat.py`（`normalize_view` 实装：委托 `_legacy_dispatch` 配对 + 重打 kind/layer/address）
- Modify: `src/ctx_weft/providers/memory_blackboard/in_memory.py`（native load_view；recall_recent / recall_recent_by_agent / count_recent 改为委托 load_view 的三元组兼容 wrapper）
- Test: `tests/unit_protocols/test_load_view.py`（新建）

**Interfaces:**
- Produces（协议方法，后续迁移的目标 API）:

```python
async def load_view(
    self, address: MemoryAddress, scope: MemoryLayer, ctx: ProviderContext,
    kinds: list[MemoryKind] | None = None,
) -> list[MemoryRecord]
```

  - 全量幸存、**(timestamp, seq_no) 升序**；kinds=None → [CONVERSATION_TURN, SUMMARY]。
  - 半址校验（矩阵）：TASK → task_id 给定=单 task 视图 / 仅 agent_id=跨 task 聚合 / 二者皆 None=ValueError；AGENT → agent_id 必给、task_id 非 None=ValueError；SESSION → task_id/agent_id 非 None=ValueError。
  - legacy 行可见性：按 KIND 展开匹配（如 CONVERSATION_TURN@TASK 匹配 type∈{user_prompt, llm_response, tool_result} 或 v2 行 kind==conversation_turn&layer==task）；OBSERVER_SUMMARY 永不出现。
  - 返回前过 `normalize_view`（dispatch 配对 + kind/layer/address 重打）。

- [x] **Step 1: 写失败测试**（核心用例，全部真 provider 无 mock）：

```python
async def test_load_view_ascending_and_default_kinds(): ...
    # ingest USER_PROMPT + LLM_RESPONSE + TOOL_INVOCATION（audit）→
    # load_view(TASK, task_id 给定) 默认 kinds 返回前两条、升序、TOOL_AUDIT 不在
async def test_load_view_cross_task_by_agent(): ...
    # 两个 task 各写一条，load_view(address(agent_id only), TASK) 聚合返回、address 回显 task_id
async def test_load_view_half_address_validation(): ...
    # TASK 全 None → ValueError；AGENT 带 task_id → ValueError；SESSION 带 agent_id → ValueError
async def test_load_view_sees_both_vocabularies(): ...
    # 旧 type 行 + v2 kind 行同分区 → 一个视图、kind 全部重打
async def test_recall_recent_wrapper_matches_v2_rows(): ...
    # v2 行（kind=CONVERSATION_TURN, role=user）能被 recall_recent([USER_PROMPT]) 读到（newest-first 不变）
```

- [x] **Step 2: 确认失败**（load_view 不存在 → AttributeError/TypeError）。
- [x] **Step 3: 实现**：
  - in_memory：`_StoredEvent` 存 normalized `(kind, layer)`（ingest 时经 kind_of/layer_of 计算，OBSERVER_SUMMARY 等死类型 kind 存 None → 永不见于视图）；`load_view` = 半址校验 → 分区过滤（layer + address 各字段）→ kinds 匹配 → (timestamp, seq_no) 升序 → `normalize_view`。
  - `recall_recent` wrapper：`matches_legacy_type` 逐条匹配（不再直接比 type）+ 现状 newest-first + limit 保持；`recall_recent_by_agent` / `count_recent` 同理委托。**行为契约不变，48 个测试文件必须全绿。**
  - 协议：`load_view` abstract；docstring 抄设计 §4 语义。
- [x] **Step 4: 跑新测试 + 全量回归。**
- [x] **Step 5: Commit** `feat(memory): P2c load_view 落地——半址校验 + 双词汇视图 + 兼容 wrapper`

---

### Task 5（P2d）: fold 落协议与 in-memory provider

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（协议加 abstract `fold`）
- Modify: `src/ctx_weft/providers/memory_blackboard/in_memory.py`（native fold：单锁内 supersede + ingest 原子完成）
- Test: `tests/unit_protocols/test_fold.py`（新建）

**Interfaces:**
- Produces:

```python
async def fold(
    self, supersede_ids: list[str], replacements: list[MemoryEvent], ctx: ProviderContext,
) -> list[str]
```

  - 原子"遗忘+补偿"；已 superseded / 不存在的 id 跳过（幂等）；replacements 可空（纯遗忘）可多条；replacement 带 `event.id` 时按 record-id 契约采用且幂等。

- [x] **Step 1: 写失败测试**：纯遗忘（fold(ids, []) 后视图为空）；遗忘+单摘要（摘要可见、raw 不可见、返回新 id）；多 replacement 保序；不存在 id 跳过；replacement 带预生成 id 幂等重放（fold 两次 → 单条）。
- [x] **Step 2: 确认失败。**
- [x] **Step 3: 实现**——in_memory 在 `self._lock` 内先标 superseded 再逐条走 ingest 内核（复用 id 契约逻辑，seq/topic 计数一致）；协议 docstring 抄设计 §4。
- [x] **Step 4: 跑测试 + 回归。**
- [x] **Step 5: Commit** `feat(memory): P2d fold 原子原语落地`

---

### Task 6（P3a）: 读侧调用点迁移——recall_recent/by_agent/count_recent → load_view

**Files（全部 Modify；行号为迁移前基线）:**
- `src/ctx_weft/core/assembler/sources/agent_recall.py:73,90`（by_agent → 半址 load_view；随后其显式 normalize_legacy_dispatch 调用已冗余，删）
- `src/ctx_weft/core/loop/steps/reconcile.py:58,66`
- `src/ctx_weft/core/loop/steps/prepare.py:245`
- `src/ctx_weft/core/loop/steps/act.py:302`（count_recent → len(load_view)）
- `src/ctx_weft/core/loop/steps/background_observe.py:77,124,189`
- `src/ctx_weft/core/loop/steps/compact.py:123,216,218,233,277,287,394,395,418,533`
- `src/ctx_weft/core/loop/steps/finalize.py:82,163,267,272,317`
- `src/ctx_weft/core/runtime.py:129,140,1222,1498`
- Test: 既有全量套件为回归网（本 task 不新增测试文件；行为等价迁移）

**Interfaces:**
- Consumes: Task 4 的 `load_view`。
- 迁移映射表（每个调用点按此机械转换，语义存疑时读上下文逐个对照）：

| 旧调用形状 | 新调用形状 |
|---|---|
| `recall_recent(scope, [USER_PROMPT, LLM_RESPONSE, ...task 层混合], N, ctx)` | `load_view(addr(task_id=T, agent_id=A), TASK, ctx)` + Python 侧按 role/kind 过滤 |
| `recall_recent(scope, [AGENT_CONVERSATION_TURN], N, ctx)` | `load_view(addr(agent_id=A), AGENT, ctx, kinds=[CONVERSATION_TURN])` |
| `recall_recent(scope, [AGENT_COMPACT_SUMMARY], ...)` | `load_view(addr(agent_id=A), AGENT, ctx, kinds=[SUMMARY])` |
| `recall_recent_by_agent(scope, types, N, ctx)` | `load_view(MemoryAddress(session_id=S, agent_id=A), TASK, ctx)`（**显式 task_id=None**）|
| `count_recent(scope, types, ctx)` | `len(await load_view(...))`（同上过滤后取 len） |
| 返回值 newest-first + 调用方 `reversed()` | load_view 已升序 → **删调用方 reversed()**；取"最近一条"用 `[-1]` |
| limit=1 的两个冷路径 | 取 `view[-1:]` |

- 类型 → kind/role 过滤对照：USER_PROMPT→role=="user"；LLM_RESPONSE→role=="assistant"；TOOL_RESULT→role=="tool"（kind 均 CONVERSATION_TURN）；TASK/AGENT_COMPACT_SUMMARY→kind==SUMMARY（layer 由查询决定）；TOOL_INVOCATION→kinds=[TOOL_AUDIT] 显式传。
- 陷阱清单（执行时逐条核对）：① `reversed()` 双重反转——每处迁移必查调用方是否自带 reversed；② by_agent 三处必须半址否则 ValueError（loud，符合预期）；③ compact.py 的 `_TASK_LAYER_TYPES`/`_TASK_BODY_TYPES` 常量改为 kind+role 谓词函数；④ metadata["task_id"] 消费点改用 `record.address.task_id`（保留 metadata 打标不删，读侧优先 address）。

- [x] **Step 1**: 按文件顺序迁移（agent_recall → reconcile → prepare → act → background_observe → finalize → runtime → compact 最后，因其最重），**每迁移完一个文件跑一次相关测试**（如 `pytest tests/unit/test_recall_by_agent.py tests/unit/test_compaction.py -q`）。
- [x] **Step 2**: 全量回归：`pytest tests -q`，除环境基线外全绿；golden 族必须逐个确认绿。
- [x] **Step 3: Commit** `refactor(memory): P3a 读侧 25 调用点迁移 load_view——升序契约、半址显式化`

---

### Task 7（P3b）: 写侧词汇迁移——ingest 调用点 type → kind+layer+role

**Files（全部 Modify）:**
- `src/ctx_weft/core/loop/driver.py:167-186`（USER_PROMPT → kind=CONVERSATION_TURN, layer=TASK, role="user"）
- `src/ctx_weft/core/loop/steps/act.py`（3 处：LLM_RESPONSE / TOOL_RESULT 合成 / 打断半截）
- `src/ctx_weft/core/loop/capability_gateway.py`（5 处：TOOL_INVOCATION→TOOL_AUDIT、TOOL_RESULT、AGENT_CONVERSATION_TURN→kind CONVERSATION_TURN@AGENT）
- `src/ctx_weft/core/loop/steps/background_observe.py`（2 处）、`compact.py`（2 处徒手摘要 ingest——Task 9 会再改成 fold，本 task 先换词汇）、`finalize.py`（5 处）、`suspend.py`（1 处 user_prompt 持久化）、`runtime.py`（3 处）
- Test: 既有套件回归（Task 4 的 wrapper 桥接保证旧读法兼容）

**Interfaces:**
- Consumes: Task 3 的过渡 MemoryEvent（kind/layer 构造合法）。
- 词汇映射: `type=USER_PROMPT` → `kind=CONVERSATION_TURN, layer=TASK, role="user"`；LLM_RESPONSE→同 kind role="assistant"；TOOL_RESULT→role="tool"；TOOL_INVOCATION→`kind=TOOL_AUDIT, layer=TASK`；AGENT_CONVERSATION_TURN→`kind=CONVERSATION_TURN, layer=AGENT`（role 沿现状参数）；TASK_COMPACT_SUMMARY→`kind=SUMMARY, layer=TASK`；AGENT_COMPACT_SUMMARY→`kind=SUMMARY, layer=AGENT`；BLACKBOARD_PUBLISH→`kind=PUBLICATION, layer=SESSION`。每处删 `type=` 加 `kind=/layer=`（role 已有的保持）。

- [x] **Step 1**: 逐文件替换；每文件跑相关测试。
- [x] **Step 2**: 全量回归 + golden 族确认（读侧已走 load_view/三元组匹配，新词汇行可见）。
- [x] **Step 3: Commit** `refactor(memory): P3b 写侧 22 ingest 调用点切 v2 词汇`

---

### Task 8（P3c）: compact 策展上移——apply_compact 两调用点改框架侧 segment_fold

**Files:**
- Create: `src/ctx_weft/core/loop/steps/segment_fold.py`（策展政策 + fold 编排；从 in_memory.apply_compact:205-305 移植语义）
- Modify: `src/ctx_weft/core/loop/steps/observe.py:472-491`（_fold_retry_segment 改调 segment_fold）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:277-291`（同；含 TypeError 降级分支简化——segment_fold 是框架内函数，签名错配不再是运行时协议风险，删该 try/except 的 TypeError 特判、保留通用异常降级）
- Test: `tests/unit/test_segment_fold.py`（新建）+ 既有 `test_observe_retry_fold.py` / `test_compact_trailing_anchor.py` / `test_compact_protects_user.py` 回归

**Interfaces:**
- Produces:

```python
@dataclass
class SegmentFoldResult:
    events_before: int
    events_after: int
    summary_event_id: str

async def segment_fold(
    memory: MemoryProvider, address: MemoryAddress, layer: MemoryLayer,
    summary: str, ctx: ProviderContext,
) -> SegmentFoldResult
```

- 移植语义（两个调用点参数完全一致：keep_last=0、protect={role=user 回合, SUMMARY kind}、since_last=段边界=最后一条 role=user 的 CONVERSATION_TURN）：
  1. `view = load_view(address, layer, ctx, kinds=[CONVERSATION_TURN, SUMMARY, TOOL_AUDIT])`（升序即渲染序）；
  2. 段边界：最后一条 role=="user" 且 kind==CONVERSATION_TURN 之后为折叠池；无则整分区；
  3. protect 过滤：role=="user" 或 kind==SUMMARY 不折；keep_last=0 → 池内全折；
  4. 锚点：被折区起点之后第一条幸存记录 → 摘要 `timestamp = anchor.timestamp - 1µs`；段尾无幸存 → `timestamp = 被折末条.timestamp`（**不用 now()**，防迟到摘要越过新 USER_PROMPT——语义注释原样移植）；空池 → `timestamp = now_utc()`；
  5. 摘要 role：TASK 层 "assistant" / AGENT 层 "user"（Anthropic 首条 assistant 400 的注释一并移植）；
  6. `fold([被折 ids], [summary_event])`，metadata={"keep_last": 0, "archived_count": n}。
- events_before/after = 折叠前后 `len(view 幸存)`（供 MEMORY_COMPACTED 事件 payload，observe.py 现有 payload 字段不变）。

- [x] **Step 1: 写失败测试**（用例从 in_memory.apply_compact 的语义注释提取）：段边界折叠不跨段；protect 保 user+SUMMARY；锚点 ts-1µs 在幸存者之前；段尾锚不用 now；多段摘要累积不互吞。
- [x] **Step 2: 确认失败 → Step 3: 实现 → Step 4: 新测试 + golden 族 + 全量回归。**
- [x] **Step 5: Commit** `refactor(memory): P3c 策展上移——segment_fold 取代 apply_compact 调用`

---

### Task 9（P3d）: 徒手 supersede+ingest → fold（6 处）

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:142`（finish 对替换：supersede 2 + ingest 2 → `fold(ids, [asst_ev, tool_ev])`）
- Modify: `src/ctx_weft/core/loop/steps/compact.py:144,352,412`（fold_root_experience / collapse / demote 族：改 fold；纯遗忘处 `fold(ids, [])`）
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:170,325`（末 raw 段折 → `fold(ids, [])`；replace 处配对写入并入 fold）
- Test: 既有 golden 族 + `test_deferred_close_raw_fold.py` / `test_cross_layer_fold.py` 回归；`tests/unit_protocols/test_fold.py` 已覆盖原语

**Interfaces:**
- Consumes: Task 5 的 `fold`。
- 转换规则：`await memory.supersede(ids, ctx)` 单独出现 → `await memory.fold(ids, [], ctx)`；`supersede(ids)` 后跟随语义配对的 `ingest(replacement)` → 合并为一次 `fold(ids, [replacement])`（消崩溃窗口——这是本 task 的全部意义，逐处确认配对关系再合并，不确定配对的保持纯遗忘 + 独立 ingest 并注释原因）。

- [x] **Step 1**: 逐处迁移 + 每文件回归。
- [x] **Step 2**: 全量回归。
- [x] **Step 3: Commit** `refactor(memory): P3d 徒手 supersede+ingest 改 fold——关原子窗口`

---

### Task 10（P4a）: 测试迁移——48 文件直调旧方法改新 API

> **执行时范围决策（2026-07-27）**：apply_compact 的 12 处测试直调**全部迁移**（其载体
> 在 Task 11 物理删除；keep_last≥1 死形态测试删除，段语义测试改 segment_fold/显式 fold）。
> recall_recent / by_agent / count_recent / supersede 的 ~220 处测试直调**不批量重写**——
> in-memory provider 将这四个薄 wrapper 保留为**标注明确的非协议测试兼容方法**（Task 11
> 从 Protocol 删除后 provider 实例方法仍在），存量测试增量迁移、新测试一律 load_view/fold。
> 理由：协议面 8 方法的目标不受影响；220 处"写入后读回断言"的机械重写收益低于回归风险。

**Files:**
- Modify: `grep -rl "\.recall_recent(\|\.recall_recent_by_agent(\|\.count_recent(\|\.apply_compact(\|\.supersede(" tests` 所列 48 文件
- 迁移映射同 Task 6 表 + Task 9 规则；测试里 `apply_compact` 直调改 `segment_fold`（或该测试本意测 provider 原语的，改测 `fold`）。

- [x] **Step 1**: 分批迁移（integration 6 文件一批、unit 按主题分 4-5 批），每批跑批内测试。
- [x] **Step 2**: 全量回归。
- [x] **Step 3: Commit** `test(memory): P4a 测试迁移新 API——48 文件`

---

### Task 11（P4b/c）: 薄包装日落 + 终名切换（三个独立 commit）

**Commit A——机械改名（名字真空前置）：**
- 全仓 `MemoryScope(` 构造/注解 → `MemoryAddress(`；`from ... import MemoryScope` → `MemoryAddress`（src + tests，sed + 手修）；`MemoryEvent`/`MemoryRecord` 的 `scope=` 关键字 → 字段改名 `address=`（dataclass 字段改名 + 全仓关键字替换；`__post_init__` 对传入 `scope=` 的残留调用 raise TypeError 天然 loud）。
- 删 `MemoryScope = MemoryAddress` 别名 → **此刻起名字 `MemoryScope` 不存在**，漏网引用 = loud NameError。
- 跑全量回归 → commit `refactor(memory): P4b-1 MemoryAddress 正名 + scope 字段改名 address`

**Commit B——旧方法删除：**
- 协议与 in_memory 删：`recall_recent` / `recall_recent_by_agent` / `count_recent` / `supersede` / `apply_compact` / `CompactResult`；`protocols/__init__.py` 导出同步；`layer_for_types` 若无调用点一并删。
- 方法面清点断言测试：`tests/unit_protocols/test_protocol_surface.py`——8 方法各 `hasattr`、旧 5 方法 `not hasattr`。
- 全量回归 → commit `refactor(memory): P4b-2 旧方法日落——方法面 11→8`

**Commit C——终名 MemoryLayer → MemoryScope：**
- `class MemoryLayer` 改名 `class MemoryScope`（StrEnum），全仓引用替换；`MemoryLayer = MemoryScope` 兼容别名**保留**（host postgres provider 仍 import；别名日落待 host 迁移后独立 PR，在别名旁注明）。
- `MemoryEvent.layer` / `MemoryRecord.layer` 字段改名 `scope`（全仓关键字替换；load_view 参数名 `scope` 已就位）。
- EVENT_LAYER 保留（归一化兜底表，改注释标 legacy）。
- 全量回归 + v2 设计文档状态行改"已实施（2026-07-27，commit 区间）" → commit `refactor(memory): P4c 终名切换——MemoryScope 落定`

---

## Self-Review 记录

1. **Spec 覆盖**：§2 词汇（Task 2）；§3 数据结构（Task 3 + 11A/C 字段终名）；§4 写面 ingest 不变量→Task 4 半址校验含 ingest 侧？——**补**：ingest 的 per-scope 全址校验并入 Task 3 `__post_init__`（TASK 层必携 task_id+agent_id 等，按设计 §4 三行规则）；§4 fold（Task 5/9）；§4 load_view（Task 4/6）；§5 metadata 注册表（无代码，文档已有）；§6 归一化模块 + 别名展开（Task 2/4）、legacy_dispatch 吸收（Task 2 移动 + Task 4 委托）；§8 四阶段全对应；record-id 增补（前置 commit 46feec5 + Task 5 replacement 幂等用例）。
2. **占位符扫描**：Task 6/7/10 为迁移表驱动（调用点逐处代码在执行时按表生成），表已含完整映射与陷阱清单——迁移类 task 以此为完备标准，无 TBD。
3. **类型一致性**：`load_view(address, scope: MemoryLayer, ctx, kinds)` param 名 `scope` 与 P4c 终名衔接；`segment_fold` 消费 Task 4/5 签名；`MemoryAddress` 自 Task 2 起可用——一致。
