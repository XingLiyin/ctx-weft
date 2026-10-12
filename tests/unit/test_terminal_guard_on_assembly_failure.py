"""终态不复活：熔断判死之后的装配失败不得把 FAILED 盖回 INTERRUPTED（总账 A7）。

`apply_run_outcome` 有终态守卫（`task_manager.py` 的 `_TERMINAL_STATUSES` 判据），
`_handle_task_failure` / `_suspend_task_interrupted` 原先一个都没有：熔断 trip 已把
某 task 判 FAILED（发 TaskFailed）后，若该 task 的**装配**随后失败，
`_suspend_task_interrupted` 会无条件写 `task.status = "INTERRUPTED"` 并发
`TaskInterrupted`——把写定的终态盖回非终态。

搭台手法照 `test_run_crash_suspend.py` 的 `_tm()`：一个挂着 `_CapturingBus`、
`_max_concurrent=0`（drain 空转，不真正派发）的 TaskManager。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.models.discriminators import InterruptReason
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import Task
from ctx_weft.protocols.events import EventType
from tests.unit._stub_runner import StubRunner


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def tm_with_bus():
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0  # drain 空转：不真正派发
    session = Session(id="s1", user_prompt="", status="RUNNING")
    tm.set_session(session)
    tm.set_runner(StubRunner(tm))
    t = Task(id="tsk_1", session_id="s1", status="ACTIVE")
    tm.register_task(t)
    return tm, bus


async def test_assembly_failure_does_not_resurrect_a_terminal_task(tm_with_bus):
    tm, bus = tm_with_bus
    task = tm.get_task("tsk_1")
    task.status = "FAILED"          # 熔断已判死
    before = len(bus.events)

    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=RuntimeError("x"),
        reason=InterruptReason.ASSEMBLY_FAILURE,
    )

    assert task.status == "FAILED", "终态被盖回了非终态"
    assert len(bus.events) == before, "终态 task 不该再发任何事件"


async def test_terminal_guard_still_clears_running_bookkeeping(tm_with_bus):
    """守卫不挡队列清理：终态 task 的装配失败仍须把它从 running 簿记里摘掉。

    生产路径上 `_handle_task_failure` 只在 `_run_task` 的 assemble 异常分支被调用，
    而 `_run_task` 是在 `drain()` 把 task_id 加进 `_running_tasks` 之后才
    `asyncio.create_task` 出来的——所以进入本方法时 task_id 必然还留在
    `_running_tasks` / 队列的 running 簿记里。若早返回连这部分清理也一并挡掉，
    这个已经终态的 task 会永久占住一个 running 槽位，堵死后续 drain。
    """
    tm, _bus = tm_with_bus
    task = tm.get_task("tsk_1")
    task.status = "FAILED"
    tm._running_tasks.add("tsk_1")
    tm._running_agents["tsk_1"] = "agent_x"
    tm._queue._running.add("tsk_1")  # 镜像 drain() 之前对队列做的 mark-running

    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=RuntimeError("x"),
        reason=InterruptReason.ASSEMBLY_FAILURE,
    )

    assert "tsk_1" not in tm._running_tasks, "终态 task 不该继续占着 running 槽位"
    assert "tsk_1" not in tm._running_agents
    assert "tsk_1" not in tm._queue._running


async def test_non_terminal_task_still_handled(tm_with_bus):
    """守卫只挡终态——非终态照常走原逻辑。"""
    tm, bus = tm_with_bus
    tm.get_task("tsk_1").status = "ACTIVE"
    await tm._handle_task_failure(
        "tsk_1", error="boom", exc=RuntimeError("x"),
        reason=InterruptReason.ASSEMBLY_FAILURE,
    )
    assert any(e.type in (EventType.TASK_REQUEUED, EventType.TASK_INTERRUPTED)
               for e in bus.events)
