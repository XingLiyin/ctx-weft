# 会话状态：core 发结构化运行时事件 + host 只存 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 session 状态由 core 计算并作为携结构化摘要的 `SESSION_STATUS_CHANGED` 事件发出，host 三层投影只存不算，消除 4 处状态写者与并发下的"运行中却卡在等审批"（#3）。

**Architecture:** session 活跃态是 (task 计数 × pending HITL 集) 的派生量，由 core 单一写者 `TaskManager._emit_session_runtime()` 计算并发事件（`HitlManager` 在 pending 变动时 poke 它）；终态/中断保留既有决策事件。三个跟随者（core `reduce_events`、host `ProjectionUpdater`、host SSE `translate_event`）退化为"存 payload"，删除全部反推与命令式直写。结构体进 `sessions.runtime_json`（前向兼容载体），phase 复用现有 `status` 列。

**Tech Stack:** Python 3 / asyncio；事件溯源（`ctx_weft.core.events`）；SQLAlchemy（Postgres prod / SQLite dev）；pytest（`uv run pytest`）。

## Global Constraints

- 关联前序：`docs/superpowers/specs/2026-07-04-tm-mechanism-and-recovery-design.md`（#3 = 缺陷 C 的并发根治）。
- 前向兼容契约在**事件 payload**：`SESSION_STATUS_CHANGED.payload.runtime` 只增字段；破坏性变更才 bump `Event.schema_version`。投影表是可重建缓存、低风险。
- 投影表落地：phase 复用 `sessions.status`（String(32)）；计数进新列 `sessions.runtime_json`（Text，默认 `'{}'`）。**不**为单个字段提 typed 列（存量 DB 无 `create_all` 加列路径，每列一条手写 ALTER）。SQL 过滤列推迟到有真实查询需求时从 JSON 回填。
- 存量 DB 加列走一次性迁移框架（`persistence/postgres/migrations.py`，gated by `applied_migrations`）；新建 DB 由 `create_all` 建列 → 迁移检测到已存在即 no-op。方言分支：PG=`information_schema`、SQLite=`PRAGMA table_info`。
- 结构体字段（V1）：`running`（已派发且未卡 HITL）、`awaiting_hitl`（pending 的 approval + ask_user）、`awaiting_input`（pending 的 wait_for_user 软待命）、`queued`（队列待派发）。phase 阶梯：终态锁存 > `running>0 or queued>0`→RUNNING > `awaiting_hitl>0`→PAUSED_HITL > `awaiting_input>0`→PAUSED > 否则 RUNNING。
- `HITL_REQUIRED` / `HITL_*resolve` 事件**保留**（承载前端 `waiting_input` 面板与领域语义）；仅删它们在跟随者里的**状态副作用**。
- 测试：core 用 `uv run pytest`（cwd=`ctx-weft`，pyproject 已配 pythonpath）；host 用 `uv run pytest`（cwd 仓根 / `src` 布局）。commit 频繁、每 Task 一提。

---

### Task 1: `SessionRuntime` 结构体 + phase 阶梯（core 纯函数）

**Files:**
- Create: `src/ctx_weft/core/orchestrator/session_runtime.py`
- Test: `tests/unit/test_session_runtime.py`

**Interfaces:**
- Produces: `build_session_runtime(running:int, queued:int, awaiting_hitl:int, awaiting_input:int, terminal:str|None) -> tuple[str, dict]` — 返回 `(phase, runtime_payload)`；`runtime_payload = {"running","awaiting_hitl","awaiting_input","queued"}`。

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_session_runtime.py
from ctx_weft.core.orchestrator.session_runtime import build_session_runtime


def test_running_when_tasks_active():
    phase, rt = build_session_runtime(running=2, queued=0, awaiting_hitl=0, awaiting_input=0, terminal=None)
    assert phase == "RUNNING"
    assert rt == {"running": 2, "awaiting_hitl": 0, "awaiting_input": 0, "queued": 0}


def test_running_wins_over_pending_hitl():
    # 3 跑 + 1 等审批 → 进度胜（phase RUNNING），但明细暴露阻塞
    phase, rt = build_session_runtime(running=3, queued=0, awaiting_hitl=1, awaiting_input=0, terminal=None)
    assert phase == "RUNNING"
    assert rt["awaiting_hitl"] == 1


def test_paused_hitl_when_only_waiting():
    phase, _ = build_session_runtime(running=0, queued=0, awaiting_hitl=1, awaiting_input=0, terminal=None)
    assert phase == "PAUSED_HITL"


def test_paused_soft_when_only_wait_for_user():
    phase, _ = build_session_runtime(running=0, queued=0, awaiting_hitl=0, awaiting_input=1, terminal=None)
    assert phase == "PAUSED"


def test_queued_counts_as_running():
    phase, _ = build_session_runtime(running=0, queued=2, awaiting_hitl=1, awaiting_input=0, terminal=None)
    assert phase == "RUNNING"


def test_terminal_latches_over_derivation():
    phase, _ = build_session_runtime(running=5, queued=0, awaiting_hitl=0, awaiting_input=0, terminal="FAILED")
    assert phase == "FAILED"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_session_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: session_runtime`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ctx_weft/core/orchestrator/session_runtime.py
"""会话运行时结构化摘要：把 (task 计数 × pending HITL) 派生成 phase + 明细。

单一真相：`TaskManager._emit_session_runtime` 用它算出 SESSION_STATUS_CHANGED 的 payload。
纯函数、无副作用，便于单测阶梯逻辑。见 plan 2026-07-04-session-status-structured-runtime。
"""
from __future__ import annotations


def build_session_runtime(
    running: int,
    queued: int,
    awaiting_hitl: int,
    awaiting_input: int,
    terminal: str | None,
) -> tuple[str, dict]:
    """返回 (phase, runtime_payload)。terminal 非空则锁存压过派生。"""
    runtime = {
        "running": running,
        "awaiting_hitl": awaiting_hitl,
        "awaiting_input": awaiting_input,
        "queued": queued,
    }
    if terminal:
        return terminal, runtime
    if running > 0 or queued > 0:
        phase = "RUNNING"
    elif awaiting_hitl > 0:
        phase = "PAUSED_HITL"
    elif awaiting_input > 0:
        phase = "PAUSED"
    else:
        phase = "RUNNING"  # 空窗；真正完成由 is_done 分支发终态
    return phase, runtime
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_session_runtime.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/orchestrator/session_runtime.py tests/unit/test_session_runtime.py
git commit -m "feat(core): add build_session_runtime phase ladder + struct"
```

---

### Task 2: `HitlManager.pending_summary`（按 kind 分桶）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`（`list_pending` 附近 :287）
- Test: `tests/unit/test_hitl_pending_summary.py`

**Interfaces:**
- Produces: `HitlManager.pending_summary(session_id: str) -> list[tuple[str, bool]]` — 返回 `[(task_id, is_soft), ...]`，`is_soft = capability_id.endswith(":wait_for_user")`（wait_for_user 软待命）。供 TaskManager 算 `awaiting_hitl`/`awaiting_input` 与"哪些在跑 task 其实在等人"。

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_hitl_pending_summary.py
import pytest
from ctx_weft.core.orchestrator.hitl_manager import HitlManager
from ctx_weft.core.orchestrator.control_capability import WAIT_FOR_USER_CAPABILITY_ID


@pytest.mark.asyncio
async def test_pending_summary_classifies_soft_vs_hard():
    hm = HitlManager()
    await hm.request("approval", "s1", "tA", capability_id="control:bash_exec", tool_call_id="c1")
    await hm.request("input", "s1", "tB", capability_id="control:ask_user", tool_call_id="c2")
    await hm.request("input", "s1", "tC", capability_id=WAIT_FOR_USER_CAPABILITY_ID, tool_call_id="c3")

    summary = hm.pending_summary("s1")
    by_task = dict(summary)
    assert by_task["tA"] is False   # approval = hard
    assert by_task["tB"] is False   # ask_user = hard
    assert by_task["tC"] is True    # wait_for_user = soft


@pytest.mark.asyncio
async def test_pending_summary_scopes_to_session():
    hm = HitlManager()
    await hm.request("approval", "s1", "tA", capability_id="control:x", tool_call_id="c1")
    await hm.request("approval", "s2", "tB", capability_id="control:y", tool_call_id="c2")
    assert [tid for tid, _ in hm.pending_summary("s1")] == ["tA"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_hitl_pending_summary.py -v`
Expected: FAIL — `AttributeError: 'HitlManager' object has no attribute 'pending_summary'`

- [ ] **Step 3: Write minimal implementation**

在 `HitlManager` 里 `list_pending`（:287）之后新增：

```python
    def pending_summary(self, session_id: str) -> list[tuple[str, bool]]:
        """该 session 未决 HITL 的 (task_id, is_soft)。is_soft=wait_for_user 软待命。

        供 TaskManager 算 awaiting_hitl/awaiting_input，并识别"在跑 task 其实卡在等人"。
        """
        from ctx_weft.core.orchestrator.control_capability import WAIT_FOR_USER_CAPABILITY_ID
        return [
            (r.task_id, r.capability_id.endswith(":wait_for_user")
             or r.capability_id == WAIT_FOR_USER_CAPABILITY_ID)
            for r in self.list_pending(session_id=session_id)
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_hitl_pending_summary.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/orchestrator/hitl_manager.py tests/unit/test_hitl_pending_summary.py
git commit -m "feat(core): HitlManager.pending_summary classifies pending by kind"
```

---

### Task 3: `TaskManager` 计数 + `_emit_session_runtime` + 自触发点

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Test: `tests/unit/test_task_manager_runtime_emit.py`

**Interfaces:**
- Consumes: `build_session_runtime`（Task 1）。
- Produces:
  - `TaskManager.set_pending_hitl_provider(fn: Callable[[], list[tuple[str,bool]]]) -> None` — 注入"当前 pending (task_id,is_soft) 列表"来源（runtime 绑 `HitlManager.pending_summary`）。取代 `set_has_pending_hitl`。
  - `TaskManager._emit_session_runtime(terminal: str|None = None) -> None` — 计算 + 去重 + 发 `SESSION_STATUS_CHANGED{new_status:phase, runtime:{...}}`。

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_task_manager_runtime_emit.py
import pytest
from ctx_weft.core.events.bus import EventBus
from ctx_weft.core.events.types import EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager


def _collect(bus):
    seen = []
    bus.subscribe(None, lambda ev: seen.append(ev) or _noop())
    return seen


async def _noop():
    return None


@pytest.mark.asyncio
async def test_emit_session_runtime_reports_awaiting_hitl():
    bus = EventBus()
    got = []
    bus.subscribe(None, lambda ev: got.append(ev))
    tm = TaskManager("s1", event_bus=bus)
    # 两个在跑 task，其中 tA 卡在硬 HITL
    tm._running_tasks = {"tA", "tB"}
    tm.set_pending_hitl_provider(lambda: [("tA", False)])

    await tm._emit_session_runtime()

    ev = [e for e in got if e.type == EventType.SESSION_STATUS_CHANGED][-1]
    assert ev.payload["new_status"] == "RUNNING"            # tB 仍在跑 → 进度胜
    assert ev.payload["runtime"]["running"] == 1            # tA 被扣除
    assert ev.payload["runtime"]["awaiting_hitl"] == 1


@pytest.mark.asyncio
async def test_emit_session_runtime_dedupes():
    bus = EventBus()
    got = []
    bus.subscribe(None, lambda ev: got.append(ev))
    tm = TaskManager("s1", event_bus=bus)
    tm.set_pending_hitl_provider(lambda: [])
    await tm._emit_session_runtime()
    n_after_first = len([e for e in got if e.type == EventType.SESSION_STATUS_CHANGED])
    await tm._emit_session_runtime()  # 状态未变 → 不应重复发
    n_after_second = len([e for e in got if e.type == EventType.SESSION_STATUS_CHANGED])
    assert n_after_second == n_after_first
```

> 注：`EventBus.subscribe` 的回调签名以本仓实现为准（见 `events/bus.py`）；若为 async 回调，改用 `async def` 收集器。实现前先核 `bus.subscribe` 签名，测试收集器随之对齐。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_manager_runtime_emit.py -v`
Expected: FAIL — `AttributeError: ... 'set_pending_hitl_provider'`

- [ ] **Step 3: Write minimal implementation**

在 `TaskManager.__init__` 增字段（替换 `_has_pending_hitl` 的语义，保留旧方法作兼容 shim）：

```python
        self._pending_hitl_provider: Callable[[], list[tuple[str, bool]]] | None = None
        self._last_runtime: tuple[str, tuple] | None = None
```

新增方法（放在 `set_has_pending_hitl` 附近）：

```python
    def set_pending_hitl_provider(self, fn: "Callable[[], list[tuple[str, bool]]]") -> None:
        """注入当前 pending HITL 的 (task_id, is_soft) 列表来源（runtime 绑 HitlManager.pending_summary）。"""
        self._pending_hitl_provider = fn

    def _runtime_counts(self) -> tuple[int, int, int, int]:
        """(running, queued, awaiting_hitl, awaiting_input)。running 扣除卡在 HITL 的在跑 task。"""
        pend = self._pending_hitl_provider() if self._pending_hitl_provider else []
        awaiting_input = sum(1 for _, soft in pend if soft)
        awaiting_hitl = len(pend) - awaiting_input
        parked = {tid for tid, _ in pend}
        running = len(self._running_tasks - parked)
        queued = self._queue.pending_count()
        return running, queued, awaiting_hitl, awaiting_input

    async def _emit_session_runtime(self, terminal: str | None = None) -> None:
        """算会话运行时结构体，去重后发 SESSION_STATUS_CHANGED。core 是 session 状态唯一写者。"""
        from ctx_weft.core.orchestrator.session_runtime import build_session_runtime
        running, queued, awaiting_hitl, awaiting_input = self._runtime_counts()
        phase, runtime = build_session_runtime(running, queued, awaiting_hitl, awaiting_input, terminal)
        key = (phase, tuple(sorted(runtime.items())))
        if key == self._last_runtime:
            return
        self._last_runtime = key
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={"new_status": phase, "runtime": runtime})
```

保留旧注入名以免打断现有 wiring（内部转发）：

```python
    def set_has_pending_hitl(self, predicate: "Callable[[], bool]") -> None:
        """兼容 shim（旧签名）：包装成 provider。新代码用 set_pending_hitl_provider。"""
        self._pending_hitl_provider = lambda p=predicate: [] if not p() else [("", False)]
```

在四处自触发（不改各分支既有逻辑，仅追加一行）：
1. `drain` 入口（`if self._runtime is None` 之后、while 之前）：`await self._emit_session_runtime()`。
2. `on_task_finished`：把 :605 的 `SESSION_STATUS_CHANGED` 发射替换为 `await self._emit_session_runtime(terminal=final_status)`（`SESSION_FINISHED` 仍由 `_fire_session_done` 发）；`FAILED`(阈值,:578)/`CANCELED`(:626) 分支同样改调 `_emit_session_runtime(terminal=...)`。
3. `_run_task` 的 SUSPENDED 分支（:347 `if self.is_done()` 之前）：`await self._emit_session_runtime()`。
4. `_handle_task_failure` retry 重排（drain 之前）与 `reopen_task` 末尾：`await self._emit_session_runtime()`。

> `drain` 是 sync 进 `async with` 前的 `await`——它本就是 async 方法，直接 await 即可。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_task_manager_runtime_emit.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Run existing TM/HITL suites (regression)**

Run: `cd ctx-weft && uv run pytest tests/unit/test_hitl_recovery.py tests/unit/test_hitl_park.py tests/unit/test_superseded_task_manager.py -v`
Expected: PASS（既有断言不受结构体新增事件影响；若某测试断言"SESSION_STATUS_CHANGED 恰一条"则需按新语义调整——见该测试改动记入 commit）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_task_manager_runtime_emit.py
git commit -m "feat(core): TaskManager emits structured session runtime on transitions"
```

---

### Task 4: runtime 接线 —— provider 注入 + HitlManager poke（#3 头条测试）

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`_register_and_drain` :716、`recover_session` 链）
- Modify: `src/ctx_weft/core/orchestrator/hitl_manager.py`（`request` / `_resolve` 末尾 + 回调 seam）
- Test: `tests/unit/test_session_runtime_concurrent_hitl.py`

**Interfaces:**
- Consumes: `TaskManager.set_pending_hitl_provider`（Task 3）、`HitlManager.pending_summary`（Task 2）。
- Produces: `HitlManager.set_on_pending_changed(fn: Callable[[str], Awaitable[None]] | None)` — pending 集变动（request/resolve）后按 session_id 回调；runtime 路由到该 session 的 TM `_emit_session_runtime()`。

- [ ] **Step 1: Write the failing test**（并发 HITL：答一个后仍显示等审批）

```python
# tests/unit/test_session_runtime_concurrent_hitl.py
import pytest
from ctx_weft.core.events.bus import EventBus
from ctx_weft.core.events.types import EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.hitl_manager import HitlManager


@pytest.mark.asyncio
async def test_answering_one_hitl_keeps_session_paused_while_another_waits():
    bus = EventBus()
    got = []
    bus.subscribe(None, lambda ev: got.append(ev))
    hm = HitlManager(event_bus=bus)
    tm = TaskManager("s1", event_bus=bus)
    tm._running_tasks = {"tA", "tB"}
    tm.set_pending_hitl_provider(lambda: hm.pending_summary("s1"))
    # runtime 的路由：pending 变了就让 TM 重算
    hm.set_on_pending_changed(lambda sid: tm._emit_session_runtime())

    await hm.request("input", "s1", "tA", capability_id="control:ask_user", tool_call_id="c1")
    await hm.request("approval", "s1", "tB", capability_id="control:bash_exec", tool_call_id="c2")

    await hm.answer("c1_req", "done") if False else None  # answer 用 request_id，见下
    # 取 tA 的 request_id 应答
    rid_a = hm.find_for_tool_call("c1").id
    await hm.answer(rid_a, "ok")

    last = [e for e in got if e.type == EventType.SESSION_STATUS_CHANGED][-1]
    # tB 仍在等审批 → 明细必须暴露，phase 不得谎报成"无人等待"
    assert last.payload["runtime"]["awaiting_hitl"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_session_runtime_concurrent_hitl.py -v`
Expected: FAIL — `AttributeError: ... 'set_on_pending_changed'`

- [ ] **Step 3: Write minimal implementation**

HitlManager：`__init__` 增 `self._on_pending_changed = None`；新增 setter + 在 `request`（`return rid` 之前）与 `_resolve`（`return req, was_hot` 之前）调用：

```python
    def set_on_pending_changed(self, fn: "Callable[[str], Awaitable[None]] | None") -> None:
        """pending 集变动（request/resolve）后的回调（runtime 路由到该 session 的 TM 重算运行时）。"""
        self._on_pending_changed = fn

    async def _notify_pending_changed(self, session_id: str) -> None:
        if self._on_pending_changed is not None:
            try:
                await self._on_pending_changed(session_id)
            except Exception:
                logger.exception("HitlManager: on_pending_changed failed for %s", session_id)
```

- `request`：`return rid` 前加 `await self._notify_pending_changed(session_id)`。
- `_resolve`：在 `_gc_resolved()` 后、冷 resolve 触发**之前**加 `await self._notify_pending_changed(req.session_id)`（保证 SSE/投影先看到运行时更新，再走续跑）。

runtime `_register_and_drain`：把 `set_has_pending_hitl(...)` 替换为：

```python
        task_manager.set_pending_hitl_provider(
            lambda sid=session.id: self.hitl_manager.pending_summary(sid)
        )
```

runtime 构造/初始化 HitlManager 处加一次绑定（仿 `set_cold_resolve_handler`）：

```python
        self.hitl_manager.set_on_pending_changed(self._emit_runtime_for_session)
```

新增 runtime 方法：

```python
    async def _emit_runtime_for_session(self, session_id: str) -> None:
        """HitlManager poke 路由：让该 session 的当前 owner TM 重算并发运行时事件。"""
        tm = self._task_managers.get(session_id)
        if tm is not None:
            await tm._emit_session_runtime()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_session_runtime_concurrent_hitl.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Run recovery/hitl regression**

Run: `cd ctx-weft && uv run pytest tests/unit/test_hitl_recovery.py tests/unit/test_hitl_ask_human_cold.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/runtime.py src/ctx_weft/core/orchestrator/hitl_manager.py tests/unit/test_session_runtime_concurrent_hitl.py
git commit -m "feat(core): wire HitlManager poke + pending provider into session runtime emit"
```

---

### Task 5: core `reduce_events` 只存运行时、删反推

**Files:**
- Modify: `src/ctx_weft/core/control/reducers.py`（`_apply` :307-371、:484-490）
- Modify: `src/ctx_weft/core/control/types.py`（`SessionView` / `RunStateView` 加 `runtime` 字段）
- Test: `tests/unit/test_reducers_session_runtime.py`

**Interfaces:**
- Consumes: `SESSION_STATUS_CHANGED.payload.runtime`（Task 3 发出）。
- Produces: `RunStateView.session_status` 从 `new_status` 存；新增 `RunStateView.session_runtime: dict`。

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reducers_session_runtime.py
from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.utils import now_utc


def _ev(t, payload=None, sid="s1"):
    return Event(id="e", run_id="r", sequence=0, session_id=sid, type=t,
                 timestamp=now_utc(), payload=payload or {})


def test_run_started_alone_does_not_set_session_running():
    # 反推已删：RUN_STARTED 只影响 task_status，不碰 session_status
    view = reduce_events([_ev(EventType.SESSION_CREATED, {"template_id": "t"}),
                          _ev(EventType.SESSION_STATUS_CHANGED, {"new_status": "PAUSED_HITL"}),
                          _ev(EventType.RUN_STARTED, {"run_id": "r"})], run_id="r")
    assert view.session_status == "PAUSED_HITL"


def test_session_status_changed_stores_runtime():
    view = reduce_events([_ev(EventType.SESSION_CREATED, {"template_id": "t"}),
                          _ev(EventType.SESSION_STATUS_CHANGED,
                              {"new_status": "RUNNING",
                               "runtime": {"running": 1, "awaiting_hitl": 1, "awaiting_input": 0, "queued": 0}})],
                         run_id="r")
    assert view.session_status == "RUNNING"
    assert view.session_runtime["awaiting_hitl"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ctx-weft && uv run pytest tests/unit/test_reducers_session_runtime.py -v`
Expected: FAIL（`test_run_started_alone...` 因 :318 仍反推 RUNNING 而失败；`session_runtime` 属性不存在）

- [ ] **Step 3: Write minimal implementation**

`control/types.py`：`RunStateView` 加 `session_runtime: dict = field(default_factory=dict)`。

`reducers.py _apply`：
- `RUN_STARTED`（:316-318）：删 `view.session_status = "RUNNING"`，仅保留 `view.task_status = "ACTIVE"`。
- `RUN_FINISHED`（:319-326）：删对 `view.session_status` 的写（`final_status != "SUSPENDED"` 那条），仅保留 task 层。
- `SESSION_STATUS_CHANGED`（:354-360）：保留 `new_status` 写；追加 `if "runtime" in p: view.session_runtime = p["runtime"]`。
- `SESSION_PAUSED_HITL`（:362-371）：整块删（状态改由 SESSION_STATUS_CHANGED 承载）。
- HITL resolve → RUNNING（:484-490）：整块删。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ctx-weft && uv run pytest tests/unit/test_reducers_session_runtime.py -v`
Expected: PASS

- [ ] **Step 5: Run reducer regression**

Run: `cd ctx-weft && uv run pytest tests/unit -k reducer -v`
Expected: PASS（受影响的旧断言随删反推更新，记入本 commit）

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/control/reducers.py src/ctx_weft/core/control/types.py tests/unit/test_reducers_session_runtime.py
git commit -m "refactor(core): reduce_events stores runtime, drops RUN_*/HITL_* status inference"
```

---

### Task 6: host `ProjectionUpdater` 存 runtime_json、删反推 + `SessionModel` 加列

**Files:**
- Modify: `src/ipmastercowork/persistence/postgres/models.py`（`SessionModel` :26-43）
- Modify: `src/ipmastercowork/persistence/postgres/projection_updater.py`（`_handle` :55-78）
- Modify: `src/ipmastercowork/persistence/postgres/state_store.py`（`SessionRecord` :33-54 加字段）
- Test: `src/ipmastercowork/tests/.../test_projection_updater_runtime.py`（按 host 测试目录放置）

**Interfaces:**
- Consumes: `SESSION_STATUS_CHANGED.payload.{new_status, runtime}`。
- Produces: `sessions.status`=phase、`sessions.runtime_json`=JSON(runtime)。

- [ ] **Step 1: Write the failing test**

```python
# host 测试：ProjectionUpdater 写 runtime_json，HITL_ANSWERED 不再翻 RUNNING
import json, pytest
from ipmastercowork.persistence.postgres.projection_updater import ProjectionUpdater
# ...（用现有 host 测试的内存 sqlite factory fixture）

@pytest.mark.asyncio
async def test_projection_writes_runtime_json(session_factory, make_event):
    pu = ProjectionUpdater(session_factory)
    await pu.on_event(make_event("SessionCreated", {"template_id": "t"}))
    await pu.on_event(make_event("SessionStatusChanged",
        {"new_status": "RUNNING", "runtime": {"running": 1, "awaiting_hitl": 1, "awaiting_input": 0, "queued": 0}}))
    row = await _read_session(session_factory, "s1")
    assert row.status == "RUNNING"
    assert json.loads(row.runtime_json)["awaiting_hitl"] == 1


@pytest.mark.asyncio
async def test_hitl_answered_no_longer_flips_status(session_factory, make_event):
    pu = ProjectionUpdater(session_factory)
    await pu.on_event(make_event("SessionCreated", {"template_id": "t"}))
    await pu.on_event(make_event("SessionStatusChanged", {"new_status": "PAUSED_HITL", "runtime": {}}))
    await pu.on_event(make_event("HitlAnswered", {"approval_id": "h1"}))
    row = await _read_session(session_factory, "s1")
    assert row.status == "PAUSED_HITL"   # 状态只由 SESSION_STATUS_CHANGED 定
```

> host fixture（`session_factory`/`make_event`/`_read_session`）复用现有 host 投影测试的既有 helper；无则在本测试文件内建最小 sqlite `async_sessionmaker` + `Base.metadata.create_all`。

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/ipmastercowork/tests -k projection_runtime -v`
Expected: FAIL（`runtime_json` 列不存在 / HITL_ANSWERED 仍翻 RUNNING）

- [ ] **Step 3: Write minimal implementation**

`SessionModel`（models.py，`config_json` 附近）加列：

```python
    runtime_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
```

`ProjectionUpdater._handle`：
- `SESSION_STATUS_CHANGED`（:55-58）：写 status 之外，附带 runtime：

```python
        elif t == EventType.SESSION_STATUS_CHANGED:
            new_status = p.get("new_status", "")
            values: dict = {}
            if new_status in _VALID_SESSION_STATUSES:
                values["status"] = new_status
            if "runtime" in p:
                values["runtime_json"] = json.dumps(p["runtime"])
            if values:
                await self._update_session(event.session_id, **values)
```

- 删 `SESSION_PAUSED_HITL` 分支（:65-69）与 HITL resolve→RUNNING 分支（:71-78）。

`SessionRecord`（state_store.py）加 `runtime: dict = field(default_factory=dict)` 并在读出处 `json.loads(row.runtime_json or "{}")`。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/ipmastercowork/tests -k projection_runtime -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/persistence/postgres/models.py src/ipmastercowork/persistence/postgres/projection_updater.py src/ipmastercowork/persistence/postgres/state_store.py src/ipmastercowork/tests
git commit -m "refactor(host): ProjectionUpdater stores runtime_json, drops status inference"
```

---

### Task 7: host SSE `translate_event` 跟随 + 删命令式直写

**Files:**
- Modify: `src/ipmastercowork/api/models/session.py`（`translate_event` :335-380、`_session_update_json` :135、`SessionEntry` 加 last-runtime、`sse_generator` :649）
- Modify: `src/ipmastercowork/api/sessions.py`（删 :158-162 / :361,365 / :503,507 的直写；保留 DB `update_session_status` 仅用于无事件场景说明）
- Test: `src/ipmastercowork/tests/.../test_translate_event_runtime.py`

**Interfaces:**
- Consumes: `SESSION_STATUS_CHANGED.payload.{new_status, runtime}`。
- Produces: `session_update` SSE 帧携 `runtime`；`entry.status` 只由 SESSION_STATUS_CHANGED 设。

- [ ] **Step 1: Write the failing test**

```python
# host 测试：translate_event 把 runtime 带进 session_update；HITL_* 不再设 entry.status
import json
from ipmastercowork.api.models.session import SessionEntry

def _entry():
    e = SessionEntry(session_id="s1")  # 按现有构造签名
    return e

def test_status_changed_carries_runtime(make_event):
    e = _entry(); e.status = "RUNNING"
    out = e.translate_event(make_event("SessionStatusChanged",
        {"new_status": "RUNNING", "runtime": {"running": 1, "awaiting_hitl": 1, "awaiting_input": 0, "queued": 0}}))
    frame = json.loads(out)
    assert frame["type"] == "session_update"
    assert frame["runtime"]["awaiting_hitl"] == 1
    assert e.status == "RUNNING"

def test_hitl_answered_does_not_set_status(make_event):
    e = _entry(); e.status = "PAUSED_HITL"
    e.translate_event(make_event("HitlAnswered", {"approval_id": "h1"}))
    assert e.status == "PAUSED_HITL"   # 不再在 SSE 层翻 RUNNING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/ipmastercowork/tests -k translate_event_runtime -v`
Expected: FAIL（session_update 无 runtime 键 / HITL_ANSWERED 仍翻 RUNNING）

- [ ] **Step 3: Write minimal implementation**

`SessionEntry`：加 `self.runtime: dict = {}`。`_session_update_json(self, status)` 末尾把 `self.runtime` 并入返回 dict（`"runtime": self.runtime`）。

`translate_event`：
- `SESSION_STATUS_CHANGED`（:366-374）：`if "runtime" in p: self.runtime = p["runtime"]`，再 `return self._session_update_json(new_status)`。
- `HITL_REQUIRED`（:335-357）：**删** :339/:342 的 `self.status = ...`（面板 `waiting_input` 与 PAUSED 软待命的 `_session_update_json` 返回保留？——软待命面板由 status 事件驱动，这里只发面板，不设 status）。wait_for_user 分支改为仅 `return None`（面板无、状态由 core 事件设）；hard 分支保留发 `waiting_input`、删 `self.status`。
- HITL resolve（:359-364）：整块删（状态由 SESSION_STATUS_CHANGED 承载）；若前端靠此帧收面板，改为返回一个不改 status 的 `hitl_resolved` 通知或 `None`（按前端契约定；默认 `None`）。
- `SESSION_PAUSED_HITL`（:376-380）：整块删。

`sse_generator` :649：`_session_update_json(entry.status)` 现自动带 `entry.runtime`，重连即重发结构体。

`api/sessions.py`：删 :159-162（HITL 应答处 `entry.status="RUNNING"` + 帧）、:361/365（/resume）、:503/507（/messages 续接）的直写——状态改由 core 事件经 `session_consumer` 落。`/resume` 的 DB `update_session_status` 可暂留作"事件到达前的即时反馈"，但**不设 entry.status**（避免与事件竞争）；理想是也删、完全依赖 recover 后首个 `_emit_session_runtime`。

- [ ] **Step 4: Run test + 手动核 session_consumer 已 translate SESSION_STATUS_CHANGED**

Run: `uv run pytest src/ipmastercowork/tests -k translate_event_runtime -v`
Expected: PASS。并确认 `session_consumer`（session.py:571-585）对 `SESSION_STATUS_CHANGED` 走 `append_event`→`translate_event`（已确认 :366 认它）。

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/api/models/session.py src/ipmastercowork/api/sessions.py src/ipmastercowork/tests
git commit -m "refactor(host): SSE follows SESSION_STATUS_CHANGED runtime, drop imperative status writes"
```

---

### Task 8: 数据迁移 m004 —— `sessions.runtime_json` 补列（存量 DB）

**Files:**
- Modify: `src/ipmastercowork/persistence/postgres/migrations.py`（新增 `_m004` + 注册）
- Test: `src/ipmastercowork/tests/.../test_migration_runtime_json.py`

**Interfaces:**
- Consumes: 无（DDL）。方言分支：`postgresql`→information_schema、`sqlite`→PRAGMA。
- Produces: 存量 `sessions` 表补 `runtime_json TEXT NOT NULL DEFAULT '{}'`；已存在则 no-op（返回 0）。

- [ ] **Step 1: Write the failing test**

```python
# src/ipmastercowork/tests/.../test_migration_runtime_json.py
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from ipmastercowork.persistence.postgres.migrations import _m004_add_sessions_runtime_json


@pytest.mark.asyncio
async def test_m004_adds_column_to_legacy_sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        # 模拟"旧 schema"：无 runtime_json 列的 sessions 表
        await db.execute(text("CREATE TABLE sessions (id TEXT PRIMARY KEY, status TEXT)"))
        await db.execute(text("INSERT INTO sessions (id, status) VALUES ('s1', 'RUNNING')"))
        await db.commit()
    async with factory() as db:
        async with db.begin():
            added = await _m004_add_sessions_runtime_json(db)
        assert added == 1
        cols = [r[1] for r in (await db.execute(text("PRAGMA table_info(sessions)"))).all()]
        assert "runtime_json" in cols
    # 幂等：再跑一次 → 0
    async with factory() as db:
        async with db.begin():
            assert await _m004_add_sessions_runtime_json(db) == 0
    await engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest src/ipmastercowork/tests -k migration_runtime_json -v`
Expected: FAIL — `ImportError: cannot import name '_m004_add_sessions_runtime_json'`

- [ ] **Step 3: Write minimal implementation**

在 `migrations.py`（`MIGRATIONS` 注册表之前）新增：

```python
async def _m004_add_sessions_runtime_json(db: AsyncSession) -> int:
    """给 sessions 投影表补 runtime_json 列（会话运行时结构化摘要的前向兼容载体）。

    存量 DB 由此补列;新建 DB 已由 create_all 建好该列 → 检测到已存在即 no-op。
    列存在检测按方言分支(PG=information_schema, SQLite=PRAGMA),避免在事务内对已存在列
    重复 ALTER 触发 PG 事务中止。返回 1=加列, 0=已存在。
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        exists = (await db.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'runtime_json'"
        ))).first() is not None
    else:  # sqlite (dev) 及其它
        cols = (await db.execute(text("PRAGMA table_info(sessions)"))).all()
        exists = any(row[1] == "runtime_json" for row in cols)
    if exists:
        return 0
    await db.execute(text(
        "ALTER TABLE sessions ADD COLUMN runtime_json TEXT NOT NULL DEFAULT '{}'"
    ))
    return 1
```

注册（`MIGRATIONS` 列表追加，勿改既有顺序/id）：

```python
    ("m004_add_sessions_runtime_json", _m004_add_sessions_runtime_json),
```

顶部确保 `from sqlalchemy.ext.asyncio import AsyncSession`（已 import async_sessionmaker，补 AsyncSession 类型即可；`text` 已 import）。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest src/ipmastercowork/tests -k migration_runtime_json -v`
Expected: PASS（1 passed）

- [ ] **Step 5: Full migration smoke (dry-run) + 全量回归**

Run: `cd ctx-weft && uv run pytest tests/unit -q` 和 `uv run pytest src/ipmastercowork/tests -q`
Expected: PASS（预存的 `test_fs_config.py` 2 例与本次无关，见前序 spec Part 3）。

- [ ] **Step 6: Commit**

```bash
git add src/ipmastercowork/persistence/postgres/migrations.py src/ipmastercowork/tests
git commit -m "feat(host): m004 adds sessions.runtime_json column (dialect-aware, idempotent)"
```

---

## Self-Review

**Spec coverage：** 事件流补全（Task 3/4 发、Task 5/6/7 存）；结构化摘要（Task 1 结构体 + phase、Task 3 计数）；#3 并发根治（Task 4 头条测试）；前向兼容（Task 6 JSON 列 + Task 8 迁移，phase 复用 status）；删 4 处写者（Task 5 reduce_events、Task 6 ProjectionUpdater、Task 7 translate_event + API 直写）。热 park 经 HitlManager poke（Task 4）覆盖；INTERRUPTED→RUNNING 经 Task 3 的 drain 入口 emit 自然清（无需专门事件）。

**Placeholder scan：** 无 TBD；各 Step 附真实代码/测试。host 测试 fixture 明确指向"复用现有 host 投影测试 helper 或本文件内建最小 sqlite factory"——非占位，是两条明确落地方式。

**Type consistency：** `build_session_runtime`(Task1) 签名与 `_emit_session_runtime`(Task3) 调用一致；`pending_summary`→`list[tuple[str,bool]]`(Task2) 与 `set_pending_hitl_provider`(Task3) / provider lambda(Task4) 一致；`SESSION_STATUS_CHANGED.payload.{new_status,runtime}` 在 Task3 发、Task5/6/7 读一致；`runtime_json` 列名在 Task6/8 一致。

**待确认（实现前核 1 分钟）：** ① `EventBus.subscribe` 回调签名（sync vs async）→ 决定 Task3/4 测试收集器写法；② host 测试目录实际路径与既有 fixture 名 → 决定 Task6/7/8 测试放置。二者不改设计、只对齐样板。

---

**Plan complete and saved to `docs/superpowers/plans/2026-07-04-session-status-structured-runtime.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 每 Task 派新 subagent、Task 间我 review，快速迭代。

**2. Inline Execution** — 本会话内按 executing-plans 批量执行、检查点 review。

**选哪种？**（另：Task 3/4 测试的 EventBus 回调签名、host 测试目录这两处样板，我可在开工前 1 分钟先核实再落。）
