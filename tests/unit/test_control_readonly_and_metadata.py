"""S3：`report_task_outcome` 的只读模式与结构化回传。

为什么需要只读：`ControlContext.task` 是从 `TaskManager._tasks` 取的**活对象**，不是
快照。后台 observe 是 fire-and-forget、常在主 run 收尾之后才跑到，它若调用写 task 的
工具就是隔着时间改主线程状态——而它的判决还要先过带外入口的仲裁（人可能已经开口重排
了这个 task）。判决改走 `ControlResult.metadata`，由 TaskManager 在仲裁通过后统一写。

本文件同时**接手了 `test_collect_process_report.py` 的职责**：那个工具在 S5 被删——它的
签名本就是 `report_task_outcome` 的真子集，存在的唯一理由「Zero state write」正是这里的
`readonly` 所保证的（见下面「只读：一个字段都不写」那一组）。
"""

from __future__ import annotations

from ctx_weft.core.capabilities.control_tools import (
    PROVIDER_NAME,
    ControlCapabilityProvider,
    ControlContext,
    ControlMetaKey as K,
    report_task_outcome,
)
from ctx_weft.core.loop.driver import LoopContext
from ctx_weft.core.loop.steps.background_observe import _readonly_ctx
from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import Task
from ctx_weft.protocols.context import ProviderContext


def _task(**kw) -> Task:
    kw.setdefault("outputs", "交付物")
    return Task(id="t1", session_id="s1", status="ACTIVE", **kw)


def _ctx(task: Task, *, readonly: bool = False) -> ControlContext:
    return ControlContext(
        session_id="s1", task_id=task.id, agent_id="ag1",
        task=task, task_manager=None, session=None, readonly=readonly,
    )


# ── 前台：行为逐字不变 ────────────────────────────────────────────────────────

def test_foreground_still_writes_every_field() -> None:
    task = _task()
    report_task_outcome(
        task_status="success", act_recap="做了甲乙丙", task_summary="全程小结",
        ctx=_ctx(task),
    )
    assert task.observer_outcome == "success"
    assert task.process_report == "做了甲乙丙"
    assert task.task_summary == "全程小结"
    assert task.actor_done is True
    assert task.process_report_at is not None
    assert task.error is None


def test_foreground_retry_stashes_failure_reason() -> None:
    task = _task()
    report_task_outcome(
        task_status="continue", act_recap="卡住了", task_failure_reason="缺凭据",
        next_step_hint="先去取凭据", ctx=_ctx(task),
    )
    assert task.observer_outcome == "continue"
    assert task.error == "缺凭据"
    assert task.next_step_hint == "Next Step Hint: 先去取凭据"


# ── 只读：一个字段都不写 ──────────────────────────────────────────────────────

def test_readonly_writes_nothing_to_task() -> None:
    task = _task()
    report_task_outcome(
        task_status="success", act_recap="做了甲乙丙", task_summary="全程小结",
        ctx=_ctx(task, readonly=True),
    )
    assert task.observer_outcome is None
    assert task.process_report is None
    assert task.task_summary is None
    assert task.next_step_hint is None
    assert task.process_report_at is None
    assert task.actor_done is False


def test_readonly_still_runs_the_no_outputs_guard() -> None:
    """护栏读的是 `task.outputs`、改的是本地判决，只读照跑——否则后台会把没交付的判成功。"""
    task = _task(outputs=None)
    res = report_task_outcome(
        task_status="success", act_recap="其实没交付",
        ctx=_ctx(task, readonly=True),
    )
    assert res.metadata[K.OBSERVER_OUTCOME] == "continue"
    assert "without a final output" in res.metadata[K.OBSERVER_NEXT_STEP_HINT]
    assert task.observer_outcome is None       # 仍然一个字段都没写


# ── 结构化回传 ────────────────────────────────────────────────────────────────

def test_metadata_carries_the_verdict_in_both_modes() -> None:
    """`content` 是给 LLM 看的话术，形状不稳定；调用方该从 metadata 取。"""
    for readonly in (False, True):
        task = _task()
        res = report_task_outcome(
            task_status="fail", act_recap="查清了原因", task_summary="小结",
            task_failure_reason="外部接口已下线", next_step_hint="别再试了",
            ctx=_ctx(task, readonly=readonly),
        )
        assert res.metadata[K.OBSERVER_OUTCOME] == "fail"
        assert res.metadata[K.OBSERVER_ACT_RECAP] == "查清了原因"
        assert res.metadata[K.OBSERVER_TASK_SUMMARY] == "小结"
        assert res.metadata[K.OBSERVER_FAILURE_REASON] == "外部接口已下线"
        assert res.metadata[K.OBSERVER_NEXT_STEP_HINT] == "Next Step Hint: 别再试了"


def test_act_recap_is_recoverable_without_parsing_content() -> None:
    """后台拿不到 task 字段，只能靠 metadata——content 里 recap 是被话术包着的。"""
    task = _task()
    res = report_task_outcome(
        task_status="success", act_recap="纯净的 recap", task_summary="x",
        ctx=_ctx(task, readonly=True),
    )
    assert res.metadata[K.OBSERVER_ACT_RECAP] == "纯净的 recap"
    assert res.content != "纯净的 recap"        # content 带前缀，不能直接当 recap 用
    assert "纯净的 recap" in res.content


# ── 标记的传递 ────────────────────────────────────────────────────────────────

def _loop_ctx(extra: dict | None = None) -> LoopContext:
    return LoopContext(
        assembler=None, llm=None, memory=None, event_bus=None,
        provider_ctx=ProviderContext(session_id="s1", extra=extra or {}),
    )


def test_readonly_ctx_sets_the_flag_and_keeps_existing_extra() -> None:
    ctx = _loop_ctx({"已有": "值"})
    ro = _readonly_ctx(ctx)
    assert ro.provider_ctx.extra["control_readonly"] is True
    assert ro.provider_ctx.extra["已有"] == "值"
    # 原 ctx 不被就地改写——主 run 还要接着用它
    assert "control_readonly" not in ctx.provider_ctx.extra


def test_readonly_ctx_tolerates_missing_provider_ctx() -> None:
    """手构 ctx 的单测里 provider_ctx 可能是 None：原样返回，不强行造一个。"""
    ctx = _loop_ctx()
    ctx.provider_ctx = None
    assert _readonly_ctx(ctx) is ctx


# ── 端到端：extra → _handle → ControlContext.readonly ─────────────────────────
#
# 上面两组分别守住了「标记被设进 extra」与「readonly 生效」，中间还隔着 `_handle`
# 里读 extra 那一行——key 名写错正是这种地方最容易出的事，所以走一遍真 provider。

class _FakeTM:
    def __init__(self, task: Task) -> None:
        self._task = task

    def get_task(self, _tid: str) -> Task:
        return self._task


async def _invoke(task: Task, extra: dict) -> None:
    provider = ControlCapabilityProvider()
    provider.register_session(
        "s1", _FakeTM(task),
        Session(id="s1", tenant_id="default", user_prompt="x", status="RUNNING"),
    )
    ctx = ProviderContext(session_id="s1", task_id="t1", agent_id="ag1", extra=extra)
    async for _ev in provider.invoke(
        f"{PROVIDER_NAME}:report_task_outcome",
        {"task_status": "success", "act_recap": "做完了", "task_summary": "小结"},
        ctx,
    ):
        pass


async def test_readonly_flag_reaches_the_tool_through_provider() -> None:
    task = _task()
    await _invoke(task, {"control_readonly": True})
    assert task.observer_outcome is None
    assert task.process_report is None


async def test_without_the_flag_the_tool_writes_as_before() -> None:
    """对照组：同一条路径不带标记 → 照旧写 task，证明上一条测的是标记本身。"""
    task = _task()
    await _invoke(task, {})
    assert task.observer_outcome == "success"
    assert task.process_report == "做完了"


def test_readonly_ctx_tolerates_non_dataclass_stand_ins() -> None:
    """手构的替身 ctx（非 dataclass）不该把后台 observe 打挂——原样返回，只记 warning。"""
    from types import SimpleNamespace
    stand_in = SimpleNamespace(provider_ctx=SimpleNamespace(extra={}))
    assert _readonly_ctx(stand_in) is stand_in
