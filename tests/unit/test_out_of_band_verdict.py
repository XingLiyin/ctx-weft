"""S4.1 带外判决入口：一条不属于任何活跃 run 的结局。

park 之后 run 就地结束（`_cold_park` 抛 `HitlPark` 释放协程），后台 observe 判完时这个
task 没有任何 run 在跑——`_close_report` 那条「同 run 内由 finalize 取用」的路走不通。

**仲裁是这道入口的要害**：`_inject_user_turn` 写 USER_PROMPT 时不等后台 observe，所以
「人先开口」是常态而非边角。判决到达时若 task 已不在 `AWAITING_HUMAN`，它就过时了。
"""

from __future__ import annotations

from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import Task
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.protocols.events import EventType


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _tm() -> tuple[TaskManager, _CapturingBus]:
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING", root_agent_id="root"))
    # `_settle` 末尾恒调 `drain()`：给一个哑 runner 过掉「未注册」守卫，再把并发置 0
    # 让 drain 空转——本文件测的是仲裁与状态转移，不是派发。
    tm.set_runner(object())
    tm._max_concurrent = 0
    return tm, bus


def _parked(tm: TaskManager, tid: str = "A", **kw) -> Task:
    task = Task(id=tid, session_id="s1", status="AWAITING_HUMAN",
                assigned_agent_id="root", creator_agent_id="root", **kw)
    tm.register_task(task)
    return task


def _success(**kw) -> RunOutcome:
    return RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict="success", **kw)


def _types(bus: _CapturingBus) -> list:
    return [e.type for e in bus.events]


# ── 被接受 ────────────────────────────────────────────────────────────────────

async def test_success_verdict_finishes_the_parked_task() -> None:
    tm, bus = _tm()
    task = _parked(tm, outputs="交付物")

    accepted = await tm.apply_out_of_band_verdict(
        "A", _success(summary="小结", outputs="交付物"),
        process_report="做了甲乙丙", task_summary="小结",
    )

    assert accepted is True
    assert task.status == "FINISHED"
    assert task.observer_outcome == "success"
    assert task.process_report == "做了甲乙丙"
    assert task.process_report_at is not None
    assert task.task_summary == "小结"
    assert EventType.TASK_FINISHED in _types(bus)


async def test_hint_is_cleared_when_not_supplied() -> None:
    """不给 hint 就清掉，不留上一轮的过期指令。"""
    tm, _ = _tm()
    task = _parked(tm, outputs="x")
    task.next_step_hint = "Next Step Hint: 上一轮的陈货"

    await tm.apply_out_of_band_verdict("A", _success())

    assert task.next_step_hint is None


# ── 被拒绝 ────────────────────────────────────────────────────────────────────

async def test_verdict_rejected_when_user_spoke_first() -> None:
    """人先开口 → `_inject_user_turn` 把它重排成 PENDING → 这份判决已过时。"""
    tm, bus = _tm()
    task = _parked(tm, outputs="交付物")
    task.status = "PENDING"                      # 模拟已被重排

    accepted = await tm.apply_out_of_band_verdict(
        "A", _success(), process_report="来晚了的 recap",
    )

    assert accepted is False
    assert task.status == "PENDING"              # 一个字段都没动
    assert task.observer_outcome is None
    assert task.process_report is None
    assert bus.events == []                      # 连事件都不该发


async def test_verdict_rejected_when_task_already_terminal() -> None:
    tm, _ = _tm()
    task = _parked(tm)
    task.status = "FINISHED"

    assert await tm.apply_out_of_band_verdict("A", _success()) is False
    assert task.observer_outcome is None


async def test_verdict_rejected_for_unknown_task() -> None:
    tm, _ = _tm()
    assert await tm.apply_out_of_band_verdict("不存在", _success()) is False


async def test_verdict_rejected_when_suspended_on_children() -> None:
    """SUSPENDED 等子任务不是「停下来等人」，带外判决不该认领它。"""
    tm, _ = _tm()
    task = _parked(tm)
    task.status = "SUSPENDED"

    assert await tm.apply_out_of_band_verdict("A", _success()) is False
    assert task.status == "SUSPENDED"


# ── 幂等 ──────────────────────────────────────────────────────────────────────

async def test_second_verdict_is_rejected() -> None:
    """第一次落定后 task 已不在 AWAITING_HUMAN，重复提交自然被仲裁挡掉。"""
    tm, bus = _tm()
    _parked(tm, outputs="x")

    assert await tm.apply_out_of_band_verdict("A", _success()) is True
    before = len(bus.events)
    assert await tm.apply_out_of_band_verdict("A", _success()) is False
    assert len(bus.events) == before


# ── retry / fail 维持 park（S5）────────────────────────────────────────────────

async def test_retry_verdict_keeps_it_parked() -> None:
    """观察者说「没做完」→ 字段落地，但状态不动：有人在场时该由人拍板下一步。

    这与 `test_retry_verdict_requeues_it` 的差别是那条用的是**旧**语义（走处置表重排）。
    S5 之后 retry 不再自动重跑，也不烧 `retry_count`——`max_retries` 是给无人值守的自动
    重跑设的失控护栏，不该被一段正常的多轮对话烧光。
    """
    tm, bus = _tm()
    task = _parked(tm, retry_count=0, max_retries=3)

    accepted = await tm.apply_out_of_band_verdict(
        "A", RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict="retry"),
        process_report="卡住了", next_step_hint="Next Step Hint: 先取凭据",
    )

    assert accepted is True                       # 判决被接受：字段落地了
    assert task.status == "AWAITING_HUMAN"        # 但仍在等人
    assert task.observer_outcome == "retry"
    assert task.process_report == "卡住了"
    assert task.next_step_hint == "Next Step Hint: 先取凭据"
    assert task.retry_count == 0                  # 预算一点没烧
    assert bus.events == []                       # 没有状态事件


async def test_fail_verdict_keeps_it_parked_too() -> None:
    """fail 同理：agent 说它做不到，也让人来决定，而不是系统直接判死。"""
    tm, bus = _tm()
    task = _parked(tm)

    accepted = await tm.apply_out_of_band_verdict(
        "A", RunOutcome(kind=RunOutcomeKind.COMPLETED, verdict="fail", error="外部接口下线"),
        process_report="查清了原因",
    )

    assert accepted is True
    assert task.status == "AWAITING_HUMAN"
    assert task.observer_outcome == "fail"
    assert bus.events == []
