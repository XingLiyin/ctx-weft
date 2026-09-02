"""运行层崩溃（**执行期**）= 可恢复中断（挂起等 /resume），不是失败。

**走的是真崩溃路径**：`runner.execute` 抛 → `TaskManager._run_task` 的 `except
Exception` → `crash_run_outcome(exc)` → `disposition_for` → `apply_run_outcome` →
`_settle`。Task 4 之前这条路径经 `_handle_task_failure`，本文件当时就是那么写的；
补 `reason=` 必传参数时它被整体挪去了**装配失败**路径（`_handle_task_failure` 如今
只服务 assemble 阶段），于是文件名与 docstring 说的 "run crash" 与实际测的东西对不上，
下面这四条断言在真崩溃路径上无人接管——`TaskInterrupted.error_code=="CONTEXT_OVERFLOW"`、
`TaskQueueInterrupted.reason=="CONTEXT_OVERFLOW"`、崩溃不动 `failure_counter`、崩溃挂起
阻塞会话收尾。现改回真崩溃路径。装配失败那条另有覆盖（`test_task_scheduling.py` 的
`TaskInterrupted` / `TaskRequeued` 两支、`test_superseded_task_manager.py` 的归属权守卫）。

判据是**事件类型**：TM 的挂起收尾发 task 域的 `TaskInterrupted`（run 域的
`RunInterrupted` 由 runtime._run_loop 发，TM 在 run 外面、拿不到 run_id），TM 聚合成
`TaskQueueInterrupted`，会话状态由 SessionManager 判定（TM 不再自己写 session.status，
也不再发 `SessionStatusChanged`）。

覆盖：
1. 不可重试异常 → TASK_INTERRUPTED + TASK_QUEUE_INTERRUPTED，绝不发 TASK_FAILED。
2. 可重试异常耗尽 max_retries → 同上（不再降级终态 FAILED）。
3. 崩溃挂起不触碰 session.failure_counter（真失败只有 observer 判 fail 一条路）。
4. ContextOverflowError：retriable=False → 不重试直接挂起，error_code=CONTEXT_OVERFLOW
   区分性地抵达事件流,并上浮成 TaskQueueInterrupted.reason（host 据此提示换更大窗口的
   模型恢复）——会话级中断的 reason 是**码**,不是自由文本。
"""

from __future__ import annotations

from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.protocols.events import EventType
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


async def _crash(tm: TaskManager, exc: BaseException) -> None:
    """让 execute 抛出 exc，驱动一次真崩溃：`_run_task` 的 except → crash_run_outcome。

    `error` 不再是独立入参——真路径上它就是 `str(exc)`（见 `errors.crash_run_outcome`）。
    """
    async def _boom(_s: str, _t: str) -> None:
        raise exc

    tm.set_runner(StubRunner(tm, _boom))
    await tm._run_task("A")


def _types(bus: _CapturingBus) -> list:
    return [e.type for e in bus.events]


async def test_non_retriable_crash_suspends_not_fails() -> None:
    bus = _CapturingBus()
    tm, session, t = _tm(bus)

    await _crash(tm, _NonRetriable("401 unauthorized"))

    assert EventType.TASK_FAILED not in _types(bus)
    assert EventType.TASK_SUSPENDED not in _types(bus)   # 旧的 reason 分流已退场
    assert EventType.SESSION_STATUS_CHANGED not in _types(bus)
    interrupted = [e for e in bus.events if e.type == EventType.TASK_INTERRUPTED]
    assert interrupted and interrupted[0].task_id == "A"
    assert interrupted[0].payload["error_code"] == "LLM_AUTH_FAILED"
    assert interrupted[0].payload["error_message"] == "401 unauthorized"
    # TM 的聚合信号：队列里没有能跑的了，因为有任务断了。会话状态由 SM 据此判定。
    # reason 带的必须是**错误码**——host 按码分流提示（换更大窗口的模型 / 改配置），
    # 降级成自由文本会让分流静默失效。
    queue_sig = [e for e in bus.events if e.type == EventType.TASK_QUEUE_INTERRUPTED]
    assert queue_sig and queue_sig[0].payload["reason"] == "LLM_AUTH_FAILED"
    assert t.status == "INTERRUPTED"
    assert t.error == "401 unauthorized"
    assert session.status == "RUNNING"   # 会话状态不再由 TM 改写（归 SessionManager）


async def test_retry_exhausted_suspends_not_fails() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    t.retry_count = t.max_retries  # 自动重试已耗尽

    await _crash(tm, _Retriable("transient"))

    assert EventType.TASK_FAILED not in _types(bus)
    assert EventType.TASK_REQUEUED not in _types(bus)  # 耗尽后不再重排
    assert EventType.TASK_INTERRUPTED in _types(bus)
    assert t.status == "INTERRUPTED"


async def test_run_crash_does_not_touch_failure_counter() -> None:
    bus = _CapturingBus()
    tm, session, _t = _tm(bus)

    await _crash(tm, _NonRetriable("boom"))

    assert session.failure_counter == 0
    assert EventType.SESSION_FINISHED not in _types(bus)


async def test_context_overflow_suspends_without_retry() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    exc = ContextOverflowError(context_limit=100_000, required=171_808,
                               effective_limit=92_000, reserved_output_tokens=8_000)

    await _crash(tm, exc)

    assert EventType.TASK_REQUEUED not in _types(bus)  # retriable=False：不重试
    assert EventType.TASK_FAILED not in _types(bus)
    interrupted = [e for e in bus.events if e.type == EventType.TASK_INTERRUPTED]
    assert interrupted and interrupted[0].payload["error_code"] == "CONTEXT_OVERFLOW"
    # 溢出码必须上浮到会话级中断的 reason —— host 据此提示换更大窗口的模型恢复。
    queue_sig = [e for e in bus.events if e.type == EventType.TASK_QUEUE_INTERRUPTED]
    assert queue_sig and queue_sig[0].payload["reason"] == "CONTEXT_OVERFLOW"
    assert t.status == "INTERRUPTED"


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

    await _crash(tm, _NonRetriable("boom"))
    await tm.on_task_finished("B", status="FINISHED")

    assert EventType.SESSION_FINISHED not in _types(bus)
    # TM 报的是「断了」而不是「跑完了」——SM 据此不会终结会话（TaskQueueDrained 才会）。
    assert EventType.TASK_QUEUE_DRAINED not in _types(bus)
    assert _types(bus).count(EventType.TASK_QUEUE_INTERRUPTED) >= 1
    assert session.status == "RUNNING"  # 会话状态归 SessionManager，TM 不写


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
