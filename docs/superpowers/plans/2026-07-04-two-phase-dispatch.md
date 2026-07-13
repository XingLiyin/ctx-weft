# TM 两阶段派发（装配/执行分离）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TaskRunner 单闭包契约拆成 assemble（装配执行 agent）/ execute（执行任务）两阶段，TASK_STARTED 与 assigned_agent_id 回填归 TaskManager，串行键单一真相化。

**Architecture:** 新模块 `core/orchestrator/task_runner.py` 定义 `AgentBinding` + `TaskRunner` 协议 + `effective_agent_id` 纯函数；`TaskManager._run_task` 改两阶段调用并接管 TASK_STARTED；runtime 的 `_make_task_runner` 闭包改写为 `_SessionTaskRunner` 类。spec：`docs/superpowers/specs/2026-07-04-two-phase-dispatch-design.md`。

**Tech Stack:** Python 3.11+（match/case、Protocol）、pytest、uv。

## Global Constraints

- 测试一律 `uv run pytest`（不要 `python -m pytest`，会掩盖 sys.path 问题）。
- core 品牌中性：不读 env、不出现 IPMC/ipmastercowork 字样。
- host（`src/ipmastercowork/`）零改动；`run_single_task`、TaskQueue、staged/flush、restore、recover 流程不动。
- 不考虑上游 LoomeX-00 回灌（用户 2026-07-04 拍板）。
- 行为不变基线：全量既有测试须绿（唯一已知 flaky：`test_snapshot_writer.py::test_postgres_snapshot_prune_keeps_latest_n`，ULID 同毫秒 tie-break，与本次无关）。
- 工作分支：`refactor/tm-two-phase-dispatch`（从 master 拉，完成后合回 master）。

---

### Task 1: 新模块 task_runner.py（AgentBinding / TaskRunner 协议 / effective_agent_id）

**Files:**
- Create: `src/ctx_weft/core/orchestrator/task_runner.py`
- Test: `tests/unit/test_effective_agent_id.py`

**Interfaces:**
- Produces（后续 Task 全部依赖，签名逐字使用）：
  - `@dataclass AgentBinding(agent_id: str, agent: Any = None, template: Any = None, initial_step: str = "prepare", run_id: str = "")`
  - `class TaskRunner(Protocol)`: `async def assemble(self, task_id: str) -> AgentBinding | None`；`async def execute(self, binding: AgentBinding, task_id: str) -> None`
  - `def effective_agent_id(task: Task | None, root_agent_id: str) -> str`

- [ ] **Step 1: 建分支**

```bash
git checkout -b refactor/tm-two-phase-dispatch master
```

- [ ] **Step 2: 写失败测试**

`tests/unit/test_effective_agent_id.py`：

```python
"""effective_agent_id 纯函数——「任务跑在哪个 agent scope」的单一真相。

五分支：subagent 已 assigned / subagent 未 assigned（占位 token）/
assigned / creator / root / 全空 per-task 兜底。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.task_runner import effective_agent_id
from ctx_weft.core.state.models import NormalTaskSettings, Task


def _task(tid: str = "T", **kw) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", **kw)


def test_none_task_is_empty() -> None:
    assert effective_agent_id(None, "root") == ""


def test_subagent_assigned_wins() -> None:
    t = _task(settings=NormalTaskSettings(use_subagent=True))
    t.assigned_agent_id = "agt_sub"
    assert effective_agent_id(t, "root") == "agt_sub"


def test_subagent_unassigned_gets_unique_placeholder() -> None:
    a = _task("A", settings=NormalTaskSettings(use_subagent=True))
    b = _task("B", settings=NormalTaskSettings(use_subagent=True))
    assert effective_agent_id(a, "root") == "__sub__A"
    assert effective_agent_id(b, "root") == "__sub__B"


def test_non_subagent_assigned_over_creator_over_root() -> None:
    t = _task()
    t.assigned_agent_id = "agt_a"
    t.creator_agent_id = "agt_c"
    assert effective_agent_id(t, "root") == "agt_a"
    t.assigned_agent_id = ""
    assert effective_agent_id(t, "root") == "agt_c"
    t.creator_agent_id = ""
    assert effective_agent_id(t, "root") == "root"


def test_all_empty_falls_back_to_per_task_token() -> None:
    assert effective_agent_id(_task("T9"), "") == "__task__T9"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_effective_agent_id.py -v`
Expected: FAIL（`ModuleNotFoundError`/`ImportError: task_runner`）

- [ ] **Step 4: 写实现**

`src/ctx_weft/core/orchestrator/task_runner.py`：

```python
"""两阶段 TaskRunner 契约：装配（assemble）与执行（execute）分离。

TaskManager 在派发点先调 assemble 拿到 AgentBinding（TM 据此回填
assigned_agent_id、发 TASK_STARTED、登记同 agent 串行键），再调 execute
驱动 step loop。spec: docs/superpowers/specs/2026-07-04-two-phase-dispatch-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ctx_weft.core.state.models import NormalTaskSettings

if TYPE_CHECKING:
    from ctx_weft.core.state.models import Task


@dataclass
class AgentBinding:
    """assemble 的产物：本次派发的执行 agent 绑定。

    TaskManager 只读 agent_id；agent/template/initial_step/run_id 对 TM
    不透明，由 execute 消费。
    """

    agent_id: str
    agent: Any = None
    template: Any = None
    initial_step: str = "prepare"
    run_id: str = ""


class TaskRunner(Protocol):
    """两阶段 runner 协议。assemble 返回 None 表示 task 已不存在（装配空转）。"""

    async def assemble(self, task_id: str) -> "AgentBinding | None": ...

    async def execute(self, binding: "AgentBinding", task_id: str) -> None: ...


def effective_agent_id(task: "Task | None", root_agent_id: str) -> str:
    """任务实际执行所在 agent scope 的单一真相——调度串行键与装配共用。

    - subagent 任务：每次实例化独立 agent；assigned 未定时用 task.id 造唯一
      占位 token（只作串行键，永不碰撞）。
    - 其余：assigned or creator or root——非 subagent 任务在创建者的 agent
      scope 上跑（延续创建者对话）；三者皆空退 per-task token，避免把
      「未知 agent」误并成一桶而过度串行。
    """
    if task is None:
        return ""
    s = task.settings
    if isinstance(s, NormalTaskSettings) and s.use_subagent:
        return task.assigned_agent_id or f"__sub__{task.id}"
    return (
        task.assigned_agent_id
        or task.creator_agent_id
        or root_agent_id
        or f"__task__{task.id}"
    )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_effective_agent_id.py -v`
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_runner.py tests/unit/test_effective_agent_id.py
git commit -m "feat(core): AgentBinding/TaskRunner 协议 + effective_agent_id 单一真相"
```

---

### Task 2: TaskManager 两阶段派发（TASK_STARTED 归 TM、真实串行键、装配失败标注）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py`
- Create: `tests/unit/_stub_runner.py`（测试共用 stub）
- Modify: `tests/unit/test_task_scheduling.py`、`test_same_agent_serialization.py`、`test_superseded_task_manager.py`、`test_hitl_recovery.py`、`test_task_manager_cancel_all.py`
- Test: `tests/unit/test_two_phase_dispatch.py`（新增）

**Interfaces:**
- Consumes: Task 1 的 `AgentBinding` / `TaskRunner` / `effective_agent_id`。
- Produces:
  - `TaskManager.set_runner(runner: TaskRunner)` 现接受两阶段对象（协议见 Task 1）。
  - `TaskManager._handle_task_failure(..., reason: str = "run_failure_retry")` 新 kwarg，TASK_REQUEUED payload 的 `reason` 用它。
  - `tests/unit/_stub_runner.py::StubRunner(tm, execute_fn=None, session_id="s1")`——旧式 `(sid, tid)` 协程适配成两阶段对象，assemble 用 `effective_agent_id` 镜像真实装配。

- [ ] **Step 1: 写测试共用 stub**

`tests/unit/_stub_runner.py`：

```python
"""两阶段 TaskRunner 的测试 stub：旧式 (session_id, task_id) 协程适配器。

assemble 用 effective_agent_id 镜像真实装配的串行键（预测==真实），
使 same-agent 串行语义在 stub 下与生产一致。
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_runner import AgentBinding, effective_agent_id

ExecuteFn = Callable[[str, str], Coroutine[Any, Any, None]]


class StubRunner:
    def __init__(self, tm: TaskManager, execute_fn: ExecuteFn | None = None,
                 session_id: str = "s1") -> None:
        self._tm = tm
        self._fn = execute_fn
        self._session_id = session_id

    async def assemble(self, task_id: str) -> AgentBinding | None:
        t = self._tm.get_task(task_id)
        if t is None:
            return None
        root = self._tm.session.root_agent_id if self._tm.session else ""
        return AgentBinding(agent_id=effective_agent_id(t, root))

    async def execute(self, binding: AgentBinding, task_id: str) -> None:
        if self._fn is not None:
            await self._fn(self._session_id, task_id)
```

- [ ] **Step 2: 写失败的新契约测试**

`tests/unit/test_two_phase_dispatch.py`：

```python
"""两阶段派发契约：TASK_STARTED 归 TM 单发、装配失败标注且不发 TASK_STARTED、
在跑任务串行键用装配的真实 agent id。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_runner import AgentBinding
from ctx_weft.core.state.models import Session, Task

from tests.unit._stub_runner import StubRunner

pytestmark = pytest.mark.asyncio


@dataclass
class _CapturingBus:
    events: list = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _task(tid: str, parent: str | None = None) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", parent_task_id=parent)


def _session(root: str = "root") -> Session:
    return Session(id="s1", user_prompt="", status="RUNNING", root_agent_id=root)


async def test_task_started_emitted_once_by_tm_with_agent_id() -> None:
    """每次派发恰好一条 TaskStarted，由 TM 发、payload 带装配的 agent id（99edd41 双发不回归）。"""
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm.set_session(_session())
    t = _task("A")
    tm.register_task(t)
    tm.set_runner(StubRunner(tm))

    await tm._run_task("A")

    started = [e for e in bus.events if e.type == EventType.TASK_STARTED]
    assert len(started) == 1
    assert started[0].task_id == "A"
    assert started[0].payload["assigned_agent_id"] == "root"
    assert t.assigned_agent_id == "root"          # TM 回填
    assert t.started_at is not None


async def test_assembly_failure_no_task_started_and_labeled_requeue() -> None:
    """装配抛错：不发 TaskStarted（无幽灵 ACTIVE），走重试且 reason=assembly_failure。"""
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0            # drain 空转，不真正重跑
    tm.set_session(_session())
    tm.register_task(_task("A"))

    class _Boom(StubRunner):
        async def assemble(self, task_id: str):
            raise RuntimeError("template gone")

    tm.set_runner(_Boom(tm))
    await tm._run_task("A")

    types = [e.type for e in bus.events]
    assert EventType.TASK_STARTED not in types
    requeued = [e for e in bus.events if e.type == EventType.TASK_REQUEUED]
    assert requeued and requeued[0].payload["reason"] == "assembly_failure"


async def test_running_serial_key_uses_real_binding_agent_id() -> None:
    """在跑任务的串行键 = 装配返回的真实 agent id，而非预测值。"""
    tm = TaskManager(session_id="s1", max_concurrent=4)
    tm.set_session(_session())
    release = asyncio.Event()

    class _RealId(StubRunner):
        async def assemble(self, task_id: str):
            return AgentBinding(agent_id="agt_real")

        async def execute(self, binding, task_id: str) -> None:
            await release.wait()

    tm.set_runner(_RealId(tm))
    await tm.push_task(_task("A"))
    await tm.drain()
    for _ in range(10):               # 让 _run_task 协程跑到 execute 阻塞点
        if "A" in tm._running_agents:
            break
        await asyncio.sleep(0)

    assert tm._running_agents.get("A") == "agt_real"
    release.set()
```

- [ ] **Step 3: 跑新测试确认失败**

Run: `uv run pytest tests/unit/test_two_phase_dispatch.py -v`
Expected: FAIL（`set_runner` 收到对象后 `self._runner(...)` 不可调用 / 无 `_running_agents` 属性）

- [ ] **Step 4: 改 TaskManager**

`src/ctx_weft/core/orchestrator/task_manager.py` 六处：

**(a)** 头部：删 `TaskRunner = Callable[[str, str], Coroutine[Any, Any, None]]` 别名（连同其注释行），加导入：

```python
from ctx_weft.core.orchestrator.task_runner import AgentBinding, TaskRunner, effective_agent_id
```

**(b)** `__init__` 加一行（`self._running_tasks: set[str] = set()` 之后）：

```python
        # 派发后登记的「真实执行 agent id」（task_id → binding.agent_id）——
        # 同 agent 串行判定对在跑任务用真值，只有队列候选才走 effective_agent_id 预测。
        self._running_agents: dict[str, str] = {}
```

**(c)** `_effective_agent` 整个替换为委托（docstring 缩短，细节归纯函数）：

```python
    def _effective_agent(self, task: "Task | None") -> str:
        """任务实际执行所在的 agent id——同 agent 串行判定的键（单一真相见 effective_agent_id）。"""
        root = self._session.root_agent_id if self._session else ""
        return effective_agent_id(task, root)
```

**(d)** `drain()` 中 busy_agents 改为真值优先：

```python
                busy_agents = {
                    self._running_agents.get(tid) or self._effective_agent(self._tasks.get(tid))
                    for tid in self._running_tasks
                }
```

**(e)** `_run_task` 整个方法替换（两阶段；原 TASK_STARTED 契约注释块删除）：

```python
    async def _run_task(self, task_id: str) -> None:
        assert self._runner is not None
        task = self._tasks.get(task_id)
        if task is not None:
            task.status = "ACTIVE"
            task.actor_done = False

        # ── 阶段 1：装配（assemble）────────────────────────────────────────
        # 失败发生在 TASK_STARTED 之前 → 投影不会出现幽灵 ACTIVE；与执行失败
        # 共用重试路径，但 TASK_REQUEUED.reason=assembly_failure 可区分。
        try:
            binding = await self._runner.assemble(task_id)
        except Exception as e:
            logger.exception("Task %s assembly failed: %s", task_id, e)
            await self._handle_task_failure(
                task_id, error=str(e), exc=e, reason="assembly_failure",
            )
            return
        if binding is None:
            # task 已不存在（装配空转）：镜像旧行为（runner 首行 get_task None 即返回）
            await self.on_task_finished(task_id, status="FINISHED")
            return

        # 回填「真正用于执行的 agent id」+ 真实启动时刻；TASK_STARTED 由 TM 发，
        # 每次派发（含 retry/resume）恰好一条（reducer 只落非空 id；见 spec/07）。
        if task is not None:
            task.assigned_agent_id = binding.agent_id
            task.started_at = now_utc()
        self._running_agents[task_id] = binding.agent_id
        await self._emit(EventType.TASK_STARTED, task_id=task_id,
                         payload={"assigned_agent_id": binding.agent_id})

        # ── 阶段 2：执行（execute）────────────────────────────────────────
        try:
            try:
                await self._runner.execute(binding, task_id)
            except BaseException:
                # cancel(CancelledError) / 异常退出：丢弃未 flush 的缓冲，
                # 防止泄漏或日后 resume 时被误入队。再交回外层原有处理。
                self._staged.pop(task_id, None)
                raise
            # runner 正常跑完才把本轮 spawn 的子任务入队（“一轮跑完之后 push”）
            await self._flush_staged(task_id)
            task = self._tasks.get(task_id)
            if task and task.status == "SUSPENDED":
                # 子任务已由 control tool 推入队列；parent 等待所有子任务完成后
                # 由 _try_resume_parent 重新入队，此处只需移出 running set 并 drain。
                # 注：LLM 故障中断（_run_loop except LLMOutageError）也置 SUSPENDED 到此挂起，无子任务，待 /resume 由 restore 重排。
                async with self._lock:
                    self._running_tasks.discard(task_id)
                    self._running_agents.pop(task_id, None)
                    self._queue.unmark_running(task_id)
                await self.drain()
                # 整个会话因 park/suspend 进入空闲（无在跑任务、无待派子任务）→ 通知 runtime 回收
                # 按 run 计的控制信号。注意是 is_done（而非"本 task 挂起"）：父等子时子仍在跑，
                # is_done 为 False、不触发，待子完成 resume 父；只有全会话静止才算空闲挂起。
                if self.is_done():
                    await self._fire_session_idle()
                return
            if task and task.status == "PENDING":
                # Observer 判 retry（本轮未完成，含机械退出）：重新入队（retry_count 已在 finalize +1）。
                async with self._lock:
                    self._running_tasks.discard(task_id)
                    self._running_agents.pop(task_id, None)
                    self._queue.unmark_running(task_id)
                    self._queue.push(QueueEntry(task_id=task_id, session_id=self._session_id))
                await self.drain()
                return
            # 使用 task 的实际终态，避免将 FAILED/CANCELED 覆盖为 FINISHED
            final_status: TaskStatus = "FINISHED"
            if task and task.status in ("FAILED", "CANCELED"):
                final_status = task.status
            await self.on_task_finished(task_id, status=final_status)
        except Exception as e:
            if getattr(e, "retriable", False):
                logger.warning("Task %s failed (retriable): %s", task_id, e)
            else:
                logger.exception("Task %s failed: %s", task_id, e)
            await self._handle_task_failure(task_id, error=str(e), exc=e)
```

**(f)** `_handle_task_failure` 加 `reason` kwarg 并用于 payload、清 `_running_agents`：

签名改为：

```python
    async def _handle_task_failure(
        self, task_id: str, error: str = "", exc: BaseException | None = None,
        reason: str = "run_failure_retry",
    ) -> None:
```

retry 分支两处改动——锁内加一行清理、payload 用 reason：

```python
            async with self._lock:
                self._running_tasks.discard(task_id)
                self._running_agents.pop(task_id, None)
                self._queue.unmark_running(task_id)  # 清除 queue._running，使 pop() 能再次调度
                entry = QueueEntry(task_id=task_id, session_id=self._session_id)
                self._queue.push(entry)
            # 重排落事件：使投影从 ACTIVE 回到 PENDING；进程在重试间隙崩溃时
            # restore 据 PENDING 重排（而非把停留 ACTIVE 的任务误当成可恢复后重跑）。
            await self._emit(EventType.TASK_REQUEUED, task_id=task_id, payload={
                "reason": reason,
                "retry_count": task.retry_count,
            })
```

**(g)** `on_task_finished` 锁内 `self._running_tasks.discard(task_id)` 后加：

```python
            self._running_agents.pop(task_id, None)
```

（`set_runner` 签名 `def set_runner(self, runner: TaskRunner) -> None:` 字面不变，类型含义随协议更新。）

- [ ] **Step 5: 跑新测试确认通过**

Run: `uv run pytest tests/unit/test_two_phase_dispatch.py tests/unit/test_effective_agent_id.py -v`
Expected: 全 PASS

- [ ] **Step 6: 更新 5 个存量测试文件的 stub**

机械变换规则（对 grep `set_runner` 的每一处）：

1. 文件头加导入 `from tests.unit._stub_runner import StubRunner`。
2. `tm.set_runner(runner)`（runner 是 `async def runner(sid, tid)` 旧式协程）→ `tm.set_runner(StubRunner(tm, runner))`。
3. `tm.set_runner(_noop_runner)` → `tm.set_runner(StubRunner(tm, _noop_runner))`（`_noop_runner` 本体保留）。
4. `test_task_manager_cancel_all.py:37` 的 `tm.set_runner(lambda sid, tid: None)` → `tm.set_runner(StubRunner(tm))`。
5. **特例** `test_task_scheduling.py::test_run_task_started_via_runner_not_created`（119-136 行）整个重写——契约反转，TM 现在自己发：

```python
async def test_run_task_started_by_tm_exactly_once() -> None:
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    parent = _task("P")
    tm.register_task(parent)
    tm.set_runner(StubRunner(tm))

    await tm._run_task("P")

    types = [e.type for e in bus.events]
    # 每次派发恰好一条 TaskStarted（由 TM 在 assemble 后发），不发 TaskCreated（创建由 push_task 负责）
    assert types.count("TaskStarted") == 1
    assert "TaskCreated" not in types
```

6. **特例** `test_same_agent_serialization.py`：`_session()` 无 `set_session` 的用例（若有 runner 却没 session）不受影响——StubRunner 对 `tm.session is None` 用 `root=""`，`effective_agent_id` 退 creator/per-task token，与原 `_effective_agent` 预测一致。该文件 2)-组测试直接调 `tm._effective_agent`，不用改。

- [ ] **Step 7: 跑受影响单测**

Run: `uv run pytest tests/unit/test_task_scheduling.py tests/unit/test_same_agent_serialization.py tests/unit/test_superseded_task_manager.py tests/unit/test_hitl_recovery.py tests/unit/test_task_manager_cancel_all.py tests/unit/test_effective_agent_creator.py -v`
Expected: 全 PASS（`test_effective_agent_creator.py` 是既有未跟踪新文件，行为不变应绿）

- [ ] **Step 8: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/_stub_runner.py tests/unit/test_two_phase_dispatch.py tests/unit/test_task_scheduling.py tests/unit/test_same_agent_serialization.py tests/unit/test_superseded_task_manager.py tests/unit/test_hitl_recovery.py tests/unit/test_task_manager_cancel_all.py
git commit -m "refactor(task-manager): 两阶段派发——TASK_STARTED 归 TM、串行键真值化、装配失败标注"
```

---

### Task 3: runtime 闭包 runner 改写为 _SessionTaskRunner 类

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（`:50` 导入、`:773-903` `_make_task_runner` 整段）

**Interfaces:**
- Consumes: Task 1 `AgentBinding`/`effective_agent_id`；Task 2 后 TM 只调 `assemble`/`execute`。
- Produces: `CtxWeftRuntime._make_task_runner(**同现有 kwargs**) -> _SessionTaskRunner`（两个调用点 `runtime.py:695`、`runtime.py:1038` 不改）。

- [ ] **Step 1: 改导入（runtime.py:50）**

```python
from ctx_weft.core.orchestrator.task_manager import TaskManager, _task_payload
from ctx_weft.core.orchestrator.task_runner import AgentBinding, TaskRunner, effective_agent_id
```

- [ ] **Step 2: `_make_task_runner` 方法体改薄工厂（签名与两个调用点不变）**

```python
    def _make_task_runner(
        self,
        *,
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: LifecycleManager,
        memory: MemoryProvider,
        llm_account: str | None,
        llm_model: str | None,
        task_manager: TaskManager,
        cancel_token: CancelToken,
        pause_token: "PauseToken | None" = None,
        default_run_id: str,
        handle: "RunHandle | None" = None,
        pre_resolved_agents: dict[str, "Agent"] | None = None,
    ) -> "_SessionTaskRunner":
        """构造本 session/run 的两阶段 runner（原闭包工厂的显式化）。"""
        return _SessionTaskRunner(
            runtime=self, session=session, template=template, template_id=template_id,
            lm=lm, memory=memory, llm_account=llm_account, llm_model=llm_model,
            task_manager=task_manager, cancel_token=cancel_token, pause_token=pause_token,
            default_run_id=default_run_id, handle=handle,
            pre_resolved_agents=pre_resolved_agents,
        )
```

- [ ] **Step 3: 在 runtime.py 模块末尾（`_execute_task` 所在类之后）加 `_SessionTaskRunner` 类**

原闭包逐段搬迁：`_default_agent`/`_reconcile_or`/`_resolve` 变方法，`_resolved_agents` 变实例属性；`run_task` 中「回填 + TASK_STARTED」已归 TM（Task 2），其余进 `execute`。

```python
class _SessionTaskRunner:
    """两阶段 TaskRunner（每个 owner-TM 一个实例）：assemble 装配执行 agent，execute 驱动 step loop。

    原 _make_task_runner 闭包的显式化：闭包捕获 → 实例字段；_resolved_agents
    闭包缓存 → 实例属性（恢复播种 = 构造参数 pre_resolved_agents）。
    assigned_agent_id 回填 / started_at / TASK_STARTED 均归 TaskManager（两阶段契约）。
    """

    def __init__(
        self,
        *,
        runtime: "CtxWeftRuntime",
        session: Session,
        template: "AgentTemplate",
        template_id: str,
        lm: LifecycleManager,
        memory: MemoryProvider,
        llm_account: str | None,
        llm_model: str | None,
        task_manager: TaskManager,
        cancel_token: CancelToken,
        pause_token: "PauseToken | None" = None,
        default_run_id: str,
        handle: "RunHandle | None" = None,
        pre_resolved_agents: dict[str, "Agent"] | None = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._template = template
        self._template_id = template_id
        self._lm = lm
        self._memory = memory
        self._llm_account = llm_account
        self._llm_model = llm_model
        self._task_manager = task_manager
        self._cancel_token = cancel_token
        self._pause_token = pause_token
        self._default_run_id = default_run_id
        self._handle = handle
        self._resolved_agents: dict[str, Agent] = dict(pre_resolved_agents or {})

    # ── 阶段 1：装配 ─────────────────────────────────────────────────────────

    async def assemble(self, task_id: str) -> "AgentBinding | None":
        """决定并实例化执行 agent + memory 预备 + reconcile 探测（原 _resolve）。"""
        import dataclasses as _dc

        t = self._task_manager.get_task(task_id)
        if t is None:
            return None
        sess_id = self._session.id
        tenant_id = self._session.tenant_id

        match t.settings:
            case NormalTaskSettings(use_subagent=True) as s:
                ctx = ProviderContext(session_id=sess_id, tenant_id=tenant_id)
                sub_tmpl_id = (
                    await self._runtime._resolve_subagent_template(s.subagent_template, ctx)
                    if s.subagent_template else ""
                ) or self._template_id
                parent_agent = self._resolved_agents.get(t.creator_agent_id) if t.creator_agent_id else None
                agent, tmpl = await self._lm.instantiate_agent(
                    template_id=sub_tmpl_id, session_id=sess_id, tenant_id=tenant_id,
                    parent_agent=parent_agent, ctx=ctx,
                    existing_agent_id=t.assigned_agent_id or None,
                )
                agent = _dc.replace(agent, loop_guard=LoopGuard(
                    context_limit=self._session.context_limit,
                    reserved_output_tokens=self._session.reserved_output_tokens,
                ))
                t.assigned_agent_id = agent.id
                await _flush_tracking_memory(agent, t, self._task_manager, self._memory, sess_id, tenant_id)
                if s.inherit_memory and not t.user_prompt_in_memory:
                    # Parented sub-tasks copy from their parent; a root turn dispatched
                    # straight to a sub-agent has no parent_task_id, so fall back to the
                    # previous root task (else its sub-agent starts blank — no session memory).
                    src_t = (
                        self._task_manager.get_task(t.parent_task_id) if t.parent_task_id
                        else _latest_prior_root_task(self._task_manager, t)
                    )
                    if src_t:
                        await _copy_memory_for_inherit(
                            parent_task=src_t, child_task=t, sub_agent=agent,
                            memory=self._memory, session_id=sess_id, tenant_id=tenant_id,
                        )
                initial = await self._reconcile_or(t, agent, "prepare")
                self._resolved_agents[agent.id] = agent
                return AgentBinding(agent_id=agent.id, agent=agent, template=tmpl,
                                    initial_step=initial, run_id=generate_id("run"))

            case _:
                # 非 subagent 任务在**创建者**的 agent scope 上跑（延续创建者对话）；
                # scope 键与串行判定共用 effective_agent_id 单一真相。
                agent = self._default_agent(
                    effective_agent_id(t, self._session.root_agent_id or ""),
                )
                await _flush_tracking_memory(agent, t, self._task_manager, self._memory, sess_id, tenant_id)
                initial = await self._reconcile_or(t, agent, "prepare")
                self._resolved_agents[agent.id] = agent
                return AgentBinding(agent_id=agent.id, agent=agent, template=self._template,
                                    initial_step=initial, run_id=self._default_run_id)

    # ── 阶段 2：执行 ─────────────────────────────────────────────────────────

    async def execute(self, binding: "AgentBinding", task_id: str) -> None:
        t = self._task_manager.get_task(task_id)
        if t is None:
            return
        # 单 owner 架构 seam：本轮执行资源在**派发时**从可变的 per-session 源读取，而非
        # 构造时捕获，这样"复用活 owner"续跑时能用上新一轮的 model / cancel-pause token
        # （"参数是消息，不焊进 owner"）。均带实例字段兜底 → start_session / 崩溃重建路径行为不变。
        s, _ = await self._runtime._execute_task(
            session=self._session,
            task=t,
            agent=binding.agent,
            template=binding.template,
            run_id=binding.run_id,
            memory=self._memory,
            llm_account=self._session.llm_provider or self._llm_account,
            llm_model=self._session.llm_model or self._llm_model,
            initial_step=binding.initial_step,
            task_manager=self._task_manager,
            cancel_token=self._runtime._cancel_tokens.get(self._session.id) or self._cancel_token,
            pause_token=self._runtime._pause_tokens.get(self._session.id) or self._pause_token,
        )
        if self._handle is not None and s is not None:
            self._handle._state = s

    # ── helpers（原闭包内嵌函数）───────────────────────────────────────────────

    def _default_agent(self, agent_id: str | None = None) -> Agent:
        return Agent(
            id=agent_id or generate_id("agt"),
            session_id=self._session.id,
            template_id=self._template.id,
            template_version=self._template.version,
            status="RUNNING",
            tenant_id=self._session.tenant_id,
            loop_guard=LoopGuard(
                context_limit=self._session.context_limit,
                reserved_output_tokens=self._session.reserved_output_tokens,
            ),
            memory_config=self._template.memory_config,
            loop_config=self._template.loop_config,
            created_at=now_utc(),
        )

    async def _reconcile_or(self, t: "Task", agent: "Agent", base: str) -> str:
        """base initial_step；若该 task 最近 assistant turn 有 dangling tool_call → reconcile。"""
        from ctx_weft.protocols.context import ProviderContext as _PCtx
        from ctx_weft.protocols.memory import MemoryScope as _Scope
        sess_id = self._session.id
        scope = _Scope(session_id=sess_id, task_id=t.id, agent_id=agent.id)
        pctx = _PCtx(session_id=sess_id, tenant_id=self._session.tenant_id,
                     task_id=t.id, agent_id=agent.id)
        if await _task_has_dangling_tool_call(self._memory, scope, pctx):
            return "reconcile"
        return base
```

删除原闭包版 `_make_task_runner` 体内的全部内容（`_default_agent`/`_reconcile_or`/`_resolve`/`run_task` 内嵌函数）。注意保留模块级 helpers（`_flush_tracking_memory`、`_copy_memory_for_inherit`、`_latest_prior_root_task`、`_task_has_dangling_tool_call`）原样不动。

- [ ] **Step 4: 跑 orchestrator/runtime 相关切片**

Run: `uv run pytest ctx-weft/tests -k "runtime or scheduling or serialization or superseded or hitl or recovery or delegate or resume or outage or interrupt" -p no:warnings`
Expected: 全 PASS（integration 测试走真实 runner，是本 Task 的回归网）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/runtime.py
git commit -m "refactor(runtime): _make_task_runner 闭包改写为 _SessionTaskRunner 两阶段类"
```

---

### Task 4: 全量回归 + 收尾

**Files:**
- 无新改动（只验证；如有红修复后再提交）

- [ ] **Step 1: core 全量**

Run: `uv run pytest ctx-weft/tests -p no:warnings`
Expected: 全 PASS（允许 ripgrep skip 1）

- [ ] **Step 2: host 全量（确认零影响）**

Run: `uv run pytest tests -p no:warnings`
Expected: 全 PASS（已知 flaky `test_postgres_snapshot_prune_keeps_latest_n` 若红，单跑确认即可）

- [ ] **Step 3: 残留检查**

Run: `git grep -n "_resolve(" ctx-weft/src` 与 `git grep -n "runner 必须发 TASK_STARTED" ctx-weft`
Expected: 前者只剩 `_resolve_llm`/`_resolve_subagent_template`；后者 0 命中（旧契约注释已清）。

- [ ] **Step 4: 收尾**

使用 superpowers:verification-before-completion 确认证据，然后 superpowers:finishing-a-development-branch（合回 master）。
```
