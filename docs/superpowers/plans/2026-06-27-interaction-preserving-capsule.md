# 交互保留型 root 自经验胶囊 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** root 自经验胶囊从「只留首条 user + 一条摘要 + delegate 黑盒」改为「交错时间线：所有用户消息逐字保留为锚点、其间 LLM 处理段被摘要、最终段由 finish_task 对承载」，并新增 root 后台异步 observe 产段摘要、同 agent 子任务平铺嵌套 / 跨 agent 黑盒。

**Architecture:** 三层叠加——(1) provider 的 `apply_compact(TASK)` 加 `protect_types` 使 USER_PROMPT 永不被折；(2) root 新增 fire-and-forget 后台 observe（仿 `recognize_intent`），在交互/finish 段边界产段摘要并折 raw；(3) close 时 `_synthesize_dispatch_pair` 重写为快照幸存对话→`AGENT_CONVERSATION_TURN`（保留原始 timestamp）+ 合成 finish 对，子任务按同/跨 agent 分流承载。

**Tech Stack:** Python 3.12（`ctx_weft` core + `ipmastercowork` host postgres provider）、asyncio、pytest（`uv run pytest`）、SQLAlchemy async。

## Global Constraints

- 所有 core 改动落 `src/ctx_weft/` 下；postgres provider 改动落 `src/ipmastercowork/providers/memory/postgres.py`。
- 测试用 `uv run pytest`（pyproject 已配 `pythonpath=["."]`）。core 单测在 `tests/unit/`；host 单测在 `tests/unit/`。
- 不破坏相邻 spec `2026-06-26-root-experience-summary-fold-design.md` 的不变量：首条消息恒 user（§2.3）；compaction summary 渲染期包装不落库（§2.4）；gateway `ensure_leading_user`（§2.5）。
- 黑盒不变量（docs/spec/06 §4.4）放宽：**只对跨 agent 成立**；同 agent 透明嵌套。
- 设计真相源：`docs/superpowers/specs/2026-06-26-interaction-preserving-capsule-design.md`（下称 spec）。
- LLM 失败一律走自愈 + 降级（spec §3.6），绝不让胶囊合成崩溃或丢用户锚点。
- 提交信息以 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` 结尾。分支已在 `feat/interaction-preserving-capsule`，最终合回 `sync/upstream-agent-memory-compaction`（非 master）。

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/ctx_weft/providers/memory_blackboard/in_memory.py` | in-memory provider 的 `apply_compact` | 改：加 `protect_types` + 摘要落位修正 |
| `src/ipmastercowork/providers/memory/postgres.py` | postgres provider 的 `apply_compact` | 改：同上 |
| `src/ctx_weft/protocols/memory.py` | `MemoryProvider.apply_compact` 抽象签名 | 改：加 `protect_types` 形参 |
| `src/ctx_weft/core/loop/steps/compact.py` | `summarize_for_compact`、`_compact_scope`、`fold_root_experience` | 改：调用传 `protect_types`；fold anchor 扩到全元素 |
| `src/ctx_weft/core/loop/steps/observe.py` | `_maybe_compact_task`、`_should_use_llm` | 改：max_turns 压缩传 protect_types；root 接后台触发 |
| `src/ctx_weft/core/loop/steps/background_observe.py` | root 后台异步 observe（新） | 建：fire-and-forget + 并发串行 |
| `src/ctx_weft/core/loop/steps/finalize.py` | `_synthesize_dispatch_pair`、`_close_one`、`_gc_subtree`、bubble | 改：交错胶囊 + finish 对 + 子任务分流 |
| `src/ctx_weft/core/assembler/sources/agent_recall.py` | 胶囊渲染 | 验证/微调 finish 对 + 嵌套子胶囊渲染 |
| `tests/unit/test_*` + `tests/unit/test_*` | 测试 | 建：A–H 组 |

---

## Phase 1 · provider：user-aware `apply_compact(TASK)`

### Task 1: in-memory provider 的 `apply_compact` 加 `protect_types` + 摘要落位

**Files:**
- Modify: `src/ctx_weft/providers/memory_blackboard/in_memory.py:205-274`（`apply_compact`）
- Modify: `src/ctx_weft/protocols/memory.py`（`MemoryProvider.apply_compact` 抽象签名加形参）
- Test: `tests/unit/test_compact_user_aware.py`（新）

**Interfaces:**
- Produces: `apply_compact(scope, summary, keep_last, ctx, layer=MemoryLayer.AGENT, protect_types: tuple[MemoryEventType, ...] = ()) -> CompactResult`。语义：`protect_types` 内的事件**永不 supersede、不计入 keep_last**；summary 落在「被折区块之后、其后第一条幸存事件之前」（保证 `[UP1][summary][UP2][kept]` 序）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_compact_user_aware.py`：
```python
import pytest
from datetime import datetime, timedelta, UTC
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryEvent, MemoryEventType, MemoryLayer, MemoryScope,
)
from ctx_weft.protocols.provider import ProviderContext


def _ctx():
    return ProviderContext(session_id="s1", tenant_id="t1", task_id="task1", agent_id="a1")


def _scope():
    return MemoryScope(session_id="s1", task_id="task1", agent_id="a1")


async def _ingest(p, typ, content, ts, role):
    return await p.ingest(
        MemoryEvent(type=typ, scope=_scope(), content=content, timestamp=ts, role=role),
        _ctx(),
    )


@pytest.mark.asyncio
async def test_apply_compact_task_protects_user_prompt():
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法1", base + timedelta(seconds=1), "assistant")
    await _ingest(p, MemoryEventType.TOOL_RESULT, "结果1", base + timedelta(seconds=2), "tool")
    await _ingest(p, MemoryEventType.USER_PROMPT, "HITL回复", base + timedelta(seconds=3), "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法2", base + timedelta(seconds=4), "assistant")

    await p.apply_compact(
        scope=_scope(), summary="段摘要", keep_last=0, ctx=_ctx(),
        layer=MemoryLayer.TASK, protect_types=(MemoryEventType.USER_PROMPT,),
    )

    recs = await p.recall_recent(
        _scope(),
        [MemoryEventType.USER_PROMPT, MemoryEventType.LLM_RESPONSE,
         MemoryEventType.TOOL_RESULT, MemoryEventType.TASK_COMPACT_SUMMARY],
        100, _ctx(),
    )
    recs = list(reversed(recs))  # newest-first → 时间序
    kinds = [(r.type, r.content) for r in recs]
    # 两条 USER_PROMPT 全留、LLM/TOOL 被折成摘要、摘要落在原始诉求之后 HITL 之前
    assert (MemoryEventType.USER_PROMPT, "原始诉求") in kinds
    assert (MemoryEventType.USER_PROMPT, "HITL回复") in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法1") not in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法2") not in kinds
    summary_idx = next(i for i, (t, _) in enumerate(kinds) if t == MemoryEventType.TASK_COMPACT_SUMMARY)
    up1_idx = kinds.index((MemoryEventType.USER_PROMPT, "原始诉求"))
    up2_idx = kinds.index((MemoryEventType.USER_PROMPT, "HITL回复"))
    assert up1_idx < summary_idx < up2_idx
```

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_user_aware.py -v`
Expected: FAIL（`apply_compact` 无 `protect_types` 形参 → TypeError）

- [ ] **Step 3: 改 protocol 抽象签名**

`src/ctx_weft/protocols/memory.py` 的 `MemoryProvider.apply_compact` 抽象方法签名加形参（与现有同位置）：
```python
    @abstractmethod
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
    ) -> CompactResult:
        ...
```

- [ ] **Step 4: 改 in-memory 实现**

替换 `in_memory.py` 的 `apply_compact`（保留签名其余不变，新增 `protect_types`）：
```python
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
    ) -> CompactResult:
        scope_key = self._scope_key(scope, ctx.tenant_id, layer)

        def _in_scope(s: _StoredEvent) -> bool:
            return (
                not s.is_superseded
                and EVENT_LAYER[s.event.type] is layer
                and self._scope_key(s.event.scope, ctx.tenant_id, layer) == scope_key
            )

        active = [s for s in self._events if _in_scope(s)]
        events_before = len(active)
        active.sort(key=lambda s: s.seq_no)

        # protect_types 永不进 archive；keep_last 只对可折类型计
        archivable = [s for s in active if s.event.type not in protect_types]
        to_archive = archivable[:-keep_last] if keep_last > 0 else archivable
        for s in to_archive:
            s.is_superseded = True

        summary_type = (
            MemoryEventType.TASK_COMPACT_SUMMARY
            if layer is MemoryLayer.TASK
            else MemoryEventType.AGENT_COMPACT_SUMMARY
        )
        # 摘要落在「被折区块之后、其后第一条幸存事件之前」→ [UP1][summary][UP2][kept]
        archived_max_seq = max((s.seq_no for s in to_archive), default=-1)
        following = [s for s in active if s.seq_no > archived_max_seq and not s.is_superseded]
        if following:
            anchor = min(following, key=lambda s: (s.event.timestamp, s.seq_no))
            summary_ts = anchor.event.timestamp - timedelta(microseconds=1)
            summary_seq = anchor.seq_no - 1
        else:
            summary_ts = datetime.now(UTC)
            self._seq_counters[scope_key] = self._seq_counters.get(scope_key, 0) + 1
            summary_seq = self._seq_counters[scope_key]

        compact_event = MemoryEvent(
            type=summary_type,
            scope=scope,
            content=summary,
            timestamp=summary_ts,
            role="user",
            metadata={"keep_last": keep_last, "archived_count": len(to_archive)},
        )
        async with self._lock:
            self._next_id += 1
            compact_id = f"mev_{self._next_id:08d}"
            self._events.append(_StoredEvent(
                id=compact_id, event=compact_event, seq_no=summary_seq, topic_seq_no=0,
            ))

        events_after = sum(1 for s in self._events if _in_scope(s))
        return CompactResult(
            events_before=events_before,
            events_after=events_after,
            summary_event_id=compact_id,
        )
```

- [ ] **Step 5: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_user_aware.py tests/unit/test_compaction.py tests/unit/test_compact_layers.py -v`
Expected: PASS（新测试过；旧 compact 测试不回归——旧调用不传 `protect_types`，默认 `()` 行为等价于「全折」，唯一差异是摘要落位由「kept 前」改为「archived 后第一条幸存前」，对无 protect 场景两者等价）

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/protocols/memory.py src/ctx_weft/providers/memory_blackboard/in_memory.py tests/unit/test_compact_user_aware.py
git commit -m "feat(memory): apply_compact 加 protect_types，USER_PROMPT 永不折（in-memory）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: postgres provider 的 `apply_compact` 同步 `protect_types`

**Files:**
- Modify: `src/ipmastercowork/providers/memory/postgres.py:291-368`（`apply_compact`）
- Test: `tests/unit/test_postgres_compact_user_aware.py`（新）

**Interfaces:**
- Consumes: Task 1 的 protocol 签名。
- Produces: 同 Task 1 行为，postgres backend。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_postgres_compact_user_aware.py`：复用仓内既有 postgres 测试夹具（参照 `tests/unit/test_postgres_recall_by_agent.py` 的 fixture 建表/factory 方式），断言与 Task 1 Step 1 等价：两条 USER_PROMPT 保留、LLM/TOOL 折成一条 TASK_COMPACT_SUMMARY、摘要落在 UP1 与 UP2 之间。
```python
# 复用 test_postgres_recall_by_agent.py 顶部的 provider fixture（async pg provider + 建表）
# 然后：
async def test_pg_apply_compact_protects_user_prompt(pg_provider):
    p = pg_provider
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    ctx = ProviderContext(session_id="s1", tenant_id="t1", task_id="task1", agent_id="a1")
    sc = MemoryScope(session_id="s1", task_id="task1", agent_id="a1")
    async def ing(typ, c, ts, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c, timestamp=ts, role=role), ctx)
    await ing(MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await ing(MemoryEventType.LLM_RESPONSE, "想法1", base + timedelta(seconds=1), "assistant")
    await ing(MemoryEventType.TOOL_RESULT, "结果1", base + timedelta(seconds=2), "tool")
    await ing(MemoryEventType.USER_PROMPT, "HITL回复", base + timedelta(seconds=3), "user")
    await ing(MemoryEventType.LLM_RESPONSE, "想法2", base + timedelta(seconds=4), "assistant")
    await p.apply_compact(scope=sc, summary="段摘要", keep_last=0, ctx=ctx,
                          layer=MemoryLayer.TASK, protect_types=(MemoryEventType.USER_PROMPT,))
    recs = list(reversed(await p.recall_recent(
        sc, [MemoryEventType.USER_PROMPT, MemoryEventType.LLM_RESPONSE,
             MemoryEventType.TOOL_RESULT, MemoryEventType.TASK_COMPACT_SUMMARY], 100, ctx)))
    kinds = [(r.type, r.content) for r in recs]
    assert (MemoryEventType.USER_PROMPT, "原始诉求") in kinds
    assert (MemoryEventType.USER_PROMPT, "HITL回复") in kinds
    assert (MemoryEventType.LLM_RESPONSE, "想法1") not in kinds
    s_idx = next(i for i, (t, _) in enumerate(kinds) if t == MemoryEventType.TASK_COMPACT_SUMMARY)
    assert kinds.index((MemoryEventType.USER_PROMPT, "原始诉求")) < s_idx < kinds.index((MemoryEventType.USER_PROMPT, "HITL回复"))
```

- [ ] **Step 2: 运行验证失败**

Run: `uv run pytest tests/unit/test_postgres_compact_user_aware.py -v`
Expected: FAIL（TypeError：无 `protect_types`）

- [ ] **Step 3: 改 postgres 实现**

替换 `postgres.py` 的 `apply_compact`，加 `protect_types` 并改落位（保持 `_compact_scope_where` / ORM 不变）：
```python
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
    ) -> CompactResult:
        scope_where = _compact_scope_where(scope, layer)
        summary_type = (
            MemoryEventType.TASK_COMPACT_SUMMARY
            if layer is MemoryLayer.TASK
            else MemoryEventType.AGENT_COMPACT_SUMMARY
        )
        protect_strs = [str(t) for t in protect_types]
        async with self._factory() as db:
            async with db.begin():
                count_result = await db.execute(
                    select(func.count()).where(scope_where, MemoryEventModel.is_superseded == False)
                )
                events_before = count_result.scalar_one() or 0

                # 全量按 seq 升序，分出可折（非 protect）与其后第一条幸存
                all_result = await db.execute(
                    select(MemoryEventModel.id, MemoryEventModel.seq_no,
                           MemoryEventModel.timestamp, MemoryEventModel.type)
                    .where(scope_where, MemoryEventModel.is_superseded == False)
                    .order_by(MemoryEventModel.seq_no.asc())
                )
                all_rows = all_result.all()
                archivable = [r for r in all_rows if r.type not in protect_strs]
                to_archive = archivable[:-keep_last] if keep_last > 0 else archivable
                to_archive_ids = [r.id for r in to_archive]
                if to_archive_ids:
                    await db.execute(
                        update(MemoryEventModel)
                        .where(MemoryEventModel.id.in_(to_archive_ids))
                        .values(is_superseded=True)
                    )

                archived_max_seq = max((r.seq_no for r in to_archive), default=-1)
                following = [r for r in all_rows
                             if r.seq_no > archived_max_seq and r.id not in set(to_archive_ids)]
                summary_id = generate_id("mev")
                if following:
                    anchor = min(following, key=lambda r: (r.timestamp, r.seq_no))
                    summary_seq = anchor.seq_no - 1
                    summary_ts = anchor.timestamp - timedelta(microseconds=1)
                else:
                    seq_result = await db.execute(
                        select(func.coalesce(func.max(MemoryEventModel.seq_no), 0)).where(scope_where)
                    )
                    summary_seq = (seq_result.scalar_one() or 0) + 1
                    summary_ts = now_utc()
                db.add(MemoryEventModel(
                    id=summary_id, session_id=scope.session_id, task_id=scope.task_id,
                    agent_id=scope.agent_id, layer=layer.value, type=str(summary_type),
                    role="user", content=summary, seq_no=summary_seq, topic_seq_no=0,
                    metadata_json=json.dumps({"keep_last": keep_last, "archived_count": len(to_archive_ids)}),
                    timestamp=summary_ts,
                ))
                events_after_count = await db.execute(
                    select(func.count()).where(scope_where, MemoryEventModel.is_superseded == False)
                )
                events_after = (events_after_count.scalar_one() or 0) + 1
        return CompactResult(events_before=events_before, events_after=events_after,
                             summary_event_id=summary_id)
```

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `uv run pytest tests/unit/test_postgres_compact_user_aware.py tests/unit/test_postgres_memory_supersede.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ipmastercowork/providers/memory/postgres.py tests/unit/test_postgres_compact_user_aware.py
git commit -m "feat(memory): apply_compact protect_types（postgres）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 2 · 现有压缩触发点传 `protect_types`

### Task 3: `max_turns` / `context_limit` 压缩保护 USER_PROMPT

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py:336-342`（`_maybe_compact_task` 的 `apply_compact` 调用）
- Modify: `src/ctx_weft/core/loop/steps/compact.py:182-185`（`_compact_scope` 的 task 层 `apply_compact` 调用）
- Test: `tests/unit/test_compact_protects_user.py`（新）

**Interfaces:**
- Consumes: Task 1 的 `protect_types`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_compact_protects_user.py`：构造一个 task 层含 [UP1, llm, tool, UP2(HITL), llm] 的 state，手动调 `_maybe_compact_task`（max_turns 退出）与 `_compact_scope`，断言两条 USER_PROMPT 都未 superseded、LLM/TOOL 被折。（参照 `tests/unit/test_observe_task_compaction.py` 的 state/ctx 构造夹具。）

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_protects_user.py -v`
Expected: FAIL（USER_PROMPT 被折，断言不满足）

- [ ] **Step 3: observe.py 传 protect_types**

`observe.py` `_maybe_compact_task` 内 `apply_compact` 调用加参：
```python
        result = await ctx.memory.apply_compact(
            scope=state.scope,
            summary=summary,
            keep_last=keep_last,
            ctx=ctx.provider_ctx,
            layer=MemoryLayer.TASK,
            protect_types=(MemoryEventType.USER_PROMPT,),
        )
```
（文件顶部确认已 import `MemoryEventType`；observe.py 已 import `MemoryLayer`。）

- [ ] **Step 4: compact.py 传 protect_types**

`compact.py` `_compact_scope` 内 task 层 `apply_compact`：
```python
        result = await ctx.memory.apply_compact(
            scope=state.scope, summary=summary_text or "[Context compacted]",
            keep_last=keep_last, ctx=ctx.provider_ctx, layer=MemoryLayer.TASK,
            protect_types=(MemoryEventType.USER_PROMPT,))
```

- [ ] **Step 5: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_protects_user.py tests/unit/test_observe_task_compaction.py tests/unit/test_predispatch_compact.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/observe.py src/ctx_weft/core/loop/steps/compact.py tests/unit/test_compact_protects_user.py
git commit -m "feat(compact): max_turns/context_limit 压缩保护 USER_PROMPT 锚点

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 3 · root 后台异步 observe

### Task 4: `background_observe.py` 模块 + 并发串行

**Files:**
- Create: `src/ctx_weft/core/loop/steps/background_observe.py`
- Test: `tests/unit/test_background_observe.py`（新）

**Interfaces:**
- Consumes: `compact.summarize_for_compact(state, ctx) -> str`；`memory.apply_compact(..., protect_types=(USER_PROMPT,))`；`task_manager.track_background(asyncio.Task)`。
- Produces:
  - `launch_background_observe(state: LoopState, ctx: LoopContext) -> asyncio.Task`：快照 state、串行锁内跑 `_run_background_observe`、注册 track_background，返回 task。
  - `_run_background_observe(state, ctx) -> None`：`summarize_for_compact` → `apply_compact(TASK, keep_last=0, protect=(USER_PROMPT,))`；异常吞掉（log）。
  - `await_pending_background_observe(task_id: str) -> None`：await 该 task 仍在跑的后台 observe（供 finalize 强一致，Task 8 用）。
  - 模块级 `_task_locks: dict[str, asyncio.Lock]`、`_task_pending: dict[str, asyncio.Task]`。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_background_observe.py`：
```python
import asyncio
import pytest
from ctx_weft.core.loop.steps import background_observe as bo


@pytest.mark.asyncio
async def test_launch_produces_summary_and_folds(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx  # 见 conftest：task 层有 [UP, llm, tool]
    monkeypatch.setattr(bo, "summarize_for_compact", _async_const("段摘要文本"))
    t = bo.launch_background_observe(state, ctx)
    await t
    # apply_compact 被调、产出 TASK_COMPACT_SUMMARY、UP 保留
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = {r.type for r in recs}
    assert MT.TASK_COMPACT_SUMMARY in types
    assert MT.USER_PROMPT in types
    assert MT.LLM_RESPONSE not in types


@pytest.mark.asyncio
async def test_serialized_per_task(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx
    order = []
    async def slow(s, c):
        order.append("start"); await asyncio.sleep(0.01); order.append("end"); return "x"
    monkeypatch.setattr(bo, "summarize_for_compact", slow)
    t1 = bo.launch_background_observe(state, ctx)
    t2 = bo.launch_background_observe(state, ctx)
    await asyncio.gather(t1, t2)
    assert order == ["start", "end", "start", "end"]  # 串行，不交错


@pytest.mark.asyncio
async def test_failure_swallowed(monkeypatch, fake_state_ctx):
    state, ctx = fake_state_ctx
    async def boom(s, c): raise RuntimeError("llm down")
    monkeypatch.setattr(bo, "summarize_for_compact", boom)
    t = bo.launch_background_observe(state, ctx)
    await t  # 不抛
    assert t.exception() is None
```
（在 `tests/unit/conftest.py` 加 `fake_state_ctx` fixture：InMemoryMemoryProvider + 最小 LoopState/LoopContext，task 层预置 [UP, llm, tool]，`ctx.task_manager` 提供 no-op `track_background`。`_async_const` 返回固定串的 async 函数。）

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现模块**

`src/ctx_weft/core/loop/steps/background_observe.py`：
```python
"""root 后台异步 observe：在交互/finish 段边界产段摘要并折 raw（spec §3.2）。

仿 recognize_intent 的 fire-and-forget：快照 state、create_task、track_background。
同一 task 至多一个在跑（_task_locks 串行），失败吞掉（降级 = 该段保 raw，spec §3.6）。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from ctx_weft.core.loop.steps.compact import summarize_for_compact
from ctx_weft.protocols import MemoryEventType, MemoryLayer

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)

_task_locks: dict[str, asyncio.Lock] = {}
_task_pending: dict[str, asyncio.Task] = {}
_orphan_tasks: set[asyncio.Task] = set()


def _lock_for(task_id: str) -> asyncio.Lock:
    lock = _task_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[task_id] = lock
    return lock


async def _run_background_observe(state: "LoopState", ctx: "LoopContext") -> None:
    async with _lock_for(state.task.id):
        try:
            summary = await summarize_for_compact(state, ctx)
            await ctx.memory.apply_compact(
                scope=state.scope,
                summary=summary or "[Context compacted]",
                keep_last=0,
                ctx=ctx.provider_ctx,
                layer=MemoryLayer.TASK,
                protect_types=(MemoryEventType.USER_PROMPT,),
            )
        except Exception:
            logger.exception("background observe failed (ignored); segment kept raw")


def launch_background_observe(state: "LoopState", ctx: "LoopContext") -> asyncio.Task:
    snapshot = dataclasses.replace(state)
    task = asyncio.create_task(_run_background_observe(snapshot, ctx))
    _task_pending[state.task.id] = task
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _orphan_tasks.add(task)
        task.add_done_callback(_orphan_tasks.discard)
    task.add_done_callback(lambda _t, tid=state.task.id: _task_pending.pop(tid, None) and None)
    return task


async def await_pending_background_observe(task_id: str) -> None:
    """供 finalize 强一致：若该 task 有在跑的后台 observe，等它完成（spec §3.3 step 1）。"""
    pending = _task_pending.get(task_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)
```

- [ ] **Step 4: 运行验证通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe.py -v`
Expected: PASS（三测试全过）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_background_observe.py tests/unit/conftest.py
git commit -m "feat(observe): root 后台异步 observe 模块（串行 + 失败降级）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 在交互 / finish 段边界接线 `launch_background_observe`

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（ask_human 路径 + root normal-exit finish）
- Modify: `src/ctx_weft/core/loop/steps/act.py`（软打断 park 前）
- Test: `tests/unit/test_background_observe_wiring.py`（新）

**Interfaces:**
- Consumes: `launch_background_observe(state, ctx)`（Task 4）。
- 触发条件：仅 root（`state.task.parent_task_id is None` 或 cross_agent own-root）；非 root 不触发（spec §3.2）。

- [ ] **Step 1: 写失败测试**

`test_background_observe_wiring.py`：mock `launch_background_observe` 计数；
- root task 走 ask_human 段边界 → 断言被调一次；
- root task 正常 finish（actor_done，normal exit）→ 断言被调一次；
- 子任务（parent_task_id 非空、同 agent）走相同路径 → 断言**未**被调（max_turns 仍同步、走 `_maybe_compact_task`）。

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_wiring.py -v`
Expected: FAIL（未接线，计数 0）

- [ ] **Step 3: 加 root 判定 helper + 接线**

`observe.py` 加 helper（与 finalize 的 own-root 判定一致）：
```python
def _is_own_root(task) -> bool:
    same_agent = task.creator_agent_id == task.assigned_agent_id
    cross_agent = bool(task.parent_task_id) and not same_agent
    return task.parent_task_id is None or cross_agent
```
在 ask_human 分支（observe 内 park HITL 取回回复**之前**）插：
```python
        if _is_own_root(state.task):
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(state, ctx)
```
在 root normal-exit（`act_exit_reason in ("normal", "actor_done")` 且 `_is_own_root`，即 finish_task 收尾路径）同样插一次 launch（finalize 会 await 它，Task 8）。

`act.py` 在 `_park_wait_for_user(..., source="interrupt")` **之前**（软打断段边界）插同样的 root-gated launch。

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_background_observe_wiring.py tests/unit/test_interrupt_act.py tests/unit/test_hitl_ask_human_cold.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/observe.py src/ctx_weft/core/loop/steps/act.py tests/unit/test_background_observe_wiring.py
git commit -m "feat(observe): 交互/finish 段边界触发 root 后台 observe（非 root 不触发）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 4 · close 胶囊合成重写

### Task 6: `_synthesize_dispatch_pair` 重写为交错时间线 + finish 对

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:225-313`（`_synthesize_dispatch_pair`）
- Test: `tests/unit/test_capsule_interleaved.py`（新）

**Interfaces:**
- Consumes: `await_pending_background_observe(task_id)`（Task 4）；`memory.recall_recent(task_scope, [...])`；`_build_memory_content`（finalize 现有）。
- Produces: `_synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx)`（签名不变）写出：
  - 每条幸存 task 层回合 → agent 层 `AGENT_CONVERSATION_TURN`，**保留原始 timestamp**、role、`tool_calls`/`tool_call_id`，metadata `origin_task_id=task.id`；
  - 末尾 finish 对：assistant(`tool_calls=[{id, name=control__finish_task, input={result: outputs}}]`) + tool(`Process Report: ...`)，metadata `origin_task_id=task.id`，timestamp `now_utc()`。

- [ ] **Step 1: 写失败测试**

`test_capsule_interleaved.py`：构造 task 层 = [UP1, TASK_COMPACT_SUMMARY("段①"), UP2(HITL), TASK_COMPACT_SUMMARY("段②")]（模拟后台 observe 已折好），task.outputs="最终答复"，verdict.summary="过程报告"。调 `_synthesize_dispatch_pair`。断言 agent 层 AGENT_CONVERSATION_TURN 序列（按 timestamp）：
```python
# 期望（情况 2/5 形态）：
# [user 原始] [assistant 段①] [user HITL] [assistant 段②] [assistant finish_task(tool_calls)] [tool Process Report]
caps = await mem.recall_recent(agent_scope, [MemoryEventType.AGENT_CONVERSATION_TURN], 100, ctx)
caps = list(reversed(caps))
assert caps[0].role == "user" and caps[0].content == "UP1原文"
assert caps[1].role == "assistant" and "段①" in caps[1].content
assert caps[2].role == "user" and caps[2].content == "HITL原文"
assert caps[3].role == "assistant" and "段②" in caps[3].content
assert caps[-2].role == "assistant" and caps[-2].metadata["tool_calls"][0]["name"].endswith("finish_task")
assert caps[-2].metadata["tool_calls"][0]["input"]["result"] == "最终答复"
assert caps[-1].role == "tool" and caps[-1].content.startswith("Process Report:")
assert all(c.metadata.get("origin_task_id") == task.id for c in caps)
```

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_interleaved.py -v`
Expected: FAIL（现实现只写 user+summary+delegate 对）

- [ ] **Step 3: 重写 `_synthesize_dispatch_pair`**

```python
async def _synthesize_dispatch_pair(memory, scope, task, mem_content, outcome, provider_ctx) -> None:
    """close 合成交错时间线胶囊（spec §3.3）：快照幸存 task 对话 → AGENT_CONVERSATION_TURN
    （保留原始 timestamp）+ 末尾合成 finish 对。"""
    from ctx_weft.core.loop.steps.background_observe import await_pending_background_observe
    from ctx_weft.core.utils import content_to_text

    # step1：强一致——等本 task 的后台 observe（末段摘要）跑完（spec §3.3 step1，方案乙）
    await await_pending_background_observe(task.id)

    task_scope = MemoryScope(session_id=scope.session_id, task_id=task.id, agent_id=scope.agent_id)
    survivors = await memory.recall_recent(
        task_scope,
        [MemoryEventType.USER_PROMPT, MemoryEventType.TASK_COMPACT_SUMMARY,
         MemoryEventType.LLM_RESPONSE, MemoryEventType.TOOL_RESULT],
        2000, provider_ctx,
    )
    survivors = list(reversed(survivors))  # newest-first → 时间序

    # step2：逐条镜像成 agent 层 AGENT_CONVERSATION_TURN，保留原始 timestamp/role/tool 信息
    for r in survivors:
        md = {"origin_task_id": task.id}
        role = r.role or "user"
        if role == "assistant":
            md["tool_calls"] = r.metadata.get("tool_calls", [])
        elif role == "tool":
            md["tool_call_id"] = r.metadata.get("tool_call_id", "")
        await memory.ingest(
            MemoryEvent(
                type=MemoryEventType.AGENT_CONVERSATION_TURN,
                scope=scope, content=r.content, timestamp=r.timestamp, role=role, metadata=md,
            ),
            provider_ctx,
        )

    # step3：合成 finish 对（最终段承载，spec §3.3 step3 + §3.5）
    base = now_utc()
    tool_call_id = generate_id("tcall")
    outputs_text = (
        task.outputs if isinstance(task.outputs, str) else content_to_text(task.outputs or "")
    ) or ("(无最终产出)" if outcome == "fail" else "")
    report_prefix = "[outcome=fail] " if outcome == "fail" else ""
    summary_text = mem_content  # mem_content 已含 outputs+Process Report；取其 report 部分
    # finish 对的 tool 结果只放 Process Report（outputs 已在 tool_call 的 result 里）
    report_only = summary_text.split("Process Report: ", 1)[-1] if "Process Report: " in summary_text else summary_text
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope, content="",
            timestamp=base, role="assistant",
            metadata={"origin_task_id": task.id, "tool_calls": [{
                "id": tool_call_id,
                "name": qualify("control:finish_task"),
                "input": {"result": outputs_text},
            }]},
        ),
        provider_ctx,
    )
    await memory.ingest(
        MemoryEvent(
            type=MemoryEventType.AGENT_CONVERSATION_TURN, scope=scope,
            content=f"{report_prefix}Process Report: {report_only}",
            timestamp=base, role="tool",
            metadata={"origin_task_id": task.id, "tool_call_id": tool_call_id},
        ),
        provider_ctx,
    )
```

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_interleaved.py tests/unit/test_root_self_experience.py tests/unit/test_agent_recall_source.py -v`
Expected: PASS（注：`test_root_self_experience.py` 旧断言「user+summary+delegate 对」会变——按新形态更新该测试的期望，纳入本 step）

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_capsule_interleaved.py tests/unit/test_root_self_experience.py
git commit -m "feat(capsule): 交错时间线 + finish_task 对承载，保留原始 timestamp

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `agent_recall` 渲染 finish 对（AGENT_CONVERSATION_TURN 带 tool_calls）

**Files:**
- Modify (验证/微调): `src/ctx_weft/core/assembler/sources/agent_recall.py:114-115`
- Test: `tests/unit/test_capsule_render.py`（新）

**Interfaces:**
- Consumes: Task 6 写出的 AGENT_CONVERSATION_TURN（含 tool_calls/tool_call_id）。
- Produces: 装配出的 messages：assistant(tool_calls=[finish_task]) 与 tool(content=Process Report, tool_call_id) 配对、过 gateway 不被当孤儿。

- [ ] **Step 1: 写失败测试**

`test_capsule_render.py`：用 Task 6 的胶囊跑 `AgentRecallSource.fetch` + composer，断言渲染出的 message 序列里 finish 对 = assistant(tool_calls 含 finish_task, input.result=outputs) + 紧邻 tool(tool_call_id 配对)；且过 `drop_orphan_tool_results` 后仍配对存活。

- [ ] **Step 2: 运行验证失败 / 或直接通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_render.py -v`
Expected: 若 `record_to_history_block`（_history.py）已正确处理 role=assistant→tool_calls / role=tool→tool_call_id（已确认支持），则 PASS；若 composer 对 AGENT_CONVERSATION_TURN 的 tool 角色有遗漏，FAIL。

- [ ] **Step 3: 按需微调 agent_recall**

若 Step 2 FAIL：`agent_recall.py:114-115` 的 conversation 渲染确保走 `record_to_history_block`（已走）；检查 composer 是否对 `AGENT_CONVERSATION_TURN` 的 tool 角色生成 tool_result block。若 composer 按 `metadata["tool_call_id"]` 生成 tool block 即可，无需改；否则补 tool 角色分支（与 TOOL_RESULT 同路）。

- [ ] **Step 4: 运行验证通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_render.py tests/unit/test_assembler_reconstruction.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/assembler/sources/agent_recall.py tests/unit/test_capsule_render.py
git commit -m "test(capsule): finish 对渲染 + 孤儿配对回归（按需微调 agent_recall）

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: `fold_root_experience` anchor 扩到保留胶囊全部元素

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py:95-148`（`fold_root_experience` 的 `anchor_ts` + supersede 列表）
- Test: `tests/unit/test_root_subtree_fold.py`（更新）

**Interfaces:**
- Consumes: Task 6 的多回合胶囊（多 user 锚点 + 段摘要 + finish 对，均 `origin_task_id` 标记）。

- [ ] **Step 1: 写失败测试**

更新/新增：构造 > keep_last 个多回合胶囊触发 `fold_root_experience`，断言：被折胶囊的**全部** AGENT_CONVERSATION_TURN（按 origin_task_id ∈ 被折 child_task_id 集合）+ 其 finish 对一并 supersede；`AGENT_COMPACT_SUMMARY.timestamp` = min(保留胶囊全部元素 timestamp) − 1µs（不再只看 TASK_DISPATCH_RESULT）。

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_subtree_fold.py -v`
Expected: FAIL（anchor 只算 root_results；落单或排序错）

- [ ] **Step 3: 改 `fold_root_experience`**

`anchor_ts` 改为对**保留胶囊全部元素**取 min。保留胶囊元素 = `kept` root_results 的 child_task_id 对应的全部 AGENT_CONVERSATION_TURN + TASK_DISPATCH/RESULT。实现：
```python
    kept_task_ids = {r.metadata.get("child_task_id") for r in kept}
    kept_elements_ts = [r.timestamp for r in recs
                        if r.metadata.get("child_task_id") in kept_task_ids
                        or r.metadata.get("origin_task_id") in kept_task_ids]
    anchor_ts = (min(kept_elements_ts) if kept_elements_ts else now_utc()) - timedelta(microseconds=1)
```
supersede 列表已含 `origin_task_id ∈ fold_task_ids` 的 AGENT_CONVERSATION_TURN（现有逻辑，line 128-130）——确认 fold_task_ids 用 child_task_id 推导且覆盖 finish 对（finish 对的 origin_task_id = task.id = child_task_id）。

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_root_subtree_fold.py tests/unit/test_dispatch_fold_golden.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/compact.py tests/unit/test_root_subtree_fold.py
git commit -m "fix(fold): anchor_ts 扩到保留胶囊全部元素，多回合胶囊不落单

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 5 · 子任务承载（同 agent 嵌套 / 跨 agent 黑盒）

### Task 9: bubble 分流（同 agent = scheduled，跨 agent = mem_content）+ 同 agent 嵌套子胶囊

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:118-187`（`_close_one` 的 bubble step1）
- Test: `tests/unit/test_subtask_nesting.py`（新）

**Interfaces:**
- Consumes: `_synthesize_dispatch_pair`（Task 6，复用于把同 agent child 胶囊写进共享 agent scope）。
- 关键：同 agent ⟹ parent/child 共用 agent scope（scope_key 按 agent_id）。同 agent child close 时：(a) bubble 的 `TASK_DISPATCH_RESULT` content = `"Sub-task '{title}' scheduled."`、timestamp = dispatch 时刻（保留原始）；(b) 调 `_synthesize_dispatch_pair` 把 child 自己的交互胶囊写进**共享 agent scope**（origin_task_id=child.id）。跨 agent child：bubble content = `mem_content`（result+report），不写嵌套胶囊。

- [ ] **Step 1: 写失败测试**

`test_subtask_nesting.py`：
- 同 agent child（use_subagent=False，creator==assigned）含一次 HITL，close。断言：parent agent scope 里 TASK_DISPATCH_RESULT content == "Sub-task 'X' scheduled."；child 的胶囊回合（user 锚点 + 段摘要 + 子 finish 对）以 AGENT_CONVERSATION_TURN(origin_task_id=child.id) 出现在 parent agent scope；按 timestamp 排在 dispatch 之后。
- 跨 agent child（use_subagent=True，creator!=assigned）close。断言：parent scope TASK_DISPATCH_RESULT content == mem_content（含 "Process Report:"）；parent scope **无** origin_task_id=child.id 的 AGENT_CONVERSATION_TURN。

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_subtask_nesting.py -v`
Expected: FAIL

- [ ] **Step 3: 改 `_close_one` bubble step1**

现有 step1 把 `mem_content` 写进 parent scope 的 TASK_DISPATCH_RESULT。改为分流：
```python
    if task.parent_task_id and task.origin_tool_call_id:
        if cross_agent:
            do_bubble = True
            result_content = mem_content
        elif same_agent and not short:
            do_bubble = True
            result_content = f"Sub-task '{task.title}' scheduled."
        else:
            do_bubble = False
        if do_bubble and result_content:
            parent_scope = MemoryScope(
                session_id=state.scope.session_id,
                task_id=task.parent_task_id,
                agent_id=task.creator_agent_id,
            )
            await memory.ingest(MemoryEvent(
                type=MemoryEventType.TASK_DISPATCH_RESULT, scope=parent_scope,
                content=result_content, timestamp=now_utc(), role="tool",
                metadata={"tool_call_id": task.origin_tool_call_id, "child_task_id": task.id,
                          "title": task.title, "outcome": outcome,
                          "parent_task_id": task.parent_task_id}), ctx.provider_ctx)
            events.append(make_event(state, EventType.MEMORY_INGESTED, payload={
                "memory_event_type": MemoryEventType.TASK_DISPATCH_RESULT.value,
                "source": "dispatch_result", "content_length": len(result_content)}))
        # 同 agent：额外把 child 交互胶囊写进共享 agent scope（嵌套）
        if same_agent and not short:
            await _synthesize_dispatch_pair(
                memory, parent_scope, task, mem_content, outcome, ctx.provider_ctx)
```
（`_synthesize_dispatch_pair` 写 AGENT_CONVERSATION_TURN 到 `parent_scope`——同 agent 下 parent_scope.agent_id == child agent_id，即共享 scope；origin_task_id=child.id 区分。）

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_subtask_nesting.py tests/unit/test_delegation.py tests/unit/test_close_task.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_subtask_nesting.py
git commit -m "feat(subtask): 同 agent 平铺嵌套子胶囊 / 跨 agent 黑盒 bubble 分流

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: `_gc_subtree` 保留直属派发对 / 嵌入子胶囊

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:71-101`（`_gc_subtree`）
- Test: `tests/unit/test_gc_preserve_dispatch.py`（新）

**Interfaces:**
- 改后：`_gc_subtree` 只 GC 后代 task 的**自身 task 层 raw 残留**，**不再** supersede parent 直属 child 的 `TASK_DISPATCH`/`TASK_DISPATCH_RESULT` 与嵌入子胶囊 `AGENT_CONVERSATION_TURN`（这些由 parent close 的 `_synthesize_dispatch_pair` + `fold_root_experience` 管理）。

- [ ] **Step 1: 写失败测试**

`test_gc_preserve_dispatch.py`：root 委派 child（同 + 跨 agent 各一），root close 后断言：直属 child 的 TASK_DISPATCH/RESULT 与同 agent 嵌入子胶囊 AGENT_CONVERSATION_TURN **未** superseded；仅 child 自身 task 层 raw（USER_PROMPT/LLM_RESPONSE/TOOL_RESULT in child task scope）被 GC。

- [ ] **Step 2: 运行验证失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_gc_preserve_dispatch.py -v`
Expected: FAIL（现 `_gc_subtree` 把直属派发对也 supersede）

- [ ] **Step 3: 改 `_gc_subtree`**

去掉对「后代 agent 层 dispatch 对」的 supersede（现 line 83-100 那段把 child_task_id ∈ descendants 的 TASK_DISPATCH_RESULT + 配对 TASK_DISPATCH 加进 ids）。保留对「后代 task 层对话」的 GC（line 76-82）。即只删 raw task 对话，派发对/嵌套胶囊留给 parent 胶囊体系。
```python
async def _gc_subtree(memory, agent_scope, descendants: set[str], ctx) -> int:
    """软删后代 task 的自身 task 层 raw 残留（派发对/嵌入子胶囊保留，由 parent 胶囊管理）。"""
    if not descendants:
        return 0
    ids: list[str] = []
    task_recs = await memory.recall_recent_by_agent(
        agent_scope, _OWN_CONV_TYPES, 2000, ctx.provider_ctx,
    )
    for r in task_recs:
        if r.metadata.get("task_id") in descendants:
            ids.append(r.id)
    return await memory.supersede(ids, ctx.provider_ctx) if ids else 0
```

- [ ] **Step 4: 运行验证通过 + 回归**

Run: `cd ctx-weft && uv run pytest tests/unit/test_gc_preserve_dispatch.py tests/unit/test_root_subtree_fold.py tests/unit/test_close_task.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_gc_preserve_dispatch.py
git commit -m "feat(subtask): _gc_subtree 保留直属派发对/嵌入子胶囊

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 6 · 端到端 golden + 边界

### Task 11: 端到端 golden（情况 1–8）+ 边界用例

**Files:**
- Test: `tests/unit/test_capsule_golden.py`（新）

**Interfaces:**
- Consumes: 全部前置 task。

- [ ] **Step 1: 写 golden 测试（spec §4 情况 1–8 + 边界）**

`test_capsule_golden.py` 覆盖 spec §5 未被前面 task 单测覆盖的剩余项：
- A1 单段正常 finish（无段摘要、只 finish 对）
- A3 ② 流中打断：段摘要含「被打断」、半截 assistant/cancelled tool 不进胶囊
- A4 ① 编辑式打断：两 user 锚点相邻
- A6 fail：finish 对 result="(无最终产出)"、tool 带 "[outcome=fail]"
- A10 short task 不合成胶囊（断言不调 `_synthesize_dispatch_pair`）
- H3 递归嵌套（同 agent 孙任务）
- H4 跨 agent 隔离（parent 看不到子 agent 段摘要）
- H8 短同 agent 子任务未配对隐去

每条用「构造事件序列 → close/装配 → 断言重建 messages」黄金风格（参照 Task 6 Step1 断言写法）。

- [ ] **Step 2: 运行**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_golden.py -v`
Expected: PASS（逐条修实现到过为止；任何 FAIL 回到对应 Phase task 修）

- [ ] **Step 3: 全量回归**

Run: `cd ctx-weft && uv run pytest && cd .. && uv run pytest tests/unit/test_postgres_compact_user_aware.py tests/unit/test_postgres_recall_by_agent.py -v`
Expected: 全绿

- [ ] **Step 4: 提交**

```bash
git add tests/unit/test_capsule_golden.py
git commit -m "test(capsule): 端到端 golden（情况 1–8）+ 边界用例

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 5.5 · fold 适配新胶囊格式（执行于 Task 10 之后、Task 11 之前）

### Task 12: agent 层压缩识别/折叠新 AGENT_CONVERSATION_TURN 胶囊

> 插入原因：Task 6 让 root 胶囊变成纯 `AGENT_CONVERSATION_TURN`，但 `_count_root_residues` /
> `fold_root_experience` 仍按 `TASK_DISPATCH_RESULT(parent=None)` 识别 → 新格式数到 0 → agent 层
> 压缩永不触发、无限增长。Task 8 只改了 anchor，未改识别判据（其测试 seed 了假的旧格式标记）。
> 详见 spec §3.11。

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（`_synthesize_dispatch_pair`：每个 AGENT_CONVERSATION_TURN metadata 加 `parent_task_id=task.parent_task_id`）
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（重写 `_count_root_residues` + `fold_root_experience` 按 spec §3.11）
- Test: `tests/unit/test_root_subtree_fold.py`（改用真实 Task-6 格式胶囊，去掉 seed 的假 `TASK_DISPATCH_RESULT(parent=None)`）

**Interfaces:**
- Consumes: Task 6 的 AGENT_CONVERSATION_TURN（+新 `parent_task_id`）、Task 9 的 cross-agent 派发对（`TASK_DISPATCH_RESULT` 带 `parent_task_id`）。
- 判据：顶层折叠单元 = origin 组 `parent_task_id is None` 或 `parent_task_id ∉ 本 scope origin 集`。详见 spec §3.11。

实现细节与 TDD 步骤见对应 task brief（`.superpowers/sdd/task-12-brief.md`）——含 `_count_root_residues` /
`fold_root_experience` 完整重写代码、真实格式 golden、cross-agent 自 scope 顶层 + 同 agent 内嵌一并折的测试。

## Phase 7 · 交互式回合边界（最终 review 推荐项）

### Task 13: 纯文本暂停（wait_for_user）触发 root 后台 observe

> 最终 whole-branch review 发现：后台 observe 缺了交互式聊天的天然边界 `wait_for_user`（纯文本暂停），
> 导致交互式 root 每轮处理段留 raw（spec §3.6 优雅降级）。补第 4 个触发点，与现有 3 个对称。详见
> spec §3.2 新增行 + `.superpowers/sdd/task-13-brief.md`。

**Files:** Modify `src/ctx_weft/core/loop/steps/act.py`（`_finish_plain_text_turn` interactive 分支，
park 前 root-gated `launch_background_observe`，复用已有 `_is_own_root`）；Test `tests/unit/test_background_observe_wiring.py`。

## Self-Review（plan 作者自查结论）

- **Spec coverage**：§3.1→Task1-3；§3.2→Task4-5；§3.3→Task6（含 await 强一致 step1、原始 timestamp、finish 对）；§3.4（finish SILENT）→Task6 用合成、无需改 gateway；§3.5 fail→Task6+Task11 A6；§3.6 降级→Task4 失败吞 + Task6 残留 raw 镜像；§3.7 打断→Task5 接线 + Task11 A3/A4；§3.9 子任务→Task9-10 + Task11 H 组；§2.2 anchor→Task8。全部有 task。
- **Placeholder scan**：无 TBD；novel 逻辑（provider、background_observe、capsule synth、bubble 分流、gc）均给完整代码；测试夹具指明复用现有 conftest/fixture 来源。
- **Type consistency**：`apply_compact(..., protect_types: tuple[MemoryEventType, ...] = ())` 在 protocol/in-memory/postgres/全调用点一致；`AGENT_CONVERSATION_TURN` metadata 键 `origin_task_id`/`tool_calls`/`tool_call_id` 与 `_history.py` 渲染键一致；`_is_own_root` 判定与 finalize `_close_one` 的 same_agent/cross_agent 一致。
- **已知顺序依赖**：Task6 依赖 Task4（await_pending）；Task9 复用 Task6 的 `_synthesize_dispatch_pair`；Task8/10 依赖 Task6 的胶囊形态。按编号顺序执行。
