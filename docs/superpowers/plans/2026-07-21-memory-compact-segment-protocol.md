# memory 协议 apply_compact 段作用域正式化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `since_last` 段作用域 + 渲染序排序正式化为 memory 协议契约，迁移应用侧 postgres provider，恢复 IpMasterCoworkPy 的段折叠。

**Architecture:** ctx-weft 侧只补两块——协议 docstring 的排序契约、bg observe 对契约错误（TypeError）的 ERROR 日志降级（段保 raw、不抛）。应用侧 `postgres.py` 镜像 in_memory 的修复（签名 + 段界 + (timestamp, seq_no) 渲染序），TDD。最后 revendor wheel、真实场景复验。

**Tech Stack:** Python 3.11+ / pytest(asyncio) / SQLAlchemy(aiosqlite) / uv / PowerShell（revendor 脚本）

**Spec:** `docs/superpowers/specs/2026-07-21-memory-compact-segment-protocol-design.md`

## Global Constraints

- ctx-weft 版本号**不变**（不 bump，兼容靠 wheel 锁版本）。
- 不加启动探测 / 能力 flag / 双轨回退。
- 契约错误处理 = `logger.error`（显式指出 provider 协议不匹配）+ 段保 raw 降级，**不抛出**。
- 排序契约：apply_compact 的段界搜索、归档池切分、锚点判定一律按渲染序 `(timestamp, seq_no)`，禁止裸 seq_no。
- `since_last=None`（缺省）= 整 scope 折叠，旧语义保留；不改 `escalating_compact` 等其他调用方。
- ctx-weft 仓两分支（master / feat/capabilities-in-task-turn）同步携带全部提交。
- 仓库路径：ctx-weft = `C:\Users\Xing\Documents\codes\Loome-02\ctx-weft`；应用 = `C:\Users\Xing\Documents\codes\IpMasterCoworkPy`。

---

### Task 1: ctx-weft — bg observe 契约错误 ERROR 降级

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（`_run_background_observe` 的异常处理，当前约 293-302 行 `except Exception` 处）
- Test: `tests/unit/test_background_observe.py`（文件末尾追加）

**Interfaces:**
- Consumes: `_run_background_observe` 现有 try/except 结构；`pop_close_synth`；`_CLOSE_BOUNDARIES`。
- Produces: 新增 `except TypeError` 分支——ERROR 日志含 `apply_compact` 与 `协议` 字样；行为与运行时故障降级一致（段保 raw、close 边界弹 synth 登记、不抛）。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_background_observe.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_apply_compact_typeerror_logs_error_and_keeps_raw(
        monkeypatch, caplog, fake_state_ctx):
    """契约错误（provider 缺 since_last → TypeError）：ERROR 日志显式指出协议不匹配，
    段保 raw 降级、不抛——与运行时故障同降级，但第一次折叠即可从日志发现（spec 2026-07-21）。"""
    import logging

    state, ctx = fake_state_ctx  # task 层预置 [UP, LLM, TOOL]
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    async def legacy_apply_compact(*args, **kwargs):
        raise TypeError("apply_compact() got an unexpected keyword argument 'since_last'")

    monkeypatch.setattr(ctx.memory, "apply_compact", legacy_apply_compact)

    with caplog.at_level(logging.ERROR, logger="ctx_weft.core.loop.steps.background_observe"):
        t = bo.launch_background_observe(state, ctx, boundary="plain_text")
        await t

    assert t.exception() is None, "契约错误不得抛出（fire-and-forget 降级）"
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == [], "契约错误不得写摘要"
    raw = await ctx.memory.recall_recent(
        state.scope, [MT.LLM_RESPONSE], 100, ctx.provider_ctx)
    assert raw, "段必须保 raw"
    err_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("apply_compact" in m and "协议" in m for m in err_msgs), \
        f"ERROR 日志须显式指出 apply_compact 协议不匹配，实得: {err_msgs}"
```

- [ ] **Step 2: 跑测试确认失败**

```
cd C:\Users\Xing\Documents\codes\Loome-02\ctx-weft
python -m pytest tests/unit/test_background_observe.py::test_apply_compact_typeerror_logs_error_and_keeps_raw -q
```

预期 FAIL：`assert any("apply_compact" in m and "协议" in m ...)` 断言失败（当前 TypeError 落进 `except Exception`，走 `logger.exception("background observe failed (ignored)...")`，消息不含「协议」）。

- [ ] **Step 3: 实现最小修改**

`src/ctx_weft/core/loop/steps/background_observe.py`，在既有 `except Exception:` 之前插入 `except TypeError:` 分支（两分支体结构相同，仅日志不同）：

```python
            except TypeError:
                # 契约错误（典型：provider 的 apply_compact 缺 since_last 参数/签名过旧）。
                # 与运行时故障同降级（段保 raw、不抛），但 ERROR 显式指出协议不匹配——
                # 静默吞掉曾让 provider 不兼容运行数日无人察觉（spec 2026-07-21）。
                if boundary in _CLOSE_BOUNDARIES:
                    pop_close_synth(state.task.id)
                logger.error(
                    "background observe contract error: apply_compact 协议不匹配"
                    "（provider 缺 since_last 参数或签名过旧？见 spec 2026-07-21）; "
                    "segment kept raw (task=%s boundary=%s)",
                    state.task.id, boundary, exc_info=True,
                )
```

- [ ] **Step 4: 跑测试确认通过 + 无回归**

```
python -m pytest tests/unit/test_background_observe.py -q
python -m pytest tests/unit/test_segment_scoped_fold.py -q
```

预期全 PASS（既有 12 + 新 1 + 9）。

- [ ] **Step 5: 提交**

```
git add tests/unit/test_background_observe.py src/ctx_weft/core/loop/steps/background_observe.py
git commit -m "fix(loop): bg observe 契约错误 ERROR 降级——TypeError 不再与运行时故障混同静默

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: ctx-weft — 协议排序契约写进 protocols/memory.py

**Files:**
- Modify: `src/ctx_weft/protocols/memory.py`（`apply_compact` 抽象方法 docstring，约 316-337 行）

**Interfaces:**
- Consumes: 现有 docstring（已含 since_last 语义段）。
- Produces: docstring 增补排序契约 + 锚点语义两条，成为所有 provider 实现方的规范文本。

- [ ] **Step 1: 编辑 docstring**

在 `apply_compact` docstring 的 since_last 段之后追加（保持原文其余不动）：

```python
        排序契约（2026-07-21，实现方必须遵守）：段界搜索、归档池切分、锚点判定一律按
        **渲染序 (timestamp, seq_no)**，与 recall 的 timestamp 序一致。不得用裸 seq_no——
        存在 timestamp 回填、seq 更高的合法记录（L3 坍缩 UP，见 collapse_task_layer），
        seq 序会把段界推到所有 raw 之后（摘要照写、raw 不折）。

        锚点语义：摘要落「被折区起点之后第一条幸存事件之前」；段尾无幸存者则锚到被折段
        末条事件位置（不用 now()，防迟到摘要越过新 USER_PROMPT）。
```

- [ ] **Step 2: 语法自检 + 提交**

```
python -c "import ctx_weft.protocols.memory"
git add src/ctx_weft/protocols/memory.py
git commit -m "docs(protocols): apply_compact 排序契约与锚点语义正式化——渲染序 (timestamp, seq_no)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: ctx-weft — 双分支同步

**Files:** 无新改动（git 操作）。

**Interfaces:**
- Consumes: Task 1、Task 2 在当前分支（feat/capabilities-in-task-turn）产生的两个提交 + 此前的 spec/plan docs 提交。
- Produces: master 与 feat 均含全部提交。

- [ ] **Step 1: 确认当前分支与待同步提交**

```
git branch --show-current
git log --oneline master..feat/capabilities-in-task-turn
```

预期：当前在 `feat/capabilities-in-task-turn`；差集顶端为 Task 1/Task 2/spec/plan 的提交（记下这几个 hash）。

- [ ] **Step 2: cherry-pick 到 master**

```
git checkout master
git cherry-pick <spec提交> <plan提交> <Task1提交> <Task2提交>
git checkout feat/capabilities-in-task-turn
```

注意：只挑本轮新增提交，**不要**把 feat 的 capabilities 提交带进 master；若冲突（test 文件 add/add），以 feat 版本为准解决后 `git cherry-pick --continue`。

- [ ] **Step 3: master 上验证**

```
git checkout master
python -m pytest tests/unit/test_background_observe.py tests/unit/test_segment_scoped_fold.py -q
git checkout feat/capabilities-in-task-turn
```

预期全 PASS。

---

### Task 4: IpMasterCoworkPy — postgres provider 迁移（TDD）

**Files:**
- Create: `tests/unit/test_postgres_compact_segment_scope.py`
- Modify: `src/ipmastercowork/providers/memory/postgres.py:291-392`（`apply_compact`）

**Interfaces:**
- Consumes: `PostgresMemoryProvider(factory)` + `init_db("sqlite:///…")` 测试先例（见 `tests/unit/test_postgres_compact_user_aware.py`）；`MemoryEventModel`（列：id/type(str)/timestamp/seq_no/is_superseded…）；既有 `protect_strs = [str(t) for t in protect_types]` 的类型字符串化惯例。
- Produces: `apply_compact(..., since_last: MemoryEventType | None = None)`——段作用域 + 渲染序，与 ctx-weft `InMemoryMemoryProvider` 行为同构。既有调用（不传 since_last）行为不变。

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_postgres_compact_segment_scope.py`：

```python
"""PostgresMemoryProvider.apply_compact 段作用域（since_last）+ 渲染序排序 —
aiosqlite 行为测试，镜像 ctx-weft tests/unit/test_segment_scoped_fold.py 的 provider 层契约
（spec 2026-07-21-memory-compact-segment-protocol）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryLayer, MemoryScope, ProviderContext
from ipmastercowork.persistence.postgres import init_db
from ipmastercowork.providers.memory.postgres import PostgresMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
BASE = datetime(2026, 7, 21, 10, 0, 0, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


async def _provider(tmp_path) -> PostgresMemoryProvider:
    factory = await init_db(f"sqlite:///{(tmp_path / 'mem.db').as_posix()}")
    return PostgresMemoryProvider(factory)


def _sc() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="task1", agent_id="a1")


async def _chrono(p, sc, ctx):
    return list(reversed(await p.recall_recent(
        sc, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT, T.TASK_COMPACT_SUMMARY],
        100, ctx)))


async def test_pg_since_last_folds_only_current_segment(tmp_path) -> None:
    """[UP1, A1(免折残留), UP2, A2a, A2b] + since_last=UP → 只折 A2；A1 保 raw；
    摘要锚在 UP2 之后。"""
    p, ctx, sc = await _provider(tmp_path), _ctx(), _sc()

    async def ing(typ, c, sec, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c,
                                   timestamp=BASE + timedelta(seconds=sec), role=role), ctx)

    await ing(T.USER_PROMPT,  "UP1 第一问", 0, "user")
    await ing(T.LLM_RESPONSE, "A1 段一回答", 1, "assistant")
    await ing(T.USER_PROMPT,  "UP2 第二问", 2, "user")
    await ing(T.LLM_RESPONSE, "A2a 当前段", 3, "assistant")
    await ing(T.TOOL_RESULT,  "A2b 工具",   4, "tool")

    await p.apply_compact(
        scope=sc, summary="S2", keep_last=0, ctx=ctx, layer=MemoryLayer.TASK,
        protect_types=(T.USER_PROMPT, T.TASK_COMPACT_SUMMARY),
        since_last=T.USER_PROMPT,
    )

    contents = [r.content for r in await _chrono(p, sc, ctx)]
    assert contents == ["UP1 第一问", "A1 段一回答", "UP2 第二问", "S2"], contents


async def test_pg_since_last_after_collapsed_up_still_folds(tmp_path) -> None:
    """坍缩 UP 形态（timestamp 回填、seq 最高）在场：段界必须按渲染序判定，
    坍缩 UP 之后的 raw 照折——seq 序实现会归档池空、摘要照写 raw 不折（今日实证 bug）。"""
    p, ctx, sc = await _provider(tmp_path), _ctx(), _sc()

    async def ing(typ, c, sec, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c,
                                   timestamp=BASE + timedelta(seconds=sec), role=role), ctx)

    await ing(T.USER_PROMPT,  "UP1", 0, "user")
    await ing(T.LLM_RESPONSE, "A1",  1, "assistant")
    await ing(T.LLM_RESPONSE, "A2",  2, "assistant")
    await ing(T.LLM_RESPONSE, "A3",  3, "assistant")
    # 坍缩 UP：ts 回填到 A2 之前 1.5s 处，但最后 ingest（seq 最高）——L3 collapse_task_layer 形态
    await p.ingest(MemoryEvent(
        type=T.USER_PROMPT, scope=sc, content="坍缩UP",
        timestamp=BASE + timedelta(seconds=1, microseconds=500_000), role="user",
        metadata={"collapsed": True}), ctx)

    await p.apply_compact(
        scope=sc, summary="S", keep_last=0, ctx=ctx, layer=MemoryLayer.TASK,
        protect_types=(T.USER_PROMPT, T.TASK_COMPACT_SUMMARY),
        since_last=T.USER_PROMPT,
    )

    chrono = await _chrono(p, sc, ctx)
    contents = [r.content for r in chrono]
    assert "A2" not in contents and "A3" not in contents, \
        f"坍缩 UP 之后的当前段 raw 必须被折: {contents}"
    s_idx = next(i for i, r in enumerate(chrono) if r.type == T.TASK_COMPACT_SUMMARY)
    cup_idx = contents.index("坍缩UP")
    assert s_idx > cup_idx, f"摘要必须锚在坍缩 UP 之后: {contents}"


async def test_pg_without_since_last_folds_whole_scope(tmp_path) -> None:
    """回归：不传 since_last（既有调用方）→ 整 scope 归档池，旧语义不变。"""
    p, ctx, sc = await _provider(tmp_path), _ctx(), _sc()

    async def ing(typ, c, sec, role):
        await p.ingest(MemoryEvent(type=typ, scope=sc, content=c,
                                   timestamp=BASE + timedelta(seconds=sec), role=role), ctx)

    await ing(T.USER_PROMPT,  "UP1", 0, "user")
    await ing(T.LLM_RESPONSE, "A1",  1, "assistant")
    await ing(T.USER_PROMPT,  "UP2", 2, "user")
    await ing(T.LLM_RESPONSE, "A2",  3, "assistant")

    await p.apply_compact(
        scope=sc, summary="S", keep_last=0, ctx=ctx, layer=MemoryLayer.TASK,
        protect_types=(T.USER_PROMPT, T.TASK_COMPACT_SUMMARY),
    )

    contents = [r.content for r in await _chrono(p, sc, ctx)]
    assert "A1" not in contents and "A2" not in contents
    assert contents == ["UP1", "S", "UP2"], contents
```

- [ ] **Step 2: 跑测试确认失败**

```
cd C:\Users\Xing\Documents\codes\IpMasterCoworkPy
uv run pytest tests/unit/test_postgres_compact_segment_scope.py -q
```

预期：前两个 FAIL——`TypeError: apply_compact() got an unexpected keyword argument 'since_last'`；第三个（回归基线）PASS。

- [ ] **Step 3: 实现 postgres.py 迁移**

`src/ipmastercowork/providers/memory/postgres.py` 的 `apply_compact` 改为（镜像 ctx-weft in_memory；只展示改动区，函数其余部分不动）：

```python
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
        protect_types: tuple[MemoryEventType, ...] = (),
        since_last: MemoryEventType | None = None,
    ) -> CompactResult:
```

查询排序改渲染序（协议 2026-07-21：不得裸 seq_no——L3 坍缩 UP 是 ts 回填、seq 最高的合法记录）：

```python
                all_result = await db.execute(
                    select(MemoryEventModel.id, MemoryEventModel.seq_no,
                           MemoryEventModel.timestamp, MemoryEventModel.type)
                    .where(scope_where, MemoryEventModel.is_superseded == False)
                    .order_by(MemoryEventModel.timestamp.asc(), MemoryEventModel.seq_no.asc())
                )
                all_rows = all_result.all()
                # since_last 段作用域：归档池限定在最后一条该类型记录（渲染序）之后；
                # 该类型不存在 → 整 scope（旧语义）。
                pool = all_rows
                if since_last is not None:
                    since_str = str(since_last)
                    b_idx = next(
                        (i for i in range(len(all_rows) - 1, -1, -1)
                         if all_rows[i].type == since_str),
                        None,
                    )
                    if b_idx is not None:
                        pool = all_rows[b_idx + 1:]
                archivable = [r for r in pool if r.type not in protect_strs]
                to_archive = archivable[:-keep_last] if keep_last > 0 else archivable
                to_archive_ids = [r.id for r in to_archive]
```

锚点判定的比较键同步改渲染序（替换 `archived_min_seq` 一段）：

```python
                # 摘要锚点：被折区起点（渲染序）之后第一条幸存事件之前（协议锚点语义）
                archived_min_key = min(
                    ((r.timestamp, r.seq_no) for r in to_archive), default=None)
                summary_id = generate_id("mev")
                to_archive_id_set = set(to_archive_ids)
                if archived_min_key is not None:
                    following = [r for r in all_rows
                                 if (r.timestamp, r.seq_no) >= archived_min_key
                                 and r.id not in to_archive_id_set]
                else:
                    following = []
```

（`if following: anchor = min(following, key=lambda r: (r.timestamp, r.seq_no))` 及 tail/else 分支已是 (ts,seq) 键，保持不动。）

- [ ] **Step 4: 跑测试确认通过 + provider 全量回归**

```
uv run pytest tests/unit/test_postgres_compact_segment_scope.py -q
uv run pytest tests/unit/test_postgres_compact_user_aware.py tests/unit/test_postgres_compact_trailing_anchor.py tests/unit/test_postgres_memory_supersede.py tests/unit/test_postgres_recall_by_agent.py -q
```

预期全 PASS（既有测试不传 since_last，整 scope 旧语义不受影响；`test_postgres_compact_trailing_anchor` 若因排序改渲染序而失败，先读该测试确认其种子数据里 ts 序与 seq 序是否一致——一致则是真回归须排查，不一致（刻意乱序种子）则按协议更新其断言并在提交信息里注明）。

- [ ] **Step 5: 提交（应用仓）**

```
git add tests/unit/test_postgres_compact_segment_scope.py src/ipmastercowork/providers/memory/postgres.py
git commit -m "fix(memory): postgres apply_compact 迁移段作用域协议——since_last + 渲染序 (timestamp, seq_no)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: revendor wheel + 真实场景复验

**Files:**
- Modify: `vendor/ctx_weft-*.whl`、`uv.lock`（revendor 脚本产物，应用仓）

**Interfaces:**
- Consumes: Task 3 完成后的 ctx-weft master；应用仓 `scripts/revendor-core.ps1`（默认 CoreRepo = `..\Loome-02\ctx-weft` 的**当前 checkout**）。
- Produces: 应用 venv 里的 ctx_weft 与 provider 协议一致；真实 DB 中段折叠恢复工作。

- [ ] **Step 1: ctx-weft 切到 master（wheel 从当前 checkout 构建）**

```
cd C:\Users\Xing\Documents\codes\Loome-02\ctx-weft
git checkout master
```

- [ ] **Step 2: revendor**

```
cd C:\Users\Xing\Documents\codes\IpMasterCoworkPy
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\revendor-core.ps1
```

预期：脚本五步全 [OK]，`import ctx_weft -> …\.venv\…`。

- [ ] **Step 3: 验证装进 venv 的代码带契约**

```
uv run python -c "import inspect; from ctx_weft.protocols.memory import MemoryProvider; print('since_last' in inspect.signature(MemoryProvider.apply_compact).parameters)"
```

预期输出 `True`。

- [ ] **Step 4: 应用仓测试全量**

```
uv run pytest tests/unit -q
```

预期：无新增失败（与迁移前基线对比）。

- [ ] **Step 5: 真实场景复验（半手动）**

启动应用，跑一轮「多轮对话」：问候 → 让它做一件会产生多条工具调用的事（如「查看你的工作区」）→ 再发一条消息（触发 plain_text 边界折叠）→ 结束。然后查 DB（把 `<TASK_ID>` 换成本轮 task）：

```
uv run python -c "import sqlite3; con=sqlite3.connect(r'data\ipmc-dev.db'); con.row_factory=sqlite3.Row; rows=con.execute('SELECT seq_no,type,is_superseded,substr(content,1,40) c FROM memory_events WHERE task_id=? ORDER BY timestamp,seq_no',('<TASK_ID>',)).fetchall(); [print(dict(r)) for r in rows]"
```

验收断言（对照 spec §验证）：
1. 存在 `task_compact_summary` 记录；
2. 该摘要对应段的 `llm_response`/`tool_invocation`/`tool_result` 均 `is_superseded=1`；
3. 渲染序里摘要落在其所属 USER_PROMPT **之后**（不在前一条 UP 之前）；
4. 应用日志无 `contract error` ERROR。

- [ ] **Step 6: 提交 revendor 产物（应用仓）**

```
git add vendor uv.lock pyproject.toml
git commit -m "chore(vendor): revendor ctx-weft——段作用域折叠协议 + 契约错误 ERROR 降级

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
