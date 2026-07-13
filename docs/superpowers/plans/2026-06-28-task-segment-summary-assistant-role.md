# task 层段摘要承载为 assistant 自述 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 task 层 `TASK_COMPACT_SUMMARY`（运行层段摘要）以 assistant 自述承载——存储 role=assistant、渲染不套包装、close 胶囊自然继承——并保持 agent 层折叠摘要 `AGENT_COMPACT_SUMMARY` 不变。

**Architecture:** 存储层 `apply_compact` 按 `layer` 写 role（TASK→assistant、AGENT→user）；渲染层 `record_to_history_block` 包装判据从「按 type」改「按 role==user」；close 层 `_synthesize_dispatch_pair` 删掉冗余的 `user→assistant` 覆盖特判，自然继承存储 role。两份 provider（in-memory + postgres）同步改。

**Tech Stack:** Python 3.11、pytest（`uv run pytest`）、SQLAlchemy + aiosqlite（postgres provider 测试用 sqlite backend）。

## Global Constraints

- 测试一律用 `uv run pytest`（pyproject 已配 `pythonpath=["."]`）；host 仓测试在仓根 `tests/`，core 测试在 `tests/`。
- **两份 provider 必须行为一致**：`src/ctx_weft/providers/memory_blackboard/in_memory.py` 与 `src/ipmastercowork/providers/memory/postgres.py` 的 `apply_compact` role 逻辑同构。
- **B 不动**：`AGENT_COMPACT_SUMMARY`（agent 层折叠摘要）全链路保持 role=user + 包装——它是 fold 后 prompt 首条，Anthropic 首条 assistant 会 400。
- **不变量 1 已落地、依赖之**：三个 task 层 compact 入口（`observe.py:361`、`background_observe.py:50`、`compact.py:235`）均传 `protect_types=(USER_PROMPT,)`，段摘要前恒有 user 锚点、永不成首条。本计划不改这些入口。
- 段摘要**内容语气不改**（仍报告体）；只动 role/包装。
- Spec：`docs/superpowers/specs/2026-06-27-task-segment-summary-assistant-role-design.md`。

---

### Task 1: in-memory `apply_compact(TASK)` 写 role=assistant

**Files:**
- Modify: `src/ctx_weft/providers/memory_blackboard/in_memory.py:252-259`
- Test: `tests/unit/test_compact_user_aware.py`

**Interfaces:**
- Consumes: `InMemoryMemoryProvider.apply_compact(scope, summary, keep_last, ctx, layer, protect_types)`（已存在）。
- Produces: `apply_compact(layer=TASK)` 折出的 `TASK_COMPACT_SUMMARY` 记录 `role == "assistant"`；`layer=AGENT` 折出的 `AGENT_COMPACT_SUMMARY` 记录 `role == "user"`。

- [ ] **Step 1: 写失败测试**（追加到 `test_compact_user_aware.py` 末尾）

```python
@pytest.mark.asyncio
async def test_apply_compact_task_summary_role_is_assistant():
    """task 层段摘要 = LLM 自述 → role=assistant。"""
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.USER_PROMPT, "原始诉求", base, "user")
    await _ingest(p, MemoryEventType.LLM_RESPONSE, "想法", base + timedelta(seconds=1), "assistant")
    await p.apply_compact(
        scope=_scope(), summary="段摘要", keep_last=0, ctx=_ctx(),
        layer=MemoryLayer.TASK, protect_types=(MemoryEventType.USER_PROMPT,),
    )
    recs = await p.recall_recent(_scope(), [MemoryEventType.TASK_COMPACT_SUMMARY], 100, _ctx())
    assert len(recs) == 1
    assert recs[0].role == "assistant"


@pytest.mark.asyncio
async def test_apply_compact_agent_summary_role_stays_user():
    """agent 层折叠摘要是 prompt 首条，必须 role=user（B 不动）。"""
    p = InMemoryMemoryProvider()
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    await _ingest(p, MemoryEventType.TASK_DISPATCH, "delegate", base, "assistant")
    await _ingest(p, MemoryEventType.TASK_DISPATCH_RESULT, "done", base + timedelta(seconds=1), "tool")
    await p.apply_compact(
        scope=_scope(), summary="派发摘要", keep_last=0, ctx=_ctx(),
        layer=MemoryLayer.AGENT,
    )
    recs = await p.recall_recent(_scope(), [MemoryEventType.AGENT_COMPACT_SUMMARY], 100, _ctx())
    assert len(recs) == 1
    assert recs[0].role == "user"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_user_aware.py::test_apply_compact_task_summary_role_is_assistant -v`
Expected: FAIL —`assert 'user' == 'assistant'`（现状硬编码 role="user"）。

- [ ] **Step 3: 改实现**（in_memory.py:252-259）

把：
```python
        compact_event = MemoryEvent(
            type=summary_type,
            scope=scope,
            content=summary,
            timestamp=summary_ts,
            role="user",
            metadata={"keep_last": keep_last, "archived_count": len(to_archive)},
        )
```
改为：
```python
        # task 层段摘要 = LLM 对前段的自述（role=assistant）；agent 层折叠摘要是 prompt
        # 首条、Anthropic 首条 assistant 会 400，故保持 role=user。
        summary_role = "assistant" if layer is MemoryLayer.TASK else "user"
        compact_event = MemoryEvent(
            type=summary_type,
            scope=scope,
            content=summary,
            timestamp=summary_ts,
            role=summary_role,
            metadata={"keep_last": keep_last, "archived_count": len(to_archive)},
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_user_aware.py -v`
Expected: PASS（新增两条 + 原有 `test_apply_compact_task_protects_user_prompt` 全绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/providers/memory_blackboard/in_memory.py tests/unit/test_compact_user_aware.py
git commit -m "feat(memory): apply_compact(TASK) 段摘要存 role=assistant；AGENT 仍 user"
```

---

### Task 2: postgres `apply_compact(TASK)` 写 role=assistant

**Files:**
- Modify: `src/ipmastercowork/providers/memory/postgres.py:357-370`
- Test: `tests/unit/test_postgres_compact_user_aware.py`

**Interfaces:**
- Consumes: `PostgresMemoryProvider.apply_compact(...)`（与 in-memory 同签名）。
- Produces: 与 Task 1 同契约——`layer=TASK` 段摘要 role=assistant、`layer=AGENT` 折叠摘要 role=user。

- [ ] **Step 1: 写失败测试**（追加到 `test_postgres_compact_user_aware.py` 末尾）

```python
async def test_pg_apply_compact_task_summary_role_is_assistant(tmp_path) -> None:
    p = await _provider(tmp_path)
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    ctx = _ctx()
    sc = MemoryScope(session_id="s1", task_id="task1", agent_id="a1")

    async def ing(typ, c, ts, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c, timestamp=ts, role=role), ctx)

    await ing(T.USER_PROMPT,  "原始诉求", base,                       "user")
    await ing(T.LLM_RESPONSE, "想法",    base + timedelta(seconds=1), "assistant")

    await p.apply_compact(scope=sc, summary="段摘要", keep_last=0, ctx=ctx,
                          layer=MemoryLayer.TASK, protect_types=(T.USER_PROMPT,))

    recs = await p.recall_recent(sc, [T.TASK_COMPACT_SUMMARY], 100, ctx)
    assert len(recs) == 1
    assert recs[0].role == "assistant"


async def test_pg_apply_compact_agent_summary_role_stays_user(tmp_path) -> None:
    p = await _provider(tmp_path)
    base = datetime(2026, 6, 27, 10, 0, 0, tzinfo=UTC)
    ctx = _ctx()
    sc = MemoryScope(session_id="s1", task_id="task1", agent_id="a1")

    async def ing(typ, c, ts, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c, timestamp=ts, role=role), ctx)

    await ing(T.TASK_DISPATCH,        "delegate", base,                       "assistant")
    await ing(T.TASK_DISPATCH_RESULT, "done",     base + timedelta(seconds=1), "tool")

    await p.apply_compact(scope=sc, summary="派发摘要", keep_last=0, ctx=ctx,
                          layer=MemoryLayer.AGENT)

    recs = await p.recall_recent(sc, [T.AGENT_COMPACT_SUMMARY], 100, ctx)
    assert len(recs) == 1
    assert recs[0].role == "user"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_postgres_compact_user_aware.py::test_pg_apply_compact_task_summary_role_is_assistant -v`
Expected: FAIL —`assert 'user' == 'assistant'`。

- [ ] **Step 3: 改实现**（postgres.py:357-370）

把 `db.add(MemoryEventModel(...))` 里：
```python
                    type=str(summary_type),
                    role="user",  # 注入给下一轮 act loop 的上下文统一 role=user
```
改为：
```python
                    type=str(summary_type),
                    # task 层段摘要 = LLM 自述 → assistant；agent 层折叠摘要是 prompt 首条
                    # （Anthropic 首条 assistant 会 400）→ 保持 user。
                    role=("assistant" if layer is MemoryLayer.TASK else "user"),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_postgres_compact_user_aware.py -v`
Expected: PASS（新增两条 + 原有 protect 测试全绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ipmastercowork/providers/memory/postgres.py tests/unit/test_postgres_compact_user_aware.py
git commit -m "feat(memory): postgres apply_compact(TASK) 段摘要存 role=assistant（对齐 in-memory）"
```

---

### Task 3: `_history.py` 渲染包装判据「按 type → 按 role」

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/_history.py:35-38`
- Test: `tests/unit/test_compact_summary_wrap.py`

**Interfaces:**
- Consumes: `record_to_history_block(record, source, idx)`（已存在）。
- Produces: role=assistant 的 `TASK_COMPACT_SUMMARY` 渲染**不含**包装前缀；role=user 的（旧数据防御）仍套包装。`AGENT_COMPACT_SUMMARY` 不经本函数包装分支、行为不变。

- [ ] **Step 1: 写失败测试**（追加到 `test_compact_summary_wrap.py`，紧跟 `test_task_compact_summary_block_wrapped` 之后）

```python
def test_task_compact_summary_assistant_not_wrapped():
    """role=assistant 的段摘要 = 自述，不套「并非用户新指令」包装。"""
    blk = record_to_history_block(
        _rec(T.TASK_COMPACT_SUMMARY, "### 会话目标\nX", role="assistant"),
        "task_conversation", 0,
    )
    assert blk.content == "### 会话目标\nX"
    assert blk.metadata["role"] == "assistant"
```

> 现有 `test_task_compact_summary_block_wrapped`（用默认 role="user"）保留——它验证旧数据 role=user 仍包装（§4 R3 防御）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_summary_wrap.py::test_task_compact_summary_assistant_not_wrapped -v`
Expected: FAIL — `blk.content` 以包装前缀开头（现状按 type 无条件套包装）。

- [ ] **Step 3: 改实现**（_history.py:35-38）

把：
```python
    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    if record.type == MemoryEventType.TASK_COMPACT_SUMMARY:
        text = wrap_compact_summary(text)
    role = record.role or "user"
```
改为：
```python
    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    role = record.role or "user"
    # 包装是给「以 user 身份呈现」的摘要消歧义；assistant 自述无需。新数据段摘要恒 assistant
    # → 不套；旧数据若残留 role=user 仍套（防御）。AGENT_COMPACT_SUMMARY 在 agent_experience/
    # agent_recall 自行包装，不走此分支。
    if record.type == MemoryEventType.TASK_COMPACT_SUMMARY and role == "user":
        text = wrap_compact_summary(text)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_compact_summary_wrap.py -v`
Expected: PASS（5 条全绿：helper、user 包装、新增 assistant 不包装、plain user、AGENT 包装）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/assembler/sources/_history.py tests/unit/test_compact_summary_wrap.py
git commit -m "feat(assembler): 段摘要包装判据改按 role；assistant 自述不套包装"
```

---

### Task 4: `finalize.py` 删覆盖特判 + 同步 capsule golden 测试 setup

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/finalize.py:223-225`（docstring）、`243-250`（删特判）
- Modify（测试 setup，role="user" → "assistant"）：
  - `tests/unit/test_capsule_interleaved.py:9,79,81,122,169,172,177,178`
  - `tests/unit/test_capsule_golden.py:236-239（A3）,605-607（H4）`
  - `tests/unit/test_root_self_experience.py:6,89,109,148,149`
  - `tests/unit/test_dispatch_fold_golden.py:53-54`
- Modify（端到端 role 断言）：`tests/unit/test_background_observe.py:33-40`

**Interfaces:**
- Consumes: 存储已是 assistant（Task 1）。
- Produces: `_synthesize_dispatch_pair` 镜像 `TASK_COMPACT_SUMMARY` 时 `role` 直接来自存储（assistant），不再特判覆盖。

**背景**：现状 finalize.py 用 `elif r.type == TASK_COMPACT_SUMMARY: role = "assistant"` 覆盖存储的 user。Task 1 让存储已是 assistant，特判冗余。删特判后，**测试 setup 里手动 ingest 的 `role="user"` 必须改 assistant**，否则镜像出 user、断言失败。

- [ ] **Step 1: 改 finalize 实现**（finalize.py:243-250）

把：
```python
        if r.type == MemoryEventType.USER_PROMPT:
            role = "user"
        elif r.type == MemoryEventType.TASK_COMPACT_SUMMARY:
            role = "assistant"
        else:
            role = r.role or "user"  # LLM_RESPONSE→assistant, TOOL_RESULT→tool 已在记录上
```
改为：
```python
        if r.type == MemoryEventType.USER_PROMPT:
            role = "user"
        else:
            # TASK_COMPACT_SUMMARY 存储即 assistant；LLM_RESPONSE→assistant、TOOL_RESULT→tool
            # 均已在记录 role 上，直接继承。
            role = r.role or "user"
```

并更新 docstring（finalize.py:224-225）——把「角色映射：…TASK_COMPACT_SUMMARY→assistant（覆盖 DB 存储的 role=user）…」改为「…TASK_COMPACT_SUMMARY→assistant（继承存储 role）…」。

- [ ] **Step 2: 改 capsule golden 测试 setup 的 role**

逐处把手动 ingest `TASK_COMPACT_SUMMARY` 的 `role="user"` 改 `role="assistant"`，并删除/更新「覆盖 DB 存的 role=user」「stored as user, must render assistant」「apply_compact stores as role=user」这类注释为「apply_compact 存 role=assistant」：

- `test_capsule_interleaved.py`：L79、L81、L122、L178 的 `role="user"` → `role="assistant"`；L9 docstring `TASK_COMPACT_SUMMARY→ role="assistant" (覆盖 DB 存的 role="user")` 改为 `TASK_COMPACT_SUMMARY→ role="assistant" (继承存储 role)`；L169 注释与 L172/L177 docstring 同步去掉「覆盖存储值 role="user"」措辞。
- `test_capsule_golden.py`：A3 的 `MemoryEvent(type=T.TASK_COMPACT_SUMMARY, …)`（L235-239）补 `role="assistant"`；H4 的 L607 `role="user"` → `role="assistant"`，注释改「apply_compact 存 role=assistant」。
- `test_root_self_experience.py`：L89、L109、L148、L149 `role="user"` → `role="assistant"`；L6 docstring「（覆盖存储的 role=user）」改「（继承存储 role=assistant）」。
- `test_dispatch_fold_golden.py`：L53-54 的 `role="user"` → `role="assistant"`。

- [ ] **Step 3: 给 background_observe 端到端测试加 role 断言**（test_background_observe.py，在 L39 `assert MT.TASK_COMPACT_SUMMARY in types` 之后插入）

```python
    summary = next(r for r in recs if r.type == MT.TASK_COMPACT_SUMMARY)
    assert summary.role == "assistant", "后台 observe 真实 apply_compact 应产 assistant 段摘要"
```

- [ ] **Step 4: 跑全部 capsule / 段摘要相关测试确认通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_capsule_golden.py tests/unit/test_capsule_interleaved.py tests/unit/test_root_self_experience.py tests/unit/test_dispatch_fold_golden.py tests/unit/test_background_observe.py tests/unit/test_capsule_render.py -v`
Expected: PASS（A1/A3/A4/H4、interleaved role 映射、root self、dispatch fold、background observe role 断言全绿）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/loop/steps/finalize.py tests/unit/test_capsule_interleaved.py tests/unit/test_capsule_golden.py tests/unit/test_root_self_experience.py tests/unit/test_dispatch_fold_golden.py tests/unit/test_background_observe.py
git commit -m "refactor(capsule): close 删 user→assistant 覆盖特判，继承存储 role；同步 golden setup"
```

---

### Task 5: 守护不变量 + 全量回归（inherit 透传 / 首条 user / composer 去重）

**Files:**
- Test: `tests/unit/test_inherit_memory_snapshot.py`（加 inherit 透传 assistant 断言）
- Test（确认既有绿）：`tests/unit/test_retry_progress_placement.py`、`tests/unit/test_compact_summary_wrap.py`

**Interfaces:**
- Consumes: `_copy_memory_for_inherit(parent_task, child_task, sub_agent, mem, session_id, tenant_id)`（runtime.py，已存在）；recall types 含 `TASK_COMPACT_SUMMARY`。
- Produces: inherit 后 child scope 的段摘要回合 role=assistant 且**无** tool_calls（不误加）；段摘要恒非首条（R1 守护）。

- [ ] **Step 1: 写 inherit 透传断言**（追加到 `test_inherit_memory_snapshot.py` 末尾）

```python
async def test_inherit_preserves_assistant_segment_summary() -> None:
    """parent 段摘要（role=assistant）inherit 后透传为 child 的 assistant 回合，
    且不误加 tool_calls；user 锚点恒在其前（段摘要非首条，R1 守护）。"""
    mem = InMemoryMemoryProvider()
    parent_scope = MemoryScope(session_id="s1", task_id="p1", agent_id="ag1")
    await mem.ingest(_ev(T.USER_PROMPT, parent_scope, "原始诉求", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, parent_scope, "段①摘要", 1, role="assistant"), _ctx())

    parent_task = Task(id="p1", session_id="s1", status="SUSPENDED", assigned_agent_id="ag1",
                       creator_agent_id="ag1", title="P", settings=NormalTaskSettings())
    child_task = Task(id="c1", session_id="s1", status="PENDING", assigned_agent_id="ag2",
                      creator_agent_id="ag1", parent_task_id="p1", title="C",
                      settings=NormalTaskSettings(inherit_memory=True))
    sub_agent = Agent(id="ag2", session_id="s1", template_id="t", template_version="1",
                      status="IDLE", parent_agent_id="ag1")

    await _copy_memory_for_inherit(parent_task, child_task, sub_agent, mem, "s1", "default")

    child_scope = MemoryScope(session_id="s1", task_id="c1", agent_id="ag2")
    turns = list(reversed(await mem.recall_recent(child_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())))
    summary = next(t for t in turns if t.content == "段①摘要")
    assert summary.role == "assistant"
    assert not summary.metadata.get("tool_calls"), "段摘要无 tool_calls，不应误加"
    # 段摘要非首条：其前有 user 锚点
    assert turns[0].role == "user" and turns[0].content == "原始诉求"
```

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `cd ctx-weft && uv run pytest tests/unit/test_inherit_memory_snapshot.py -v`
Expected: PASS（`_copy_memory_for_inherit` 现状 `role=r.role` 透传 + L109 仅 assistant+tool_calls 才加 tool_calls → 段摘要无 tool_calls，断言成立；若失败说明透传逻辑回归，需排查 runtime.py:107-123）。

- [ ] **Step 3: 确认 composer 去重（R2）与渲染回归绿**

Run: `cd ctx-weft && uv run pytest tests/unit/test_retry_progress_placement.py tests/unit/test_compact_summary_wrap.py -v`
Expected: PASS。`test_progress_deduped_when_in_task_compact_summary` 与 `test_progress_rendered_when_compact_summary_differs` 均绿（去重生效、不误删）。

- [ ] **Step 4: 全量回归（core + host）**

Run: `cd ctx-weft && uv run pytest -q`
Then: `uv run pytest -q`（仓根，跑 host 测试含 postgres provider）
Expected: 两侧全绿，无因 role 变更引发的回归。

- [ ] **Step 5: 提交**

```bash
git add tests/unit/test_inherit_memory_snapshot.py
git commit -m "test(memory): 守护 inherit 透传 assistant 段摘要 + 非首条不变量"
```

---

## 自检（writing-plans self-review）

- **Spec coverage**：§3.1 写入层→Task 1（in-memory）+ Task 2（postgres）；§3.2 渲染层→Task 3；§3.3 close 层→Task 4；§4 R1 守护→Task 5 Step 1；§4 R2 composer→Task 5 Step 3；§4 R3 旧数据防御→Task 3（role==user 仍包装，含既有 user 用例）；§5 测试 T1–T8 + F3 反转→分布于 Task 1/2（T1/T2）、Task 3（T3，F3 反转 = 新增 assistant 用例 + 保留 user 用例）、Task 4（T4/T5/T8 端到端）、Task 5（T6/T7/T8）。
- **B 不动**覆盖：Task 1/2 的 `*_agent_summary_role_stays_user` + Task 3 不碰 agent_experience/agent_recall。
- **Placeholder scan**：无 TBD/TODO；每个 code step 含完整 before/after 代码。
- **Type consistency**：`apply_compact(... layer, protect_types)` 签名、`record_to_history_block`、`_copy_memory_for_inherit`、`MemoryLayer.TASK/AGENT`、`MemoryEventType.TASK_COMPACT_SUMMARY/AGENT_COMPACT_SUMMARY` 全程一致。
