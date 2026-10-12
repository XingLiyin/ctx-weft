"""自治作业停在 `INTERRUPTED` 时由 SDK 自己恢复（2026-09-28）。

洞的形状:`INTERRUPTED` 的定义是「等 `/resume`」,而 `/resume` 的唯一运行期入口是
`requeue_resumable` ← `_resume_in_existing_tm` ← host 主动调用。自治作业没有对端,
没有人会对一个后台作业按「继续跑」——于是恢复只能靠下一次 `recover_session` 被
`restore` 顺带捡回来,也就是**依赖一个 SDK 无法保证的外部事件**,期间会话既不 idle
收尾也不 done。LLM outage 就足以触发。

修法是追加式的:既有 INTERRUPTED 出口一个字节不改(`TASK_INTERRUPTED` 照发、槽位
照放),只在末尾多安排一次退避重排;预算耗尽落 `FAILED`——响亮失败,会话得以收敛。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.models.discriminators import InterruptReason, TaskErrorCode
from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import PORT_MAIN, PORT_NONE, Task
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.core.orchestrator.task.runner import AgentBinding


class _IdleRunner:
    """装配即空转——本文件只关心 INTERRUPTED 之后的调度，不关心 run 内部。"""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def assemble(self, task_id: str) -> AgentBinding | None:
        return AgentBinding(agent_id=f"ag-{task_id}")

    async def execute(self, binding: AgentBinding, task_id: str):
        self.dispatched.append(task_id)
        return None


def _tm(*, requeue_max: int = 3, base: float = 0.01) -> TaskManager:
    tm = TaskManager(
        session_id="s1",
        autonomous_requeue_max=requeue_max,
        autonomous_requeue_backoff_base_sec=base,
    )
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING", root_agent_id="root"))
    tm.set_runner(_IdleRunner())
    return tm


def _autonomous(tid: str = "BG") -> Task:
    return Task(
        id=tid, session_id="s1", status="ACTIVE",
        assigned_agent_id=f"ag-{tid}", creator_agent_id=f"ag-{tid}",
        unattended=True, port_key=PORT_NONE,
    )


def _attended(tid: str = "CHAT") -> Task:
    return Task(
        id=tid, session_id="s1", status="ACTIVE",
        assigned_agent_id=f"ag-{tid}", creator_agent_id=f"ag-{tid}",
        unattended=False, port_key=PORT_MAIN,
    )


async def _interrupt(tm: TaskManager, task: Task) -> str:
    """走真实路径把一个在跑的 task 打断成 INTERRUPTED（outage 的形状）。

    `retriable=False` 是 outage 的契约（见 `disposition_for`）：它不许原地立即重试，
    于是 disposition 表直接给出 INTERRUPTED——这正是洞的入口。
    """
    tm.register_task(task)
    tm._running_tasks.add(task.id)
    status = await tm.apply_run_outcome(task.id, RunOutcome(
        kind=RunOutcomeKind.INTERRUPTED,
        reason=InterruptReason.LLM_OUTAGE,
        error_code=InterruptReason.LLM_OUTAGE,
        error="LLM outage", retriable=False,
    ))
    assert status == "INTERRUPTED"
    await tm._settle(task.id, status)
    return status


async def test_autonomous_task_comes_back_by_itself() -> None:
    """**本修的要害**：没有任何外部动作，自治作业自己回到队列。"""
    tm = _tm()
    task = _autonomous()
    await _interrupt(tm, task)

    # 立刻：还停着，但已经有一个退避定时器在飞
    assert task.status == "INTERRUPTED"
    assert "BG" in tm._autonomous_requeue_timers

    await asyncio.sleep(0.08)      # 退避 base=0.01 → 首轮 0.01s

    assert task.interrupt_requeue_count == 1
    # 已被派发（drain 在重排后立刻跑）或至少已离开 INTERRUPTED
    assert task.status != "INTERRUPTED"


async def test_attended_task_still_waits_for_resume() -> None:
    """不回归：有对端的 task 照旧停在 INTERRUPTED 等 `/resume`，不自动重排。"""
    tm = _tm()
    task = _attended()
    await _interrupt(tm, task)

    await asyncio.sleep(0.08)

    assert task.status == "INTERRUPTED"
    assert task.interrupt_requeue_count == 0
    assert tm._autonomous_requeue_timers == {}


async def test_budget_exhausted_fails_loudly_and_session_converges() -> None:
    """预算耗尽 → FAILED + 专属结局码。**不能**停在一个没人会碰的非终态上。"""
    tm = _tm(requeue_max=2)
    task = _autonomous()
    task.interrupt_requeue_count = 2          # 预算已用完
    await _interrupt(tm, task)

    assert task.status == "FAILED"
    assert task.error_code == TaskErrorCode.AUTONOMOUS_REQUEUE_EXHAUSTED
    # 会话得以收敛：队列空、无在跑、无非终态停顿
    assert tm.is_done()
    assert not tm._blocked_or_interrupted()
    # 计入失败账（阈值已因此从 3 提到 5，见 Session.failure_threshold）
    assert tm.session is not None and tm.session.failure_counter == 1


async def test_cancel_all_kills_the_pending_timer() -> None:
    """`cancel_all` 硬取消在飞的定时器——否则 session 关闭要等满退避。"""
    tm = _tm(base=30.0)                        # 真实退避，靠取消而不是等待
    task = _autonomous()
    await _interrupt(tm, task)
    timer = tm._autonomous_requeue_timers.get("BG")
    assert timer is not None

    await tm.cancel_all(reason="test")

    assert tm._autonomous_requeue_timers == {}
    await asyncio.sleep(0)
    assert timer.cancelled() or timer.done()


async def test_timer_does_not_touch_a_task_someone_else_already_handled() -> None:
    """退避期间世界会变：task 已被别的路径处理 → 定时器醒来什么都不做。"""
    tm = _tm()
    task = _autonomous()
    await _interrupt(tm, task)
    # 模拟 /resume 或 restore 先把它捡走了
    task.status = "PENDING"

    await asyncio.sleep(0.08)

    assert task.interrupt_requeue_count == 0   # 定时器没有二次重排
    assert task.status == "PENDING"


async def test_no_duplicate_timer_for_the_same_task() -> None:
    """同一个 task 不叠第二个定时器（两处 INTERRUPTED 出口都可能调进来）。"""
    tm = _tm(base=30.0)
    task = _autonomous()
    await _interrupt(tm, task)
    first = tm._autonomous_requeue_timers["BG"]

    assert await tm._schedule_autonomous_requeue("BG") is False
    assert tm._autonomous_requeue_timers["BG"] is first

    await tm.cancel_all(reason="cleanup")
