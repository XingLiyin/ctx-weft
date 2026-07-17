# dispatch 段边界 recap 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 委派父派发 sub task 转 SUSPENDED 时，后台把派发前的 task 层 raw 折成段摘要；父 resume 前强一致等待折叠完成。

**Architecture:** 复用现有 boundary 分流基建——`SuspendStep` 末尾 fire-and-forget `launch_background_observe(boundary="dispatch")`（非 close 分支自动走 `apply_compact`），`_run_loop` 入口统一 `await_pending_background_observe` 封死 resume 竞态。无新增状态、无新增配置。

**Tech Stack:** Python 3.11 / pytest + pytest-asyncio（unit 层 `asyncio_mode=auto`，测试文件用 `pytestmark = pytest.mark.asyncio`）/ InMemoryMemoryProvider / MockLLMAdapter。

**Spec:** `docs/superpowers/specs/2026-07-16-dispatch-boundary-recap-design.md`（三节均经用户确认，实现遇歧义以 spec 为准）。

## Global Constraints

- 仓库根：`C:\Users\Xing\Documents\codes\Loome-02\ctx-weft`，所有命令在根目录跑。
- 测试命令形态：`python -m pytest tests/unit/test_x.py -v`（Windows，PowerShell 或 Git Bash 均可）。
- 注释/docstring 用中文、跟随各文件既有密度与口吻；commit message 用中文 conventional commits（见各 task 给定文案），结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 不改任何公共接口签名；不新增 loop_config 配置项。
- `"dispatch"` 是新 boundary 字符串值，**不得**加入 `background_observe._CLOSE_BOUNDARIES`。

---

### Task 1: SuspendStep 触发 dispatch 边界后台 recap

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/suspend.py`（`execute` 末尾，`return StepOutcome` 之前）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:6-10`（模块 docstring 的 boundary 分流清单补 dispatch）
- Test: `tests/unit/test_background_observe_wiring.py`

**Interfaces:**
- Consumes: `background_observe.launch_background_observe(state, ctx, *, boundary: str) -> asyncio.Task`（已存在）
- Produces: SuspendStep 在每次委派挂起时以 `boundary="dispatch"` 触发一次后台 recap（root 与非 root 一致）。Task 3/6 依赖此行为。

- [ ] **Step 1: 写两个失败的 wiring 测试**

在 `tests/unit/test_background_observe_wiring.py` 末尾追加（文件顶部 import 区补一行 `from ctx_weft.core.loop.steps.suspend import SuspendStep`）：

```python
# ── suspend.py: dispatch 边界（spec 2026-07-16）对所有委派父生效 ──────────────


def _make_suspend_state_ctx(task: Task):
    """Minimal LoopState + LoopContext for SuspendStep.execute()."""
    mem = InMemoryMemoryProvider()
    scope = MemoryScope(session_id="s1", task_id=task.id, agent_id="ag1")
    pctx = ProviderContext(session_id="s1", tenant_id="default", task_id=task.id, agent_id="ag1")
    session = Session(id="s1", tenant_id="default", user_prompt="hello", status="RUNNING")
    agent = SimpleNamespace(id="ag1")

    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope,
        extra={"template": None},
    )

    class _FakeEventBus:
        async def emit(self, event: Any) -> None:
            pass

    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=_FakeEventBus(),
        provider_ctx=pctx,
    )
    return state, ctx


async def test_suspend_step_fires_dispatch_boundary_for_root(monkeypatch):
    """root task 委派挂起 → launch_background_observe(boundary='dispatch') 一次。"""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch, raising=False,
    )

    task = _make_root_task(status="SUSPENDED")
    state, ctx = _make_suspend_state_ctx(task)

    await SuspendStep().execute(state, ctx)

    assert launched == [("t1", "dispatch")], f"Expected one dispatch launch, got {launched}"


async def test_suspend_step_fires_dispatch_boundary_for_child(monkeypatch):
    """非 root 委派父同样触发（spec：不加 _is_own_root 门控）。"""
    launched = []

    def fake_launch(state, ctx, *, boundary=""):
        launched.append((state.task.id, boundary))
        return asyncio.ensure_future(asyncio.sleep(0))

    import ctx_weft.core.loop.steps.background_observe as bo_mod
    monkeypatch.setattr(bo_mod, "_task_pending", {})
    monkeypatch.setattr(
        "ctx_weft.core.loop.steps.background_observe.launch_background_observe",
        fake_launch, raising=False,
    )

    child = _make_child_task(status="SUSPENDED")
    state, ctx = _make_suspend_state_ctx(child)

    await SuspendStep().execute(state, ctx)

    assert launched == [("t2", "dispatch")], f"Expected one dispatch launch, got {launched}"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_background_observe_wiring.py -v -k dispatch`
Expected: 两个新用例 FAIL（`assert launched == [...]` 得到 `[]`——SuspendStep 尚未触发）。既有用例不受影响。

- [ ] **Step 3: 实现——SuspendStep 末尾触发**

`src/ctx_weft/core/loop/steps/suspend.py`，在 `events.append(make_event(...TASK_SUSPENDED...))` 之后、`return StepOutcome(...)` 之前插入：

```python
        # dispatch 段边界（spec 2026-07-16）：父坐实 SUSPENDED 后 fire-and-forget 后台
        # recap，折派发前 raw——挂起空窗跑 LLM。所有委派父生效（不加 _is_own_root 门控）；
        # resume 竞态由 _run_loop 入口 await_pending_background_observe 封死。
        from ctx_weft.core.loop.steps.background_observe import launch_background_observe
        launch_background_observe(state, ctx, boundary="dispatch")
```

同时更新 `src/ctx_weft/core/loop/steps/background_observe.py` 模块 docstring 的 boundary 分流段（第 6-10 行），把第 8 行改为：

```
  - 其他（interrupt、plain_text、dispatch 等）→ apply_compact 写 TASK_COMPACT_SUMMARY；
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_background_observe_wiring.py -v`
Expected: 全部 PASS（含既有 7 个用例——它们 patch 的是 bo 模块命名空间里的 `launch_background_observe`，suspend.py 的局部 import 同样命中）。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/suspend.py src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_background_observe_wiring.py
git commit -m "feat(suspend): dispatch 段边界——委派父挂起时后台折派发前 raw

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: dispatch 边界非 close 分支契约特征测试

无源码改动——`boundary="dispatch" ∉ _CLOSE_BOUNDARIES` 时非 close 分支的行为是既有代码自然给出的，本 task 用特征测试把契约锁死（防未来把 dispatch 误加进 close 集合或改 protect_types）。**预期直接 PASS**，不走 fail-first。

**Files:**
- Test: `tests/unit/test_background_observe.py`（文件末尾追加；复用文件内既有 `_FakeGateway`、`_fake_stream_collect_process_report` 与 conftest 的 `fake_state_ctx` fixture——task 层预置 `[USER_PROMPT, LLM_RESPONSE, TOOL_RESULT]`）

**Interfaces:**
- Consumes: `bo.launch_background_observe`、conftest `fake_state_ctx`
- Produces: 契约断言——dispatch 边界写 `TASK_COMPACT_SUMMARY`、折掉 `OBSERVER_SUMMARY`（挂起摘要不特护）、短段免折门与幂等护栏生效。

- [ ] **Step 1: 追加三个特征测试**

```python
# ── dispatch 边界（spec 2026-07-16）：非 close 分支契约特征测试 ────────────────


@pytest.mark.asyncio
async def test_dispatch_boundary_folds_segment_and_observer_summary(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：走非 close 分支写段摘要；SuspendStep 的挂起摘要
    （OBSERVER_SUMMARY）随段折叠、不特护（spec §1）。UP 保留。"""
    from datetime import UTC, datetime

    from ctx_weft.protocols import MemoryEvent
    from ctx_weft.protocols import MemoryEventType as MT

    state, ctx = fake_state_ctx  # task 层预置 [UP, LLM, TOOL]
    ctx.capability_gateway = _FakeGateway("dispatch段摘要")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    # 模拟 SuspendStep 已写的挂起摘要
    await ctx.memory.ingest(MemoryEvent(
        type=MT.OBSERVER_SUMMARY, scope=state.scope,
        content="Delegated to sub-task(s): 'x'. Awaiting completion.",
        timestamp=datetime.now(UTC), role="assistant",
        metadata={"task_id": state.task.id, "outcome": "suspended"}), ctx.provider_ctx)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    recs = await ctx.memory.recall_recent(
        state.scope,
        [MT.USER_PROMPT, MT.LLM_RESPONSE, MT.TOOL_RESULT, MT.OBSERVER_SUMMARY,
         MT.TASK_COMPACT_SUMMARY],
        100, ctx.provider_ctx)
    types = {r.type for r in recs}
    assert MT.TASK_COMPACT_SUMMARY in types, "dispatch 边界必须写段摘要"
    assert MT.USER_PROMPT in types, "UP 受 protect_types 保护"
    assert MT.LLM_RESPONSE not in types, "派发前 raw 必须折掉"
    assert MT.OBSERVER_SUMMARY not in types, "挂起摘要随段折叠、不特护（spec §1）"


@pytest.mark.asyncio
async def test_dispatch_boundary_short_segment_kept_raw(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：段 raw 低于 short_segment_token_threshold → 免折保 raw。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(
        compact_keep_last=2, max_turns_per_observe=3,
        short_segment_token_threshold=100_000)  # 远超预置 raw → 门必命中
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.LLM_RESPONSE, MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    types = [r.type for r in recs]
    assert MT.TASK_COMPACT_SUMMARY not in types, "短段必须免折"
    assert MT.LLM_RESPONSE in types, "raw 必须保留"


@pytest.mark.asyncio
async def test_dispatch_boundary_refold_guard_skips(monkeypatch, fake_state_ctx):
    """boundary="dispatch"：段内无 active LLM_RESPONSE（恢复重跑已折过）→ 幂等护栏跳过。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("unused")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def zero_count(scope, types, pctx):
        return 0

    monkeypatch.setattr(ctx.memory, "count_recent", zero_count)
    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream_collect_process_report)

    await bo.launch_background_observe(state, ctx, boundary="dispatch")

    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert recs == [], "护栏命中不得产冗余胶囊"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_background_observe.py -v -k dispatch`
Expected: 3 PASS（特征测试，行为已由既有非 close 分支给出）。若 FAIL，说明对分支行为的理解有误——回读 spec §1 与 `_run_background_observe`，先修测试认知再动源码。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_background_observe.py
git commit -m "test(background_observe): dispatch 边界非 close 分支契约特征测试

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: run 启动前等在途段 recap（resume 竞态封死）

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:41`（import 行）与 `_run_loop`（约 :1748，方法体最顶部）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py:12-17`（模块 docstring 竞态段改写）
- Test: 新建 `tests/unit/test_run_start_awaits_recap.py`

**Interfaces:**
- Consumes: `background_observe.await_pending_background_observe(task_id: str) -> None`（已存在，内部 `asyncio.shield`）
- Produces: 任何 task run（初始步 prepare 或 reconcile，新跑/resume/恢复重排一律）开跑前先等本 task 在途 recap；无 pending 零开销直通。Task 6 的 e2e 依赖此保证。

- [ ] **Step 1: 写失败测试（新文件）**

```python
"""_run_loop 入口的段 recap 强一致等待（spec 2026-07-16 §2）。

驱动 CtxWeftRuntime._run_loop（unbound + fake self）：driver 首步必须在
本 task 在途后台 recap 完成之后才执行；无 pending 时直通。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import ctx_weft.core.loop.steps.background_observe as bo
from ctx_weft.core.loop.driver import LoopState
from ctx_weft.core.runtime import CtxWeftRuntime
from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
from ctx_weft.protocols import MemoryScope

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_pending():
    bo._task_pending.clear()
    yield
    bo._task_pending.clear()


class _FakeBus:
    async def emit(self, event) -> None:
        pass


class _RecordingDriver:
    """driver.run 是 async generator：首次迭代时记录时刻，不产出任何 outcome。"""

    def __init__(self, order: list):
        self._order = order

    async def run(self, state, ctx):
        self._order.append("driver_started")
        if False:  # 使函数成为 async generator，且不产出任何 outcome
            yield


def _make_state_and_task():
    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        assigned_agent_id="ag1", creator_agent_id="ag1",
        settings=NormalTaskSettings(),
    )
    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING")
    agent = SimpleNamespace(id="ag1")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    state = LoopState(run_id="r1", session=session, task=task, agent=agent, scope=scope)
    return state, task, agent


def _fake_runtime_self():
    return SimpleNamespace(
        _event_bus=_FakeBus(),
        _capability_cache=SimpleNamespace(evict=lambda agent_id: None),
    )


async def test_run_start_waits_for_pending_recap():
    """有在途 recap：driver 首步必须排在 recap 完成之后。"""
    order: list = []

    async def slow_recap():
        await asyncio.sleep(0.02)
        order.append("recap_done")

    state, task, agent = _make_state_and_task()
    bo._task_pending[task.id] = asyncio.create_task(slow_recap())

    await CtxWeftRuntime._run_loop(
        _fake_runtime_self(), state, None, _RecordingDriver(order),
        "r1", "prepare", task, agent,
    )

    assert order == ["recap_done", "driver_started"], \
        f"run 必须等 recap 折完才开跑，实得 {order}"


async def test_run_start_passthrough_without_pending():
    """无 pending：直通，driver 正常执行。"""
    order: list = []
    state, task, agent = _make_state_and_task()

    await CtxWeftRuntime._run_loop(
        _fake_runtime_self(), state, None, _RecordingDriver(order),
        "r1", "prepare", task, agent,
    )

    assert order == ["driver_started"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_run_start_awaits_recap.py -v`
Expected: `test_run_start_waits_for_pending_recap` FAIL（`order` 为 `["driver_started", "recap_done"]`——尚未等待）；`test_run_start_passthrough_without_pending` PASS（本来就直通）。

- [ ] **Step 3: 实现——`_run_loop` 入口 await + import**

`src/ctx_weft/core/runtime.py:41` 改为：

```python
from ctx_weft.core.loop.steps.background_observe import (
    await_pending_background_observe,
    launch_background_observe,
    register_close_synth,
)
```

`_run_loop` 方法体最顶部（docstring 之后、`RUN_STARTED` emit 之前）插入：

```python
        # 段 recap 强一致（spec 2026-07-16 §2）：本 task 若有在途后台 recap
        # （dispatch/interrupt/plain_text 边界），先等它折完再开跑——run 的一切
        # memory 读写都落在折叠结果之上。无 pending 零开销直通。recap 自吞异常
        # 必正常结束，此处不会抛；shield 保证 run 被取消时不牵连 recap。
        await await_pending_background_observe(task.id)
```

- [ ] **Step 4: 改写 background_observe.py 模块 docstring 竞态段**

把第 12-17 行（"崩溃恢复的已知 best-effort 竞态…无需修复。"整段）替换为：

```
崩溃恢复竞态（spec §5.1/§3.6；2026-07-16 起同进程内闭合）：`recover_session` 对一个
SUSPENDED-且-有待完成段 recap 的 task，会（a）经 TaskManager.restore 重排该 task 的新一轮
run，（b）经 `_relaunch_task_recap` 重跑被打断的段 recap。relaunch 先于 register_and_drain
发生，且 `_run_loop` 入口 await_pending_background_observe——新 run 开跑前必等 recap 完成，
两者不再并发写同一段。跨进程/其他极端时序仍是 best-effort：最坏该段保 raw，不影响正确性。
```

- [ ] **Step 5: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/unit/test_run_start_awaits_recap.py tests/unit/test_background_observe.py tests/unit/test_background_observe_wiring.py -v`
Expected: 全部 PASS。

Run: `python -m pytest tests/integration -x -q`
Expected: 全部 PASS（await 点对所有 run 生效，集成回归确认无死锁/顺序问题）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py src/ctx_weft/core/loop/steps/background_observe.py tests/unit/test_run_start_awaits_recap.py
git commit -m "feat(runtime): run 启动前等在途段 recap——dispatch resume 竞态封死

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 恢复路径 boundary="dispatch" 重跑用例

无源码改动——`_relaunch_task_recap` 对 boundary 是透传的（`runtime.py:1272-1277`：仅 close 边界才 register_close_synth）。特征测试锁契约，**预期直接 PASS**。

**Files:**
- Test: `tests/unit/test_relaunch_task_recap.py`（文件末尾追加；复用 `minimal_runtime_with_session` fixture 与既有 `_fake_instantiate` 模式）

**Interfaces:**
- Consumes: `runtime._relaunch_task_recap(..., boundary="dispatch")`、fixture `minimal_runtime_with_session`
- Produces: 契约断言——dispatch 边界恢复重跑按原 boundary 重跑、不登记 close_synth。

- [ ] **Step 1: 追加测试**

```python
async def test_relaunch_dispatch_boundary_no_close_synth(minimal_runtime_with_session, monkeypatch):
    """非 close 边界（dispatch，spec 2026-07-16）重跑：按原 boundary 重跑、
    不 register_close_synth（那是 close 边界替换占位 finish 对的专属动作）。"""
    runtime, session, template, task_manager, task, agent_id, memory = minimal_runtime_with_session
    task.status = "SUSPENDED"  # 委派挂起中崩溃的形态

    async def _fake_instantiate(self, *, template_id, session_id, tenant_id, existing_agent_id, ctx, parent_agent=None):
        agent = Agent(id=existing_agent_id, session_id=session_id, template_id=template_id,
                      template_version="v1", status="IDLE", tenant_id=tenant_id)
        return agent, template

    monkeypatch.setattr(LifecycleManager, "instantiate_agent", _fake_instantiate)

    registered = []
    monkeypatch.setattr(rt_mod, "register_close_synth",
                        lambda *a, **k: registered.append(a), raising=False)

    launched = {}

    def _fake_launch(state, ctx, *, boundary):
        launched["boundary"] = boundary
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(rt_mod, "launch_background_observe", _fake_launch, raising=False)

    await runtime._relaunch_task_recap(
        session=session, template=template, template_id="tpl_echo", task_manager=task_manager,
        task=task, agent_id=agent_id, boundary="dispatch",
    )
    await asyncio.sleep(0)

    assert launched["boundary"] == "dispatch"
    assert registered == [], "dispatch 边界不得登记 close_synth"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_relaunch_task_recap.py -v`
Expected: 全部 PASS（含新用例）。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_relaunch_task_recap.py
git commit -m "test(runtime): 恢复路径 boundary=dispatch 重跑用例

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 取消后在途 recap 良性完成用例

spec §2 并发边界第 2 条的单测化：SUSPENDED 期间任务被判 CANCELED，在途 dispatch recap 事后完成——不抛错、正常折段、不回写 task 状态。取消胶囊闭合本身由既有 Task 14 测试覆盖，此处只锁「recap 与取消交叉无害」。**预期直接 PASS**。

**Files:**
- Test: `tests/unit/test_background_observe.py`（文件末尾追加）

**Interfaces:**
- Consumes: `bo.launch_background_observe`、conftest `fake_state_ctx`
- Produces: 契约断言——取消不需要与 recap 同步（spec §2「接受，不加同步」的行为依据）。

- [ ] **Step 1: 追加测试**

```python
@pytest.mark.asyncio
async def test_dispatch_recap_completes_benignly_after_cancel(monkeypatch, fake_state_ctx):
    """SUSPENDED 期间被取消：在途 dispatch recap 事后完成——不抛错、照常折段、
    不回写 task 状态（spec 2026-07-16 §2 并发边界：接受，不加同步）。"""
    state, ctx = fake_state_ctx
    ctx.capability_gateway = _FakeGateway("段总结X")
    state.agent.loop_config = SimpleNamespace(compact_keep_last=2, max_turns_per_observe=3)
    state.session = SimpleNamespace(id="s1", tenant_id="default", token_used=0)

    async def slow_stream(c, s, req):
        await asyncio.sleep(0.02)  # 给取消留出交叉窗口
        yield _make_tool_call_chunk(BACKGROUND_PROCESS_REPORT_NAME)
        yield _make_usage_chunk()

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", slow_stream)

    t = bo.launch_background_observe(state, ctx, boundary="dispatch")
    state.task.status = "CANCELED"  # recap 在跑时取消坐实
    await t

    assert t.exception() is None, "取消交叉不得让 recap 抛错"
    assert state.task.status == "CANCELED", "recap 不得回写 task 状态"
    from ctx_weft.protocols import MemoryEventType as MT
    recs = await ctx.memory.recall_recent(
        state.scope, [MT.TASK_COMPACT_SUMMARY], 100, ctx.provider_ctx)
    assert len(recs) == 1, "折的是取消前已存在的 raw——照常成段摘要（良性）"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_background_observe.py -v -k cancel`
Expected: PASS。

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_background_observe.py
git commit -m "test(background_observe): 取消后在途 recap 良性完成用例

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: e2e 集成——delegate → 子完成 → 父 resume，折叠可见

真实 runtime 驱动全链路：父派发（SuspendStep 触发 dispatch recap）→ 子跑完 → 父 resume（run 启动 await 等折叠）→ 父 finish。终态断言父 task 层有 dispatch 段摘要、派发前 raw 已折。

**Files:**
- Create: `tests/integration/test_dispatch_boundary_recap_e2e.py`（模式照抄 `tests/integration/test_finish_delegate_e2e.py`：`_RouterLLM` 按 request.tools 路由 + `rebuild_view` 轮询）

**Interfaces:**
- Consumes: Task 1 的 SuspendStep 触发 + Task 3 的 run 启动 await；`tests.integration.test_minimal_loop.InMemoryTemplateResolver` / `make_echo_template`
- Produces: 端到端行为锁定，无下游依赖。

- [ ] **Step 1: 写 e2e 测试（新文件）**

```python
"""E2E（spec 2026-07-16）：父 delegate → 子完成 → 父 resume 时派发前 raw 已折成段摘要。

路由型 mock LLM：act 第 1 次调用（root）只 delegate；第 2 次（子）finish；
第 3 次（root resume 后）finish。bg observe 第 1 次 = dispatch 边界（父挂起时），
产 "DISPATCH段摘要"；此后 = root close 复述。子任务非 root，close 不走 bg observe，
故 bg 调用次序确定。
"""
from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.reducers import rebuild_view
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import MemoryEventType, MemoryScope, ProviderContext, ToolCall
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InMemoryTemplateResolver, make_echo_template,
)

pytestmark = pytest.mark.asyncio


class _RouterLLM(MockLLMAdapter):
    """按 request.tools 路由；act 次序：root delegate → child finish → root finish。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._act_calls = 0
        self._bg_calls = 0
        self._n = 0

    def _id(self, kind: str) -> str:
        self._n += 1
        return f"{kind}{self._n}"

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}

        if "control__update_task_metadata" in names:  # recognize_intent
            return self._stream(MockResponse(text=""), request)

        if "control__report_task_outcome" in names:  # LLM observe（子任务 close）
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=self._id("obs"), name="control__report_task_outcome",
                         arguments={"task_status": "success",
                                    "task_process_report": "done"}),
            ]), request)

        if "control__collect_process_report" in names:  # background observe
            self._bg_calls += 1
            report = "DISPATCH段摘要" if self._bg_calls == 1 else "CLOSE复述"
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=self._id("bg"), name="control__collect_process_report",
                         arguments={"task_process_report": report}),
            ]), request)

        # act
        self._act_calls += 1
        if self._act_calls == 1:  # root：只 delegate（派发前已有本轮 raw）
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=self._id("del"), name="control__delegate_task",
                         arguments={"title": "child-work",
                                    "task_prompt": "do the delegated work"}),
            ]), request)
        # 子任务 act 与 root resume 后的 act：finish
        return self._stream(MockResponse(tool_calls=[
            ToolCall(id=self._id("fin"), name="control__finish_task",
                     arguments={"result": "done"}),
        ]), request)


async def _wait_all_finished(runtime, session_id, n, timeout=8.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        view = await rebuild_view(runtime.event_store, session_id)
        finished = [t for t in view.tasks.values() if t.status == "FINISHED"]
        if len(finished) >= n:
            return view
        await asyncio.sleep(0.02)
    view = await rebuild_view(runtime.event_store, session_id)
    raise TimeoutError(
        f"expected >={n} FINISHED tasks; got "
        f"{[(t.title, t.status) for t in view.tasks.values()]}"
    )


async def test_dispatch_boundary_recap_e2e():
    llm = _RouterLLM()
    resolver = InMemoryTemplateResolver()
    template = make_echo_template()
    # 关短段免折门：本测试的 raw 只有几十 token，默认阈值(400)下必免折、断言不到折叠
    template.loop_config.short_segment_token_threshold = 0
    resolver.register(template)
    runtime = CtxWeftRuntime(llm=llm, template_resolver=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="tpl_echo", user_prompt="delegate then finish",
            context_limit=100_000,
        )
    )
    await handle.wait_for_finish(timeout=8.0)
    view = await _wait_all_finished(runtime, handle.session_id, 2)

    root = next(t for t in view.tasks.values() if t.parent_task_id is None)
    scope = MemoryScope(session_id=handle.session_id, task_id=root.id,
                        agent_id=root.assigned_agent_id)
    pctx = ProviderContext(session_id=handle.session_id, tenant_id="default",
                           task_id=root.id, agent_id=root.assigned_agent_id)

    summaries = await memory.recall_recent(
        scope, [MemoryEventType.TASK_COMPACT_SUMMARY], 100, pctx)
    contents = [r.content for r in summaries]
    assert "DISPATCH段摘要" in contents, \
        f"父 task 层必须有 dispatch 段摘要（派发前 raw 的折叠产物），实得 {contents}"

    raws = await memory.recall_recent(
        scope, [MemoryEventType.LLM_RESPONSE], 100, pctx)
    assert raws == [], f"派发前 raw 应已被折掉/胶囊化，实得 {[r.content for r in raws]}"
```

- [ ] **Step 2: 跑测试确认通过**

Run: `python -m pytest tests/integration/test_dispatch_boundary_recap_e2e.py -v`
Expected: PASS。若 FAIL 于 `start_session` 签名或 `make_echo_template().loop_config` 属性——对照 `tests/integration/test_finish_delegate_e2e.py:118` 起的既有用法修正调用形态（那是权威模式，本测试只是它的 delegate-suspend 变体），断言目标不变。

- [ ] **Step 3: 全量回归**

Run: `python -m pytest tests/unit tests/integration -q`
Expected: 全部 PASS。

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_dispatch_boundary_recap_e2e.py
git commit -m "test(integration): delegate→子完成→父 resume 段折叠 e2e

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
