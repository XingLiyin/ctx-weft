"""单交互线闸门（S1）：非 unattended 的 task 在一个 session 内同时至多一个占着交互线。

判据是 `TaskManager._interactive_line_held`：「在跑 **或** `AWAITING_HUMAN`」。

三条边界各有一个用例，它们分别是这道闸门最容易写错的地方：
- **算上 park 中的**——只看 `_running_tasks` 不够，park 一发生槽位就还回来了；
- **SUSPENDED 不算**——父等子时子正该跑，算进来整个 DAG 立刻死锁；
- **INTERRUPTED 不算**——那是等 `/resume` 的故障态，不该把正常工作一并按住。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.core.orchestrator.task.queue import QueueEntry
from ctx_weft.core.orchestrator.task.runner import AgentBinding, effective_agent_id


class _ParkedRunner:
    """派发即挂住——让被派发的 task 停在「在跑」，好观察闸门对后续条目的作用。

    不保存 `_run_task` 的 Task 句柄是 `drain()` 的既有行为，所以收尾靠 `release()`
    放行 + 一次事件循环让渡，而不是 cancel。
    """

    def __init__(self, tm: TaskManager) -> None:
        self._tm = tm
        self._gate = asyncio.Event()
        self.dispatched: list[str] = []

    async def assemble(self, task_id: str) -> AgentBinding | None:
        task = self._tm.get_task(task_id)
        if task is None:
            return None
        root = self._tm.session.root_agent_id if self._tm.session else ""
        return AgentBinding(agent_id=effective_agent_id(task, root))

    async def execute(self, binding: AgentBinding, task_id: str):
        self.dispatched.append(task_id)
        await self._gate.wait()
        return None

    def release(self) -> None:
        self._gate.set()


def _tm() -> TaskManager:
    tm = TaskManager(session_id="s1")
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING", root_agent_id="root"))
    return tm


def _task(tid: str, *, unattended: bool = False, status: str = "PENDING") -> Task:
    # 每个 task 一个独立 agent——否则会先被「同 agent 不并发」挡住，测不到本闸门。
    return Task(
        id=tid, session_id="s1", status=status,
        assigned_agent_id=f"ag-{tid}", creator_agent_id=f"ag-{tid}",
        unattended=unattended,
    )


def _enqueue(tm: TaskManager, task: Task) -> None:
    tm.register_task(task)
    tm._queue.push(QueueEntry(task_id=task.id, session_id="s1"))


@pytest.fixture
def runner_cleanup():
    runners: list[_ParkedRunner] = []
    yield runners
    for r in runners:
        r.release()


async def test_two_attended_tasks_are_serialized(runner_cleanup) -> None:
    """两个非 unattended task 同时可派发 → 只有一个真被派发。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    _enqueue(tm, _task("A"))
    _enqueue(tm, _task("B"))

    await tm.drain()

    # `drain` 在锁内就把 task_id 加进 `_running_tasks`，无需等协程起跑。
    assert len(tm._running_tasks) == 1
    await asyncio.sleep(0)


async def test_parked_task_still_holds_the_line(runner_cleanup) -> None:
    """**本闸门的要害**：park 中的 task 不在 `_running_tasks` 里，但仍占着交互线。

    `_settle` 对 park 先 `_release_slot` 再立刻 `drain()`——只看在跑的话，槽位一空
    下一个就被派发、它也 park，host 侧于是出现两条同时等人说话的线。
    """
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    # A 已 park 等人：槽位早已归还，只剩 status 作证。
    tm.register_task(_task("A", status="AWAITING_HUMAN"))
    _enqueue(tm, _task("B"))

    await tm.drain()

    assert tm._running_tasks == set()
    assert runner.dispatched == []


async def test_unattended_tasks_are_not_gated(runner_cleanup) -> None:
    """无人值守的后台作业不受此限，照 `max_concurrent` 并行。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    for tid in ("A", "B", "C"):
        _enqueue(tm, _task(tid, unattended=True))

    await tm.drain()

    assert len(tm._running_tasks) == 3
    await asyncio.sleep(0)


async def test_unattended_task_runs_while_attended_one_is_parked(runner_cleanup) -> None:
    """一条交互线 park 着，后台作业照跑——闸门只拦非 unattended 的。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    tm.register_task(_task("A", status="AWAITING_HUMAN"))
    _enqueue(tm, _task("BG", unattended=True))

    await tm.drain()

    assert tm._running_tasks == {"BG"}
    await asyncio.sleep(0)


async def test_suspended_parent_does_not_hold_the_line(runner_cleanup) -> None:
    """SUSPENDED（等子任务）不算占线——算进来父等子时子就永远排不上，DAG 死锁。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    parent = _task("P", status="SUSPENDED")
    tm.register_task(parent)
    child = _task("C")
    child.parent_task_id = "P"
    _enqueue(tm, child)

    await tm.drain()

    assert tm._running_tasks == {"C"}
    await asyncio.sleep(0)


async def test_interrupted_task_does_not_hold_the_line(runner_cleanup) -> None:
    """INTERRUPTED（等 /resume 的故障态）不算占线——故障不该按住正常工作。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    tm.register_task(_task("A", status="INTERRUPTED"))
    _enqueue(tm, _task("B"))

    await tm.drain()

    assert tm._running_tasks == {"B"}
    await asyncio.sleep(0)


async def test_line_frees_when_parked_task_reaches_terminal(runner_cleanup) -> None:
    """park 的 task 一旦终结，闸门放行——判据读的是实时 `task.status`。"""
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    parked = _task("A", status="AWAITING_HUMAN")
    tm.register_task(parked)
    _enqueue(tm, _task("B"))

    await tm.drain()
    assert runner.dispatched == []

    parked.status = "FINISHED"
    await tm.drain()

    assert tm._running_tasks == {"B"}
    await asyncio.sleep(0)
