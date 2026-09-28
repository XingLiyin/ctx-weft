"""交互口闸门：同一个 `port_key` 上同时至多一个 task 占着往返。

判据是 `TaskManager._held_ports`：`port_key` 非空 + 非 unattended + 「在跑 **或**
`AWAITING_HUMAN`」。单值年代（2026-09-22）它叫「单交互线闸门」，port 化之后
「一条线」只是「所有 task 都接在 `main` 口」的那个特例。

三条边界各有一个用例，它们分别是这道闸门最容易写错的地方：
- **算上 park 中的**——只看 `_running_tasks` 不够，park 一发生槽位就还回来了；
- **SUSPENDED 不算**——父等子时子正该跑，算进来整个 DAG 立刻死锁；
- **INTERRUPTED 不算**——那是等 `/resume` 的故障态，不该把正常工作一并按住。

port 维度另有三个用例（见文件末尾）：同口串行 / 异口并行 / 「接口但自治」既不被拦
也不拦人。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import PORT_MAIN, PORT_NONE, Task
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


def _task(
    tid: str, *, unattended: bool = False, status: str = "PENDING",
    port_key: str = PORT_MAIN,
) -> Task:
    # 每个 task 一个独立 agent——否则会先被「同 agent 不并发」挡住，测不到本闸门。
    return Task(
        id=tid, session_id="s1", status=status,
        assigned_agent_id=f"ag-{tid}", creator_agent_id=f"ag-{tid}",
        unattended=unattended, port_key=port_key,
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


# ── port 维度 ────────────────────────────────────────────────────────────────


async def test_same_port_tasks_are_serialized(runner_cleanup) -> None:
    """两个接在**同一个非默认口**上的 task → 仍然只有一个被派发。

    与 `test_two_attended_tasks_are_serialized` 的区别只在口的名字：证明串行不是
    `main` 的特权，而是每个口各自的不变式。
    """
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    _enqueue(tm, _task("A", port_key="btw"))
    _enqueue(tm, _task("B", port_key="btw"))

    await tm.drain()

    assert len(tm._running_tasks) == 1
    await asyncio.sleep(0)


async def test_different_ports_run_in_parallel(runner_cleanup) -> None:
    """**port 化的要害**：两条不同口上的交互线并行，互不相拦。

    这正是 btw 要的形状——主线接 `main`、旁支接 `btw`，两边可以同时等各自的对端。
    单值闸门年代这里只会派发一个。
    """
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    _enqueue(tm, _task("MAIN", port_key=PORT_MAIN))
    _enqueue(tm, _task("BTW", port_key="btw"))

    await tm.drain()

    assert tm._running_tasks == {"MAIN", "BTW"}
    await asyncio.sleep(0)


async def test_attached_but_autonomous_neither_waits_nor_blocks(runner_cleanup) -> None:
    """「接口但自治」（`port_key` 非空 + `unattended=True`）：既不被拦，也不拦别人。

    它跑完只是把产出推到那个口，不参与往返——所以主口上有人占着往返时它照样派发，
    而它自己在跑也不会让同口的常规对话等它。
    """
    tm = _tm()
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)
    # 先让一个「接主口但自治」的 task 跑起来
    _enqueue(tm, _task("PUSH", port_key=PORT_MAIN, unattended=True))
    await tm.drain()
    assert tm._running_tasks == {"PUSH"}

    # 它不占口 → 同一个主口上的常规对话照样能被派发
    _enqueue(tm, _task("CHAT", port_key=PORT_MAIN))
    await tm.drain()
    assert tm._running_tasks == {"PUSH", "CHAT"}

    # 反向：常规对话占着主口时，再来一个自治推送也不被拦
    _enqueue(tm, _task("PUSH2", port_key=PORT_MAIN, unattended=True))
    await tm.drain()
    assert tm._running_tasks == {"PUSH", "CHAT", "PUSH2"}
    await asyncio.sleep(0)


def test_fully_autonomous_task_holds_no_port() -> None:
    """完全自治（`port_key` 空）：不出现在 `_held_ports` 里，哪怕它正在跑。"""
    tm = _tm()
    t = _task("BG", port_key=PORT_NONE, unattended=True)
    tm.register_task(t)
    tm._running_tasks.add("BG")
    assert tm._held_ports() == set()


# ── 并发槽：一个池，交互 task 可临时顶上去（2026-09-28）──────────────────────


def _tm_with_slots(n: int) -> TaskManager:
    tm = TaskManager(session_id="s1", max_concurrent=n)
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING", root_agent_id="root"))
    return tm


async def test_interactive_task_can_push_the_pool_over_the_limit(runner_cleanup) -> None:
    """**本条的要害**：池满之后交互 task 照样派发，总数因此短暂超过 `max_concurrent`。

    额度不会失控——交互口闸门保证一口一往返，所以一条口最多让池涨 1。
    """
    tm = _tm_with_slots(1)
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)

    # LIFO（pop 取栈顶）：BG 先被派发占满池，MAIN 随后仍要能进来。
    _enqueue(tm, _task("MAIN", port_key=PORT_MAIN))
    _enqueue(tm, _task("BG", unattended=True, port_key=PORT_NONE))

    await tm.drain()

    assert tm._running_tasks == {"BG", "MAIN"}
    assert len(tm._running_tasks) > 1        # 池的基准是 1，被交互顶上去了
    await asyncio.sleep(0)


async def test_autonomous_is_squeezed_by_interactive(runner_cleanup) -> None:
    """反向：交互 task 占了池，自治作业就得等——所有 task 都计入同一个池。"""
    tm = _tm_with_slots(1)
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)

    _enqueue(tm, _task("BG", unattended=True, port_key=PORT_NONE))
    _enqueue(tm, _task("MAIN", port_key=PORT_MAIN))      # 栈顶，先派发

    await tm.drain()

    assert tm._running_tasks == {"MAIN"}
    assert [e.task_id for e in tm._queue.peek_all()] == ["BG"]   # 留在队列里等
    await asyncio.sleep(0)


async def test_full_pool_does_not_terminate_the_drain_loop(runner_cleanup) -> None:
    """池满从「终止条件」降级成「跳过自治条目」。

    旧实现是 `if len(running) >= max: break`——池一满整个 drain 就退出，排在自治条目
    **底下**的交互 task 再也轮不到。现在它只是 skip 谓词的一条分支，循环继续往下扫。
    """
    tm = _tm_with_slots(1)
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)

    _enqueue(tm, _task("MAIN", port_key=PORT_MAIN))                  # 栈底
    _enqueue(tm, _task("BG1", unattended=True, port_key=PORT_NONE))
    _enqueue(tm, _task("BG2", unattended=True, port_key=PORT_NONE))  # 栈顶

    await tm.drain()

    assert "MAIN" in tm._running_tasks, "压在自治条目底下的交互 task 必须仍被派发"
    assert len([t for t in tm._running_tasks if t.startswith("BG")]) == 1
    await asyncio.sleep(0)


async def test_one_port_lifts_the_pool_by_at_most_one(runner_cleanup) -> None:
    """一条口最多让池涨 1：同口的第二个交互 task 被口闸门挡住，不会继续顶。"""
    tm = _tm_with_slots(1)
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)

    _enqueue(tm, _task("BG", unattended=True, port_key=PORT_NONE))
    _enqueue(tm, _task("MAIN1", port_key=PORT_MAIN))
    _enqueue(tm, _task("MAIN2", port_key=PORT_MAIN))

    await tm.drain()

    # 两个 main 口的 task 只有一个能跑；池因此最多是 基准1 + 口1 = 2
    mains = [t for t in tm._running_tasks if t.startswith("MAIN")]
    assert len(mains) == 1
    assert len(tm._running_tasks) <= 2
    await asyncio.sleep(0)


async def test_zero_max_concurrent_is_still_a_master_switch(runner_cleanup) -> None:
    """`max_concurrent <= 0` 仍然封死一切，交互 task 也不例外。

    这条既有语义是「这个 session 暂不派发」的表达方式，多处依赖（不少测试靠它让
    drain 空转以观察队列）。收窄池语义时**刻意**保留。
    """
    tm = _tm_with_slots(0)
    runner = _ParkedRunner(tm)
    runner_cleanup.append(runner)
    tm.set_runner(runner)

    _enqueue(tm, _task("MAIN", port_key=PORT_MAIN))
    _enqueue(tm, _task("BG", unattended=True, port_key=PORT_NONE))

    await tm.drain()

    assert tm._running_tasks == set()
    assert tm._queue.pending_count() == 2
