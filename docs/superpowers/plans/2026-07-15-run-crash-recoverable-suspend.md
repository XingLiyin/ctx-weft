# 运行层崩溃改为可恢复挂起（非终态）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 运行层崩溃（异常退出）的任务不再落终态 FAILED，而是挂起（SUSPENDED + 会话 INTERRUPTED）等 `/resume` 恢复重跑；真失败只保留 observer（LLM）判 fail 一条路（它闭合胶囊、回传父亲）。恢复入口切换模型时同步会话窗口参数，使 CONTEXT_OVERFLOW 挂起可以换大窗口模型续跑。

**Architecture:** 复用既有的「可恢复中断」形状（LLMOutageError 路径 = SUSPENDED + SessionStatusChanged(INTERRUPTED)，`/resume` 时 `restore()` 据非终态重排）。改动集中三处：① `TaskManager._handle_task_failure` 的两个终态分支改为挂起（新方法 `_suspend_task_interrupted`，取代 `_emit_task_failed`）；② `on_task_finished` 会话终结判定新增「有 SUSPENDED 任务 → 空闲不终结」守卫；③ `CtxWeftRuntime` 恢复路径在 llm 覆盖时用 `_sync_session_llm_window` 对齐 context_limit / reserved_output_tokens。`ContextOverflowError` 不再特判：`retriable=False` 天然跳过重试直接挂起，错误码经 TASK_SUSPENDED.payload.error_code 与会话 INTERRUPTED.reason 抵达 host。

**Tech Stack:** Python 3.11+，pytest（`pytestmark = pytest.mark.asyncio` 风格），dataclasses，事件溯源投影（`TASK_STATUS_BY_EVENT`）。

## Global Constraints

- 语义红线：**任何运行层异常路径都不得再发 `TASK_FAILED`、不得触碰 `session.failure_counter`**；`TASK_FAILED` 只能来自 FinalizeStep（observer 判 fail / retry 降级 fail）。
- 投影对齐红线：task 状态投影只认 TASK_* 事件（`TASK_STATUS_BY_EVENT`，events/types.py:181）。挂起必须发 `TASK_SUSPENDED`，否则任务在投影停留 ACTIVE、被 restore 误判（这正是旧 `_emit_task_failed` 存在的原因，删除它时必须以 `TASK_SUSPENDED` 顶上）。
- 自动重试保留：retriable 异常在 `retry_count < max_retries` 内仍原地重排（`TASK_REQUEUED`）；变化只在「耗尽/不可重试后的去向」——从终态 FAILED 改为挂起。
- 事件白名单：所有事件仍走 `TaskManager._emit`（它校验 `EVENT_TYPES`）。`SESSION_STATUS_CHANGED(INTERRUPTED)` 的 payload 形状对齐 `runtime._emit_session_interrupted`（`{"new_status": "INTERRUPTED", "reason": ...}`，runtime.py:1411）。
- 注释/文档风格：与现有代码一致（中文注释、说明"为什么"）。测试文件顶部要有中文 docstring 说明覆盖点。
- 运行测试统一用 `uv run pytest`（Windows / PowerShell 环境，本仓库用 uv 管理）。
- 提交信息风格对齐 git log（`fix(scope): 中文描述`），每个 Task 一个 commit，结尾加 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

---

### Task 1: TaskManager 崩溃挂起路径（`_suspend_task_interrupted` 取代终态 FAILED）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:512-570`（`_handle_task_failure` + 删除 `_emit_task_failed`、新增 `_suspend_task_interrupted`）
- Modify: `tests/unit/test_task_scheduling.py:155-179`（重写 `test_run_layer_failure_emits_task_failed`）
- Create: `tests/unit/test_run_crash_suspend.py`

**Interfaces:**
- Produces: `async TaskManager._suspend_task_interrupted(task_id: str, error: str, exc: BaseException | None) -> None`（Task 2/3 的测试会间接触发它）。
- 事件契约：崩溃终局发 `TASK_SUSPENDED`（payload: `reason="run_crash"`, `error_code`, `error_message`, `retry_count`）+ `SESSION_STATUS_CHANGED`（payload: `new_status="INTERRUPTED"`, `reason=<error_code>`）；**不发** `TASK_FAILED`。
- 内存状态：task `SUSPENDED` + `error`/`error_code` 回填；session（若注入）`RUNNING → INTERRUPTED`；`failure_counter` 不变。

- [ ] **Step 1: 写失败测试（新文件）**

创建 `tests/unit/test_run_crash_suspend.py`：

```python
"""运行层崩溃 = 可恢复中断（挂起等 /resume），不是失败。

覆盖：
1. 不可重试异常 → TASK_SUSPENDED + SESSION_STATUS_CHANGED(INTERRUPTED)，绝不发 TASK_FAILED。
2. 可重试异常耗尽 max_retries → 同上（不再降级终态 FAILED）。
3. 崩溃挂起不触碰 session.failure_counter（真失败只有 observer 判 fail 一条路）。
4. ContextOverflowError：retriable=False → 不重试直接挂起，error_code/reason=CONTEXT_OVERFLOW
   区分性地抵达事件流（host 据此提示换更大窗口的模型恢复）。
"""

from __future__ import annotations

from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.core.events.types import EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from tests.unit._stub_runner import StubRunner


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


class _NonRetriable(Exception):
    retriable = False
    code = "LLM_AUTH_FAILED"


class _Retriable(Exception):
    retriable = True


def _tm(bus: _CapturingBus) -> tuple[TaskManager, Session, Task]:
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0  # drain 空转：不真正派发
    session = Session(id="s1", user_prompt="", status="RUNNING")
    tm.set_session(session)
    tm.set_runner(StubRunner(tm))
    t = Task(id="A", session_id="s1", status="ACTIVE")
    tm.register_task(t)
    return tm, session, t


def _types(bus: _CapturingBus) -> list:
    return [e.type for e in bus.events]


async def test_non_retriable_crash_suspends_not_fails() -> None:
    bus = _CapturingBus()
    tm, session, t = _tm(bus)

    await tm._handle_task_failure("A", error="401 unauthorized", exc=_NonRetriable("boom"))

    assert EventType.TASK_FAILED not in _types(bus)
    suspended = [e for e in bus.events if e.type == EventType.TASK_SUSPENDED]
    assert suspended and suspended[0].task_id == "A"
    assert suspended[0].payload["error_code"] == "LLM_AUTH_FAILED"
    assert suspended[0].payload["error_message"] == "401 unauthorized"
    interrupted = [
        e for e in bus.events
        if e.type == EventType.SESSION_STATUS_CHANGED
        and e.payload.get("new_status") == "INTERRUPTED"
    ]
    assert interrupted and interrupted[0].payload.get("reason") == "LLM_AUTH_FAILED"
    assert t.status == "SUSPENDED"
    assert t.error == "401 unauthorized"
    assert session.status == "INTERRUPTED"


async def test_retry_exhausted_suspends_not_fails() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    t.retry_count = t.max_retries  # 自动重试已耗尽

    await tm._handle_task_failure("A", error="transient", exc=_Retriable("boom"))

    assert EventType.TASK_FAILED not in _types(bus)
    assert EventType.TASK_REQUEUED not in _types(bus)  # 耗尽后不再重排
    assert EventType.TASK_SUSPENDED in _types(bus)
    assert t.status == "SUSPENDED"


async def test_run_crash_does_not_touch_failure_counter() -> None:
    bus = _CapturingBus()
    tm, session, _t = _tm(bus)

    await tm._handle_task_failure("A", error="boom", exc=_NonRetriable("boom"))

    assert session.failure_counter == 0
    assert EventType.SESSION_FINISHED not in _types(bus)


async def test_context_overflow_suspends_without_retry() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    exc = ContextOverflowError(context_limit=100_000, required=171_808,
                               effective_limit=92_000, reserved_output_tokens=8_000)

    await tm._handle_task_failure("A", error=str(exc), exc=exc)

    assert EventType.TASK_REQUEUED not in _types(bus)  # retriable=False：不重试
    assert EventType.TASK_FAILED not in _types(bus)
    suspended = [e for e in bus.events if e.type == EventType.TASK_SUSPENDED]
    assert suspended and suspended[0].payload["error_code"] == "CONTEXT_OVERFLOW"
    interrupted = [
        e for e in bus.events
        if e.type == EventType.SESSION_STATUS_CHANGED
        and e.payload.get("new_status") == "INTERRUPTED"
    ]
    assert interrupted and interrupted[0].payload.get("reason") == "CONTEXT_OVERFLOW"
    assert t.status == "SUSPENDED"
```

- [ ] **Step 2: 跑新测试确认失败**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py -v`
Expected: 4 个测试全部 FAIL（当前实现发 TASK_FAILED、task 落 FAILED）。

- [ ] **Step 3: 实现 `_suspend_task_interrupted` 并改写 `_handle_task_failure`**

在 `task_manager.py`，将 `_handle_task_failure`（512 行起）改为：

```python
    async def _handle_task_failure(
        self, task_id: str, error: str = "", exc: BaseException | None = None,
        reason: str = "run_failure_retry",
    ) -> None:
        """运行层失败：先尝试自动 retry，耗尽或不可重试 → 挂起等恢复（绝不落终态 FAILED）。

        运行层崩溃（异常退出，未经 observer/FinalizeStep）是**可恢复中断**，不是任务失败：
        真失败只有 observer 判 fail 一条路（FinalizeStep 闭合胶囊、发 TaskFailed、回传父亲）。
        exc.retriable=False（如 LLMCallError 401 认证失败、CONTEXT_OVERFLOW）时跳过重试直接挂起，
        避免对确定性错误做无效重试。
        """
        # 不可重试的错误（如认证失败 / 上下文溢出），不重试、直接挂起等恢复
        if exc is not None and not getattr(exc, "retriable", True):
            logger.warning(
                "Task %s non-retriable error (%s), suspending for recovery: %s",
                task_id, type(exc).__name__, error,
            )
            await self._suspend_task_interrupted(task_id, error, exc)
            return

        task = self._tasks.get(task_id)
        if task is not None and task.retry_count < task.max_retries:
            task.retry_count += 1
            task.status = "PENDING"
            task.error = error
            logger.info(
                "Task %s retrying (%d/%d): %s",
                task_id, task.retry_count, task.max_retries, error,
            )
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
            await self.drain()
        else:
            await self._suspend_task_interrupted(task_id, error, exc)
```

将 `_emit_task_failed`（557-570 行）整体替换为：

```python
    async def _suspend_task_interrupted(
        self, task_id: str, error: str, exc: BaseException | None,
    ) -> None:
        """运行层崩溃的终局：挂起等 /resume，**不是失败**。

        置 SUSPENDED（非终态）+ 发 TASK_SUSPENDED——投影只认 TASK_* 事件
        （TASK_STATUS_BY_EVENT），不发则任务停留 ACTIVE、restore 语义错位（此前
        由已删除的 _emit_task_failed 发 TaskFailed 兜这一点，现由本事件顶上）。
        再发 SessionStatusChanged(INTERRUPTED)（形状对齐 runtime._emit_session_interrupted，
        reason=错误码——如 CONTEXT_OVERFLOW，host 据此提示换更大窗口的模型恢复）。
        不发 TASK_FAILED、不增 failure_counter、不闭合胶囊：真失败只有 observer 判 fail
        一条路。恢复由 /resume → restore() 据非终态重排（重排时 retry_count 归零）。
        """
        task = self._tasks.get(task_id)
        error_code = (getattr(exc, "code", None)
                      or (type(exc).__name__ if exc is not None else "RUN_CRASH"))
        if task is not None:
            task.status = "SUSPENDED"
            task.error = error
            task.error_code = error_code
        async with self._lock:
            self._running_tasks.discard(task_id)
            self._running_agents.pop(task_id, None)
            self._queue.unmark_running(task_id)
        if self._session is not None and self._session.status == "RUNNING":
            self._session.status = "INTERRUPTED"
        await self._emit(EventType.TASK_SUSPENDED, task_id=task_id, payload={
            "reason": "run_crash",
            "error_code": error_code,
            "error_message": error,
            "retry_count": task.retry_count if task else 0,
        })
        await self._emit(EventType.SESSION_STATUS_CHANGED, payload={
            "new_status": "INTERRUPTED",
            "reason": error_code,
        })
        # 其它 agent 的排队任务照常派发；全会话静止则通知 runtime 回收 per-run 控制信号
        await self.drain()
        if self.is_done():
            await self._fire_session_idle()
```

- [ ] **Step 4: 重写 `tests/unit/test_task_scheduling.py` 里的旧断言**

把 `test_run_layer_failure_emits_task_failed`（155-179 行）整体替换为：

```python
async def test_run_layer_failure_suspends_not_fails() -> None:
    """运行层失败（非 observer 判定，如 model 名写错）挂起等恢复，不落终态。

    仍须发 TASK_* 事件对齐投影（TASK_STATUS_BY_EVENT 只认 TASK_* 事件）——
    现在是 TaskSuspended：任务在投影为 SUSPENDED，restore 会把它当可恢复任务重排，
    这正是期望语义（真失败只有 observer 判 fail 一条路）。
    """
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(StubRunner(tm, runner))
    t = _task("A")
    tm.register_task(t)

    class _NonRetriable(Exception):
        retriable = False

    await tm._handle_task_failure("A", error="unknown model", exc=_NonRetriable("boom"))

    assert not [e for e in bus.events if e.type == EventType.TASK_FAILED]
    suspended = [e for e in bus.events if e.type == EventType.TASK_SUSPENDED]
    assert suspended, "run-layer failure must emit TaskSuspended (projection alignment)"
    assert suspended[0].task_id == "A"
    assert t.status == "SUSPENDED"
```

- [ ] **Step 5: 跑相关测试确认通过**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py tests/unit/test_task_scheduling.py -v`
Expected: 全部 PASS（含未改动的 `test_retry_emits_task_requeued`——retry 分支行为不变）。

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_run_crash_suspend.py tests/unit/test_task_scheduling.py
git commit -m "feat(task): 运行层崩溃改挂起等恢复——终态 FAILED 只留 observer 判 fail 一条路

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: on_task_finished 挂起守卫（有 SUSPENDED 任务时会话空闲、不终结）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:618-637`（`on_task_finished` 的 is_done 终结块）
- Test: `tests/unit/test_run_crash_suspend.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_suspend_task_interrupted`（制造崩溃挂起态）。
- Produces: 行为契约——存在任何 `status == "SUSPENDED"` 的任务时，`on_task_finished` 的收尾走 `_fire_session_idle()`，不发 `SESSION_FINISHED` / 不写会话终态。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_run_crash_suspend.py` 追加：

```python
async def test_crash_suspended_task_blocks_session_finish() -> None:
    """A 崩溃挂起后 B 正常完成：会话不得终结（等 /resume），否则挂起任务被孤立。

    与 pending-HITL 守卫同理：queue 空、无在跑任务 ≠ 会话完成——中断待恢复的任务
    也是"会话未完"的真相源。合法的"父等子"SUSPENDED 到不了这条守卫：子未终态时
    is_done() 为 False；子全终态时父已被 _try_resume_parent 重排回队列。
    """
    bus = _CapturingBus()
    tm, session, _a = _tm(bus)
    b = Task(id="B", session_id="s1", status="ACTIVE")
    tm.register_task(b)

    await tm._handle_task_failure("A", error="boom", exc=_NonRetriable("boom"))
    await tm.on_task_finished("B", status="FINISHED")

    assert EventType.SESSION_FINISHED not in _types(bus)
    assert session.status == "INTERRUPTED"  # 不被 SUCCEEDED/FAILED 覆盖
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py::test_crash_suspended_task_blocks_session_finish -v`
Expected: FAIL——当前 `on_task_finished` 在 is_done 时直接发 SESSION_FINISHED 并把 session 写成 SUCCEEDED。

- [ ] **Step 3: 实现守卫**

在 `on_task_finished` 的 is_done 块（task_manager.py:626-637），把：

```python
            if self._has_pending_hitl is not None and self._has_pending_hitl():
                await self._fire_session_idle()
            else:
```

改为：

```python
            if self._has_pending_hitl is not None and self._has_pending_hitl():
                await self._fire_session_idle()
            elif any(t.status == "SUSPENDED" for t in self._tasks.values()):
                # 崩溃/LLM 故障挂起（INTERRUPTED）的任务在等 /resume：会话是"中断待恢复"
                # 而非"完成"——绝不发 SESSION_FINISHED 把挂起任务孤立（与 pending-HITL 同理）。
                # 合法的"父等子"SUSPENDED 到不了这里：子未终态时 is_done() 为 False；
                # 子全终态时父已在上方 _try_resume_parent 重排回队列（不再 SUSPENDED）。
                await self._fire_session_idle()
            else:
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_run_crash_suspend.py
git commit -m "fix(task): 有挂起任务时会话收尾走 idle 不终结，防中断任务被 SESSION_FINISHED 孤立

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: _run_loop 撤销 ContextOverflow 终态特判，崩溃预标 SUSPENDED

**Files:**
- Modify: `src/ctx_weft/core/runtime.py:1628-1645`（`_run_loop` 的 `except ContextOverflowError` + `except Exception` 分支）
- Modify: `tests/unit/test_overflow_routing.py`（断言反转）

**Interfaces:**
- Consumes: Task 1 的挂起终局（`_run_loop` re-raise → `_run_task` → `_handle_task_failure`）。
- Produces: 行为契约——`_run_loop` 崩溃分支把 task 预标 `SUSPENDED`（非 FAILED），`RUN_FINISHED.payload.final_status == "SUSPENDED"`；异常照旧 re-raise 交 TaskManager 定夺（retry 分支会翻回 PENDING）。

- [ ] **Step 1: 反转 `tests/unit/test_overflow_routing.py` 的断言**

先读该文件全文；**保留既有 harness（构造 runtime/task、触发装配溢出、收集事件的部分）不动**，把文件顶部 docstring 改为：

```python
"""_run_loop routes ContextOverflowError to recoverable SUSPENDED (interrupted), not terminal FAILED.

溢出不再终态：retriable=False → 不重试、挂起等 /resume；用户换更大窗口的模型恢复
（recover_session 的 llm_model 覆盖 + 窗口参数同步）。错误文案仍随 task.error 抵达 host。
"""
```

把结尾断言块（原 64-78 行：`assert task.status == "FAILED"` 到 `final_status == "FAILED"`）替换为：

```python
    # 溢出 = 可恢复中断：挂起等 /resume，不是终态失败
    assert task.status == "SUSPENDED"
    assert "171808" in task.error or "171,808" in task.error
    # 不发 TaskFailed；TaskSuspended + SessionStatusChanged(INTERRUPTED) 由
    # TaskManager._suspend_task_interrupted 发（tests/unit/test_run_crash_suspend.py 覆盖）
    assert not [e for e in seen if getattr(e, "type", None) == EventType.TASK_FAILED]
    # RUN_FINISHED 反映挂起（可恢复中断），且不再自动重试
    finished = [e for e in seen if getattr(e, "type", None) == EventType.RUN_FINISHED]
    assert finished and finished[-1].payload.get("final_status") == "SUSPENDED"
    assert finished[-1].payload.get("will_retry") is False
```

注意：若原测试对 `SESSION_STATUS_CHANGED(INTERRUPTED)` 断言 `not status_events`（原 75 行）：该事件现在由 TaskManager 发、不在 `_run_loop` 层——若 harness 只驱动 `_run_loop`/`_execute_task`（不经 `TaskManager._run_task`），保留 `assert not status_events` 并把注释改为「INTERRUPTED 由 TaskManager 挂起终局发，不在本层」；若 harness 经 TaskManager 全链路驱动，则改为断言**存在** INTERRUPTED 事件且 `reason == "CONTEXT_OVERFLOW"`。以 harness 实际驱动层级为准（读文件即知）。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_overflow_routing.py -v`
Expected: FAIL（当前 `_run_loop` 把溢出标 FAILED）。

- [ ] **Step 3: 修改 `_run_loop`**

删除整个 `except ContextOverflowError as exc:` 分支（runtime.py:1628-1636），并把紧随其后的 `except Exception as exc:` 分支改为：

```python
        except Exception as exc:
            run_error = exc
            # 运行层崩溃 = 可恢复中断的临时标记（非终态）：re-raise 交 _handle_task_failure
            # 定夺——原地重试（翻回 PENDING）或挂起等 /resume（保持 SUSPENDED + 发事件）。
            # 真失败只有 observer 判 fail 一条路（FinalizeStep 闭合胶囊、回传父亲）。
            # ContextOverflowError 不再特判终态：retriable=False 使其跳过重试直接挂起，
            # 溢出文案随 task.error / TASK_SUSPENDED.error_message 抵达 host（提示换大窗口模型）。
            if task.status not in ("FINISHED", "FAILED", "CANCELED"):
                task.status = "SUSPENDED"
                task.error = str(exc)
            if getattr(exc, "retriable", False):
                logger.warning("_run_loop: task %s failed (retriable): %s", task_id_safe(task), exc)
            else:
                logger.exception("_run_loop: run failed for task %s", task.id)
```

注意：上面 `task_id_safe(task)` 不存在——保持原样用 `task.id`（两处 logger 均与现状一致，只改状态标记与注释）：

```python
            if getattr(exc, "retriable", False):
                logger.warning("_run_loop: task %s failed (retriable): %s", task.id, exc)
            else:
                logger.exception("_run_loop: run failed for task %s", task.id)
```

随后检查 `ContextOverflowError` 在 runtime.py 是否还有其它使用点：`grep -n "ContextOverflowError" src/ctx_weft/core/runtime.py`——若 except 分支删除后仅剩 import，连 import 一并删除；若 compact 等路径仍在用则保留 import。

- [ ] **Step 4: 跑测试确认通过（含既有 outage 回归）**

Run: `uv run pytest tests/unit/test_overflow_routing.py tests/unit/test_run_loop_outage.py tests/unit/test_run_crash_suspend.py -v`
Expected: 全部 PASS（outage 路径未动，仍 SUSPENDED + INTERRUPTED(reason=llm_outage)）。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_overflow_routing.py
git commit -m "feat(loop): 撤销 ContextOverflow 终态特判——溢出与其它崩溃同走挂起恢复路径

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: 恢复重排时 retry_count 归零（restore + resume_task）

**Files:**
- Modify: `src/ctx_weft/core/orchestrator/task_manager.py:171-177`（`restore` 的 SUSPENDED 分支）与 `:785-802`（`resume_task`）
- Test: `tests/unit/test_run_crash_suspend.py`（追加）

**Interfaces:**
- Consumes: Task 1 的挂起态（挂起任务带着 `retry_count == max_retries` 等恢复）。
- Produces: 行为契约——`restore()` 重排 SUSPENDED 任务、`resume_task()` 重排任务时 `retry_count` 归零（否则恢复重跑后第一次崩溃就"已耗尽"、立刻又挂起）。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_run_crash_suspend.py` 追加：

```python
def test_restore_requeues_crash_suspended_with_fresh_retries() -> None:
    """崩溃挂起的任务带着耗尽的 retry_count；restore 重排必须归零，否则恢复后一崩即再挂。"""
    tm = TaskManager(session_id="s1")
    t = Task(id="A", session_id="s1", status="SUSPENDED", retry_count=3)

    tm.restore([t], terminal_ids=set())

    assert t.status == "PENDING"
    assert t.retry_count == 0
    entry = tm._queue.pop()
    assert entry is not None and entry.task_id == "A"


def test_resume_task_resets_retry_count() -> None:
    """就地续跑路径（resume_task）同样归零：挂起期间的旧计数不带入新一轮 attempt。"""
    tm = TaskManager(session_id="s1")
    t = Task(id="A", session_id="s1", status="SUSPENDED", retry_count=2)
    tm.register_task(t)

    tm.resume_task("A")

    assert t.status == "PENDING"
    assert t.retry_count == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py::test_restore_requeues_crash_suspended_with_fresh_retries tests/unit/test_run_crash_suspend.py::test_resume_task_resets_retry_count -v`
Expected: 两个都 FAIL（retry_count 保留旧值）。

- [ ] **Step 3: 实现**

`restore()` 的 SUSPENDED 分支（task_manager.py:171-177）加一行：

```python
            if t.status == "SUSPENDED":
                children = self._children_of.get(t.id, set())
                if all(cid in terminal_ids for cid in children):
                    t.status = "PENDING"
                    t.retry_count = 0  # 崩溃挂起带着耗尽的计数；恢复重跑从零重计
                    self._queue.push(QueueEntry(
                        task_id=t.id, session_id=self._session_id, priority=t.priority,
                    ))
```

`resume_task()`（task_manager.py:799 附近）在 `t.status = "PENDING"` 后加：

```python
        t.status = "PENDING"
        t.retry_count = 0  # 挂起期间的旧计数不带入新一轮 attempt
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_run_crash_suspend.py tests/unit/test_task_scheduling.py -v`
Expected: 全部 PASS（`test_restore_rebuilds_blocked_chain` 不受影响——它的父任务不满足"子全终态"、不重排）。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/orchestrator/task_manager.py tests/unit/test_run_crash_suspend.py
git commit -m "fix(recover): 恢复重排归零 retry_count——挂起任务不带着耗尽计数复跑

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 换模型恢复同步会话窗口参数（context_limit / reserved_output_tokens）

**Files:**
- Modify: `src/ctx_weft/core/runtime.py`（新增 `_sync_session_llm_window`，放在 `_resolve_llm` 之后；`_recover_session_locked` 960-963 行后与 `_resume_in_existing_tm` 1132-1135 行后各加调用）
- Create: `tests/unit/test_resume_model_switch.py`

**Interfaces:**
- Consumes: `CtxWeftRuntime._resolve_llm(llm_account, llm_model) -> LLMClient`（client duck-type 暴露 `context_limit: int`、`output_reserve: int | None`，形状对齐 `run_single_task` runtime.py:675-679）。
- Produces: `CtxWeftRuntime._sync_session_llm_window(session: Session) -> None`；契约——恢复方显式传入 `llm_account`/`llm_model` 覆盖时，`session.context_limit`、`session.reserved_output_tokens` 对齐新解析出的 client（未传覆盖则保持投影原值，不破坏 test_resume_context_limit 的回归约定）。
- 说明：恢复入口的"换模型字段"本身已存在——`recover_session(llm_account=, llm_model=)`、`HitlRequest.resume_llm_account/resume_llm_model`（冷 HITL 应答经 `_resume_after_cold_hitl` 透传）、`SessionStartParams.llm_model`（新轮次）。本 Task 补的是缺口：字段生效但**窗口参数不随新模型**，导致 CONTEXT_OVERFLOW 挂起换大模型恢复后原样再溢出。

- [ ] **Step 1: 写失败测试（新文件）**

创建 `tests/unit/test_resume_model_switch.py`：

```python
"""换模型恢复：llm 覆盖须同步会话窗口参数（context_limit / reserved_output_tokens）。

CONTEXT_OVERFLOW 挂起的会话换更大窗口的模型 /resume：若窗口仍沿用投影里旧模型的值，
重装配会原样再溢出，切换等于无效。未传覆盖时保持投影原值（host 可能刻意配了更小窗口）。
"""

from __future__ import annotations

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.types import RunStateView, SessionView, TaskView
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

pytestmark = pytest.mark.asyncio


class _BigClient:
    context_limit = 400_000
    output_reserve = 16_384


def _runtime(monkeypatch) -> CtxWeftRuntime:
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    runtime = CtxWeftRuntime(template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())
    monkeypatch.setattr(runtime, "_resolve_llm", lambda a=None, m=None: _BigClient())
    return runtime


def test_sync_session_llm_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", llm_model="big-model",
                      context_limit=64_000, reserved_output_tokens=8_192)

    runtime._sync_session_llm_window(session)

    assert session.context_limit == 400_000
    assert session.reserved_output_tokens == 16_384


async def test_resume_in_existing_tm_syncs_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", context_limit=64_000)
    tm = TaskManager(session_id="s1", event_bus=runtime.event_bus)
    tm.set_session(session)
    tm.register_task(Task(id="A", session_id="s1", status="SUSPENDED"))
    monkeypatch.setattr(runtime, "_register_and_drain", lambda s, m: None)

    await runtime._resume_in_existing_tm(
        tm, user_reply=None, llm_account=None, llm_model="big-model", resumed_task_id="A")

    assert session.llm_model == "big-model"
    assert session.context_limit == 400_000


async def test_resume_in_existing_tm_no_override_keeps_window(monkeypatch) -> None:
    """未换模型的恢复不动窗口参数（host 可能刻意配了小于模型窗口的 limit）。"""
    runtime = _runtime(monkeypatch)
    session = Session(id="s1", user_prompt="", status="RUNNING", context_limit=64_000)
    tm = TaskManager(session_id="s1", event_bus=runtime.event_bus)
    tm.set_session(session)
    tm.register_task(Task(id="A", session_id="s1", status="SUSPENDED"))
    monkeypatch.setattr(runtime, "_register_and_drain", lambda s, m: None)

    await runtime._resume_in_existing_tm(
        tm, user_reply=None, llm_account=None, llm_model=None, resumed_task_id="A")

    assert session.context_limit == 64_000


async def test_recover_session_model_switch_syncs_window(monkeypatch) -> None:
    runtime = _runtime(monkeypatch)
    tmpl = make_echo_template()
    view = RunStateView(
        run_id="", session_id="s1", task_id="", agent_id="",
        sessions={"s1": SessionView(id="s1", template_id=tmpl.id,
                                    root_agent_id="agt_root", context_limit=64_000)},
        tasks={"A": TaskView(id="A", session_id="s1", status="SUSPENDED")},
    )

    async def fake_rebuild(store, sid):  # noqa: ANN001
        return view

    monkeypatch.setattr("ctx_weft.core.control.reducers.rebuild_view", fake_rebuild)
    captured: dict = {}
    monkeypatch.setattr(
        runtime, "_register_and_drain",
        lambda session, tm: captured.update(session=session, tm=tm))

    await runtime.recover_session("s1", llm_model="big-model")

    s = captured["session"]
    assert s.llm_model == "big-model"
    assert s.context_limit == 400_000
    assert s.reserved_output_tokens == 16_384
    # 挂起任务被重排（Task 4：retry_count 归零由 restore 保证）
    assert captured["tm"].get_task("A").status == "PENDING"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_resume_model_switch.py -v`
Expected: `test_sync_session_llm_window` FAIL（AttributeError：方法不存在）；两个 sync 测试 FAIL；`no_override` 用例可能 PASS（现状本来不动窗口）。

注意：若 `test_recover_session_model_switch_syncs_window` 因 `rebuild_view` 的 monkeypatch 未生效而失败（`_recover_session_locked` 是函数内 `from ... import rebuild_view`，patch 模块属性 `ctx_weft.core.control.reducers.rebuild_view` 应当生效——函数内 import 在调用时才解析），排查时先确认 patch 目标路径；其余依赖（event_store.read_by_session 返回空列表→无 pending recap；user_reply=None）均为默认可走通路径。

- [ ] **Step 3: 实现**

在 `runtime.py` 的 `_resolve_llm` 方法之后新增：

```python
    def _sync_session_llm_window(self, session: Session) -> None:
        """换模型/账号续跑后，把会话窗口参数对齐新模型（context_limit / reserved_output_tokens）。

        只在恢复方显式传入 llm 覆盖时调用：CONTEXT_OVERFLOW 挂起的会话换更大窗口的模型
        恢复，若窗口仍沿用投影里旧模型的值，重装配会原样再溢出，切换等于无效。
        duck-type 读取（镜像 run_single_task）：桩 client 缺属性时保持会话原值；解析失败
        （如未注册 provider）不阻断恢复，只记日志、沿用原值。
        """
        try:
            llm = self._resolve_llm(session.llm_provider or None, session.llm_model or None)
        except Exception:
            logger.warning(
                "model-switch resume: cannot resolve LLM client for session %s; "
                "keeping projected window params", session.id,
            )
            return
        limit = getattr(llm, "context_limit", None)
        if limit:
            session.context_limit = limit
        reserve = getattr(llm, "output_reserve", None)
        if reserve is not None:
            session.reserved_output_tokens = reserve
```

`_recover_session_locked`（runtime.py:960-963）改为：

```python
        if llm_account is not None:
            session.llm_provider = llm_account
        if llm_model is not None:
            session.llm_model = llm_model
        if llm_account is not None or llm_model is not None:
            # 换模型恢复：窗口参数须随新模型，否则 CONTEXT_OVERFLOW 挂起换大模型也照旧溢出
            self._sync_session_llm_window(session)
```

`_resume_in_existing_tm`（runtime.py:1132-1135）改为：

```python
        if llm_account is not None:
            session.llm_provider = llm_account
        if llm_model is not None:
            session.llm_model = llm_model
        if llm_account is not None or llm_model is not None:
            # 同 recover_session：换模型就地续跑也要对齐窗口参数
            self._sync_session_llm_window(session)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_resume_model_switch.py tests/unit/test_resume_context_limit.py -v`
Expected: 全部 PASS（后者守护"未覆盖时保留投影 context_limit"的既有约定，不得被破坏）。

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/runtime.py tests/unit/test_resume_model_switch.py
git commit -m "feat(recover): 换模型恢复同步窗口参数——CONTEXT_OVERFLOW 挂起可换大窗口模型续跑

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 全量回归与旧语义残余清理

**Files:**
- Test: 全测试套件；可能修改的候选（跑完才能确定，均为断言层面、不改生产代码）：
  - `tests/unit/test_superseded_task_manager.py`（若断言崩溃路径发 TASK_FAILED / 会话 FAILED）
  - `tests/unit/test_pause_abandon_tm.py`、`tests/unit/test_task_manager_cancel_all.py`（若间接经过 `_handle_task_failure`）
  - `tests/integration/test_task_recap_recovery.py:178`（种子事件用 `TASK_FAILED_AT_RUN` 构造**历史** TaskFailed——历史事件重放语义不变，预期无需修改；若失败按新语义重审）
  - `tests/integration/test_outage_resume.py`、`tests/unit/test_outage_interrupt_reason.py`（outage 路径未动，预期通过）

**Interfaces:**
- Consumes: Task 1-5 的全部行为契约。
- Produces: 绿色测试套件；无生产代码改动（若回归暴露生产缺陷，回到对应 Task 修复并补测试）。

- [ ] **Step 1: 全量跑单元测试**

Run: `uv run pytest tests/unit -x -q`
Expected: 全部 PASS。若有失败：先判断该测试断言的是"旧语义"（运行层崩溃→FAILED / failure_counter 计数 / SESSION_FINISHED）还是真回归——前者按 Task 1-4 的新契约反转断言（改法参照 Task 1 Step 4 的样例：TASK_FAILED→TASK_SUSPENDED、FAILED→SUSPENDED、终结→INTERRUPTED/idle），后者回到对应 Task 修实现。

- [ ] **Step 2: 全量跑集成测试**

Run: `uv run pytest tests/integration -x -q`
Expected: 全部 PASS。重点观察 `test_outage_resume.py`（挂起-恢复全链路，应天然覆盖新路径）与 `test_task_recap_recovery.py`（历史 TaskFailed 事件重放仍应为终态 FAILED——事件溯源的既往事件语义不回改）。

- [ ] **Step 3: 修复后复跑直至全绿**

Run: `uv run pytest tests -q`
Expected: 全部 PASS，无 skip 新增。

- [ ] **Step 4: Commit（如有测试修改）**

```bash
git add tests
git commit -m "test: 对齐运行层崩溃挂起新语义的存量断言

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Host 侧适配——/resume 可选模型字段 + 崩溃挂起 run 的 SSE 错误透传

**仓库：`C:\Users\Xing\Documents\codes\IpMasterCoworkPy`（host，非本仓）。本任务只改 host 源码与测试，不依赖新 core wheel（SSE 翻译测试可自造事件）。**

**Files:**
- Modify: `src/ipmastercowork/api/schemas/sessions.py`（新增 `ResumeSessionRequest`，紧跟 `SendMessageRequest` 之后）
- Modify: `src/ipmastercowork/api/sessions.py:309-346`（`resume_session` 端点加可选 body）
- Modify: `src/ipmastercowork/api/models/session.py:461-472` 附近（`RUN_FINISHED` 翻译分支）
- Test: `tests/test_resume_model_switch_api.py`（新建；harness 模式参照 `tests/test_bash_review_mode_endpoints.py` 与既有 sessions 端点测试的写法）

**Interfaces:**
- Consumes: core 新契约——崩溃 run 的 `RUN_FINISHED.payload = {"final_status": "SUSPENDED", "will_retry": False, "error": <非空>, "error_type": ...}`；干净挂起（HITL park / 委派等子 / LLM outage）`error` 为 None。`SESSION_STATUS_CHANGED(INTERRUPTED, reason=<错误码>)` 已由 `entry.interrupt_reason` 透传（models/session.py:443），**无需改动**。
- Produces: `POST /sessions/{id}/resume` 接受可选 JSON body `{"llm_account": str|None, "llm_model": str|None}`（无 body 行为不变=沿用 entry 既有选择）；SSE 新增字段 `task_failed.recoverable: bool`（true = 崩溃挂起、可 /resume 续跑或换模型）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_resume_model_switch_api.py`，覆盖三点（harness 复用仓内既有端点测试的 app/TestClient 构造方式，先读 `tests/test_bash_review_mode_endpoints.py` 依样搭建；若 resume 需要 INTERRUPTED 态 entry，参照 `tests/test_crash_running_recovery_repro.py` 的 entry 构造）：

1. `POST /resume` 带 `{"llm_account": "acc2", "llm_model": "big-model"}` → `entry.llm_account == "acc2"`、`entry.llm_model == "big-model"`，且 `runtime.recover_session` 收到 `llm_account="acc2", llm_model="big-model"`（monkeypatch stub runtime 记录调用参数）。
2. `POST /resume` 无 body → 兼容不 break（entry 原值透传，行为与现状一致）。
3. `SessionEntry` 对 `RUN_FINISHED(final_status="SUSPENDED", error="boom", will_retry=False)` 的翻译产出 `{"type": "task_failed", "recoverable": true, "error": "boom", ...}`；对 `RUN_FINISHED(final_status="SUSPENDED", error=None)`（干净挂起）产出 None（不冒泡）。第 3 点是纯 `SessionEntry.apply`/翻译层单测，直接构造事件对象即可，不需要 HTTP harness。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_resume_model_switch_api.py -v`（在 host 仓根目录）
Expected: FAIL（无 body 解析、无 recoverable 字段）。

- [ ] **Step 3: 实现**

`api/schemas/sessions.py` 在 `SendMessageRequest` 后新增：

```python
class ResumeSessionRequest(BaseModel):
    """POST /resume 的可选 body：恢复时切换 LLM（如 CONTEXT_OVERFLOW 中断后换大窗口模型续跑）。

    缺省/空 body = 沿用会话既有选择（向后兼容）。语义与 send_message 略异：这里"未传"是
    "不改"，不做 null→默认账号 的重置（恢复动作不应顺带改走默认账号）。
    """
    llm_account: str | None = None
    llm_model: str | None = None
```

`api/sessions.py` 的 `resume_session`：

```python
@router.post("/{session_id}/resume", response_model=dict)
async def resume_session(
    session_id: str,
    req: ResumeSessionRequest | None = None,
    runtime=Depends(deps.get_runtime),
) -> dict:
```

并在 `entry.status` 闸门之后、`entry.status = "RUNNING"` 之前插入：

```python
    # 恢复时切换模型（可选）：写回 entry（会话记忆当前选择），随下方 recover_session 生效；
    # core 侧会据非 None 覆盖同步 context_limit/reserved_output_tokens（换大窗口模型解溢出）。
    if req is not None:
        if req.llm_account:
            entry.llm_account = req.llm_account
            entry.llm_model = req.llm_model or None
        elif req.llm_model:
            entry.llm_model = req.llm_model
```

（`recover_session` 调用处不变——它已经透传 `entry.llm_account/llm_model`。同文件顶部 import `ResumeSessionRequest`，与 `SendMessageRequest` 同一 import 行。）

`api/models/session.py` 的 `RUN_FINISHED` 分支改为：

```python
        if t == EventType.RUN_FINISHED:
            will_retry = p.get("will_retry", False)
            final_status = p.get("final_status")
            # 崩溃挂起（core 新语义：运行层崩溃不再终态 FAILED，final_status=SUSPENDED 且带 error）：
            # 错误文案仍要冒泡给用户，并标 recoverable 提示可 /resume（可换模型）续跑。
            # 干净挂起（HITL park / 委派等子 / LLM outage）error 为 None，不冒泡。
            crashed_suspend = final_status == "SUSPENDED" and bool(p.get("error"))
            if final_status == "FAILED" or will_retry or crashed_suspend:
                self.updated_at = _now()
                return json.dumps({
                    "type": "task_failed",
                    "error": p.get("error") or "Task failed",
                    "error_type": p.get("error_type", ""),
                    "will_retry": will_retry,
                    "recoverable": crashed_suspend,
                    "created_at": ts,
                })
```

（保留该分支原有的其余字段/结构——先读原文件再改，原 json 里若有本计划未列出的字段一律保留。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_resume_model_switch_api.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit（host 仓）**

```bash
git add src/ipmastercowork/api/schemas/sessions.py src/ipmastercowork/api/sessions.py src/ipmastercowork/api/models/session.py tests/test_resume_model_switch_api.py
git commit -m "feat(api): /resume 可选模型字段 + 崩溃挂起 run 的错误冒泡（recoverable 标记）

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Re-vendor core wheel + host 全量回归

**仓库：host（`C:\Users\Xing\Documents\codes\IpMasterCoworkPy`）。前置：core Task 1-6 全部完成。**

**Files:**
- Modify: `vendor/ctx_weft-*.whl`、`uv.lock`（由 revendor 脚本产生）
- Test: host 全测试套件；可能修改的候选（断言层面）：`tests/test_crash_running_recovery_repro.py` 及其它编码「运行层崩溃→FAILED」旧语义的测试

**Interfaces:**
- Consumes: core Task 1-6 的全部行为契约（TASK_SUSPENDED / INTERRUPTED(reason=错误码) / RUN_FINISHED(final_status=SUSPENDED)）。
- Produces: host 跑在新 core 上全绿。

- [ ] **Step 1: 重打包并 vendor core**

Run（host 仓根目录，PowerShell）: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\revendor-core.ps1`
Expected: 脚本四步全 [OK]（build wheel → copy vendor → uv lock → uv sync + import 验证）。

- [ ] **Step 2: host 全量测试**

Run: `uv run pytest tests -q`
Expected: 全部 PASS。失败项判别标准同 core Task 6：断言"崩溃→FAILED / TASK_FAILED / 会话 FAILED 终态"的测试按新语义反转（FAILED→SUSPENDED、终结→INTERRUPTED、TASK_FAILED→TASK_SUSPENDED）；真回归则回到对应 core/host Task 修复。

- [ ] **Step 3: Commit（host 仓）**

```bash
git add vendor uv.lock pyproject.toml tests
git commit -m "chore(vendor): 升级 core——运行层崩溃改挂起恢复语义 + 对齐存量测试断言

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## v2 追加：熔断真终结 + 胶囊闭合（用户定案 2026-07-15）

### 机制设计（全量）

**不变**：计数语义（FAILED +1 / FINISHED 清零 / CANCELED 与运行层崩溃不计）；触发条件（`threshold > 0 and counter >= threshold`，默认 3）。

**TaskManager 新增状态**：
- `_threshold_tripped: bool`——幂等闩（现状有 double-trip 洞：trip 后在途任务再失败会重进分支重复发事件）。
- `_recent_failures: list[tuple[str, str]]`——(title, reason) 随 counter 同步积累（FAILED 追加 `(task.title or task.id, (task.error or task.process_report or "")[:200])`，FINISHED 清空）。跨崩溃恢复不重建（清单可能不满员），接受。
- 三个注入点（接线方式镜像 `set_has_pending_hitl`）：`set_cancel_pending_hitl(async cb)`、`set_cancel_inflight(cb(task_id)->bool)`、`set_threshold_finalizer(async cb(root_task|None, ack_tasks, failures))`。

**trip 序列**（`_trip_failure_threshold()`，从 on_task_finished 的 FAILED 分支进入）：
1. 幂等闩置位；`_cancelled = True` 封闸（drain 守卫白拿；`_flush_staged` 丢弃条件扩为 `_pause_abandon or _cancelled`，堵在途 run 迟到 staged 落日志成新搁浅）。
2. 发 `FAILURE_THRESHOLD_HIT`，payload 加 `failures: [{title, reason}]`。
3. `await _cancel_pending_hitl()`（best-effort）。HitlCancelled 全部先于会话终态；host 投影 from_status 守卫防翻态（projection_updater.py:80）。
4. 清队：`drain_pending()`；对**非 root** 条目标 CANCELED + `TASK_CANCELED(reason="failure_threshold")`；root 条目直接丢弃（去向是第 6 步判 FAILED）。
5. 取消挂起：`_tasks` 中 SUSPENDED 且非 root → CANCELED + 事件；其中已启动（`started_at and origin_tool_call_id and parent_task_id`）者收进 `ack_tasks`。
   同时对在途非 root run 调 `_cancel_inflight(tid)` 发协作取消（不发事件——它们的 TASK_CANCELED 由 `_run_loop` 退出时发）；已启动带框者也收进 `ack_tasks`。
6. root 判 FAILED：所有 `parent_task_id is None` 且非终态的任务 → status=FAILED、`error_code="TASK_FAILED_BY_THRESHOLD"`、error 带 N 连败文案、finished_at，发 `TASK_FAILED(error_code="TASK_FAILED_BY_THRESHOLD")`。root 已终态（自己是第 3 败→FinalizeStep 已闭合；或时序尾巴已 FINISHED）→ 不改状态不发事件、闭合跳过。**先标 FAILED 再**对在跑的 root 调 `_cancel_inflight`（顺序保证 `_run_loop` 终态守卫接得住，见 Task 10 守卫）。
7. `await _threshold_finalizer(root_we_failed_and_started or None, ack_tasks, failures)`——**内联 await**，SESSION_FINISHED（SSE 关闭）前 memory 落盘；异常记日志不阻断。
8. session.status="FAILED" → `SESSION_STATUS_CHANGED(FAILED, reason="failure_threshold")` → `_fire_session_done()`。

**on_task_finished 配套改动**：FAILED 分支改调 `_trip_failure_threshold`（带 `not _threshold_tripped` 前置）；CANCELED 分支守卫扩为 `if not self._pause_abandon and not self._threshold_tripped`（防迟到协作取消把 FAILED 盖成 CANCELED）；is_done 终结块加 `_session_done_fired` 前置防重复 SESSION_STATUS_CHANGED。

**Runtime 侧**（Task 10）：
- `_cancel_session_hitl(sid)`：遍历 `hitl_manager.list_pending(session_id)` 逐个 `cancel(id, message="failure_threshold")`。
- `_cancel_run_token(sid, tid)`：查 `_run_tokens` 发 `tokens.cancel.cancel()`。
- `_finalize_threshold_memory(session, root_task, ack_tasks, failures)`：
  - ack 替换：每个 ack_task 以 `parent_scope=MemoryScope(session, t.parent_task_id, t.creator_agent_id)` 走 `_ensure_dispatch_frame` + `_put_dispatch_result(replace=True)`，文案 `Sub-task '<title>' was cancelled mid-run (session failure threshold hit); its partial execution below is incomplete.`。若某在途任务赶在取消前正常收尾，FinalizeStep 的 replace=True 会再次替换为真实终态——自愈为真相。
  - root finish 对：`_synthesize_dispatch_pair(memory, MemoryScope(session, root.id, session.root_agent_id), root, act_recap="Session failure threshold was hit (N consecutive sub-task failures); terminating this task.", task_summary="Failure threshold hit — consecutive failures: 1) title: reason; 2) …", outcome="fail", provider_ctx, register_bg=False)`。仅当熔断亲手标的 FAILED 且 `started_at` 非空（有胶囊可闭）。
- finalize.py helper 重构：`_ensure_dispatch_frame` / `_put_dispatch_result` 的 `ctx` 参数改为直接接受 `provider_ctx`（更新既有 3-4 个调用点）；`_synthesize_dispatch_pair` 加 `register_bg: bool = True`，threshold 路径传 False（跳过 pop_close_report/register_close_synth 的 bg-observe 替换登记）。
- `_run_loop` A1 守卫：`finally` 的 `was_cancelled` 分支只在 `task.status == "CANCELED"`（真的由本 run 置的取消态）时才发 RUN_CANCELED + TASK_CANCELED；已被熔断标 FAILED 的 run 被协作取消属内部清场，两事件都不发（否则 postgres 投影把 root/session 盖成 CANCELED），RUN_FINISHED 照发。

**D 折叠**（Task 11 core / Task 12 host）：
- core reducers `_apply`：TASK_FAILED 且 `payload.error_code != "TASK_FAILED_BY_THRESHOLD"` → counter +1；TASK_FINISHED → 清零；删除 FAILURE_THRESHOLD_HIT 的 +1。恢复路径（converters 已透传）从此有真值；`finalize_idle_session` 的 FAILED/SUCCEEDED 判定得到修复。
- host `SessionEntry.apply` + `postgres/projection_updater` 同语义同步；FAILURE_THRESHOLD_HIT 不再驱动计数。

**多轮语义（E）**：trip 清场后事件日志零非终态任务 → 下一条 /messages 直接开新轮；熔断只判死本轮，失败胶囊留在历史。

**接受的边缘**：trip 与 memory 闭合之间崩溃 → 闭合丢失（毫秒窗口，事件已终态不复活）；`_recent_failures` 不跨崩溃恢复。

### Task 9: TM trip 序列 + 注入点 + 配套守卫（含单测）

**Files:** Modify `src/ctx_weft/core/orchestrator/task_manager.py`；Test 新建 `tests/unit/test_failure_threshold_trip.py`
**Produces:** `_trip_failure_threshold`、三个 set_* 注入点、`_recent_failures`、`_threshold_tripped`、`_flush_staged`/`CANCELED` 分支/is_done 块守卫。实现按上方设计逐条落；测试覆盖：3 连败 trip 事件序（THRESHOLD_HIT→TASK_CANCELED*→TASK_FAILED(root)→SESSION_STATUS_CHANGED(FAILED)）；队列/挂起任务全 CANCELED、root FAILED；幂等（第 4 败不重发）；封闸后 `_flush_staged` 丢弃；CANCELED 迟到不盖 FAILED；`ack_tasks`/`failures` 传给 finalizer stub 的内容正确；root 已终态跳过；`cancel_inflight` 对在途 id 被调用。TDD、`uv run pytest`、commit `feat(task): 熔断真终结——trip 清场+root 判死+注入点`。

### Task 10: runtime 接线 + memory 闭合 + _run_loop 守卫（含单测）

**Files:** Modify `src/ctx_weft/core/runtime.py`、`src/ctx_weft/core/loop/steps/finalize.py`；Test 新建 `tests/unit/test_threshold_finalizer.py`（memory 断言用 InMemoryMemoryProvider）+ `_run_loop` 守卫用例入 `tests/unit/test_run_crash_suspend.py` 或独立文件
**Produces:** `_cancel_session_hitl` / `_cancel_run_token` / `_finalize_threshold_memory` + `_register_and_drain` 三处接线；finalize helpers 的 provider_ctx 重构与 `register_bg` 参数；`_run_loop` was_cancelled 事件守卫。测试覆盖：root finish 对内容（`[outcome=fail]` 前缀 + 失败清单）落 root scope；ack 替换后 parent scope 旧 running ack 被 supersede、新文案就位；root=None 跳过；helpers 重构后既有 finalize 测试不回归（跑 `tests/unit/test_finalize.py test_close_task.py test_two_phase_dispatch.py`）；was_cancelled+FAILED 不发 RUN_CANCELED/TASK_CANCELED、was_cancelled+CANCELED 照发。commit `feat(loop): 熔断胶囊闭合——root finish 对+取消 ack 替换+run 取消事件守卫`。

### Task 11: core counter 真折叠（含单测）

**Files:** Modify `src/ctx_weft/core/control/reducers.py`；Test `tests/unit/test_failure_counter_fold.py`
按设计折叠；测试：TASK_FAILED +1、THRESHOLD 码不计、TASK_FINISHED 清零、FAILURE_THRESHOLD_HIT 不再 +1、快照往返保留。commit `fix(reducer): failure_counter 真折叠——恢复后熔断记忆一致`。

### Task 12: host 同步（折叠 + revendor + 回归）

**Files:** host 仓 `src/ipmastercowork/api/models/session.py`、`src/ipmastercowork/persistence/postgres/projection_updater.py`、vendor/uv.lock；Test host `tests/test_failure_counter_fold.py`
先 revendor（Task 8 同法），host 两处投影按 D 同步，全量 `uv run pytest tests -q` 绿（断言旧语义者按新契约反转）。commit `chore(vendor)+fix(projection): 熔断真终结语义同步`。

### Task 14: 统一取消胶囊闭合（用户定案：所有 CANCELED 终态 = ack 终态化 + 取消说明 finish 对）

**执行顺序：在 Task 10/11 之后、Task 12（host revendor）之前。**

**Files:** Modify `src/ctx_weft/core/loop/steps/finalize.py`（新 `synthesize_cancel_closure` + `_find_dispatch_frame` 拆分 + `_finish_report_prefix` 增 `[outcome=cancelled]`）、`src/ctx_weft/core/orchestrator/task_manager.py`（`set_cancel_finalizer` 注入点 + 三个调用点）、`src/ctx_weft/core/runtime.py`（接线 + 实现）；Test 新建 `tests/unit/test_cancel_closure.py`

**设计（权威）：**
- `synthesize_cancel_closure(memory, session_id, task, provider_ctx, reason_text)`：镜像 `_close_one` 分支——①有 parent+origin_tool_call_id → 父 scope ack 替换 `Sub-task '<title>' was cancelled before completion (<reason>); its partial execution below is incomplete.`；②同 agent 子任务在共享 scope、跨 agent 子任务与 root 在自己 scope 合成 finish 对：assistant 槽 `finish_task{}` content="Task was cancelled before completion."，tool 槽 `[task: <title>] [outcome=cancelled] Cancelled (<reason>) — no final output was produced; the partial execution above is all that ran.`（`register_bg=False` 语义：不挂 bg 替换登记）；③无框（born-cancel 未铸）→ 整体跳过、不补铸（`_find_dispatch_frame` 只查不建；`_ensure_dispatch_frame` 重构为 find+create 复用它）。
- 写入时机：直接标 CANCELED 的点（cancel_all 清队、熔断清场挂起/排队）→ 标态后立即闭合；在途协作取消 → 发信号时只做 ack 替换（幂等自愈），finish 对等 `on_task_finished(CANCELED)` 终态坐实后写（任务抢跑正常收尾则走 FinalizeStep，绝无重复 finish 对）。
- TM `set_cancel_finalizer(async cb(tasks: list[Task], reason: str))`，调用点：`cancel_all`（reason="user_cancel"）、`_trip_failure_threshold` 清场步骤（reason="failure_threshold"，替代 Task 10 对挂起/排队任务的 ack-only 处理；在途仍 ack-eager+funnel finish 对）、`on_task_finished` CANCELED 分支（started_at 且 origin_tool_call_id 者；reason 取 task.error 回退通用文案）。异常单条记日志不阻断。
- pause 弃子的在途协作取消经同一 funnel 受益；熔断 root 保持 FAILED+失败清单 finish 对不变。

**测试覆盖：** cancel_all 已启动任务 → ack 替换+finish 对（同 agent 嵌套形态断言 `[outcome=cancelled]`）；未启动 → 零 memory 写；born-cancel 无框 → 跳过不补铸；在途取消 funnel → on_task_finished(CANCELED) 后才有 finish 对、信号时刻只有 ack；抢跑正常收尾 → 无取消 finish 对（FinalizeStep 真终态覆盖 ack）；root 被 cancel_all → own scope finish 对；熔断路径回归（test_failure_threshold_trip 不破）。
TDD；回归 `uv run pytest tests/unit/test_cancel_closure.py tests/unit/test_failure_threshold_trip.py tests/unit/test_threshold_finalizer.py tests/unit/test_finalize.py tests/unit/test_close_task.py tests/unit/test_task_manager_cancel_all.py tests/unit/test_pause_abandon_tm.py -q`；commit `feat(finalize): 统一取消胶囊闭合——ack 终态化+[outcome=cancelled] finish 对`。

### Task 13: 两仓全量回归 + 增量 final review

core `uv run pytest tests -q` + host 全量；生成两仓增量 review 包（core 自 8987f3e、host 自 4051fb8）派最终审查（最强模型），修 Critical/Important。

## Self-Review 记录

1. **Spec 覆盖**：
   - 「运行层崩溃需恢复重跑、不落终态」→ Task 1（挂起终局）+ Task 3（_run_loop 预标）+ Task 4（恢复归零重试计数）。
   - 「只有 LLM 判定失败才是真失败（终态+闭合胶囊+回传父亲）」→ Task 1 移除运行层 TASK_FAILED / failure_counter；observer 路径（FinalizeStep）不动，天然独占终态。
   - 「ContextOverflow 不特判、报不同的错、不重试」→ Task 3 删分支；`retriable=False` 保证不重试；错误码 CONTEXT_OVERFLOW 经 TASK_SUSPENDED.error_code 与 INTERRUPTED.reason 区分（Task 1 测试 4）。
   - 「所有恢复入口允许切换模型」→ 字段已存在（recover_session / HitlRequest.resume_llm_* / SessionStartParams），Task 5 补齐"字段生效但窗口不随新模型"的缺口并给出入口审计说明。
   - 「挂起任务不被并行任务的会话终结孤立」→ Task 2（衍生自新语义的必要守卫，含 LLM outage 既有隐患）。
2. **占位符扫描**：Task 3 Step 1 对 `status_events` 断言给了两种情形的明确改法（以 harness 驱动层级为判据）；Task 6 列出候选文件与统一反转规则，不含 TBD。
3. **类型/签名一致性**：`_suspend_task_interrupted(task_id: str, error: str, exc: BaseException | None)` 在 Task 1 定义、Task 2/3 消费；`_sync_session_llm_window(session: Session)` 在 Task 5 定义与两处调用一致；测试 helper `_tm()` 返回 `(TaskManager, Session, Task)` 三元组，Task 2 测试按此解包。
