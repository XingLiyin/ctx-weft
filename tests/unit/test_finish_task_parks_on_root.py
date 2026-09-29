"""S-b（2026-09-27）：root 调 `finish_task` 收尾时也让位 + 后台判定。

## 为什么要有这条

`finish_task` 在 root 上此前是**零复核**的：`observe._should_use_llm` 对
`parent_task_id is None` 降级走机械判决，而 `_mechanical_verdict` 把 `actor_done` 无条件
映射成 success；那道 success-without-outputs 护栏又长在 `report_task_outcome` 里、不在机械
判决的路上。于是同一个 agent 的两种收尾一个被判、一个不被：

    说了段话就停下      → park + 后台判定（S5）
    明确宣布「做完了」  → 直接 FINISHED，没人看一眼

后者恰恰是更该被看一眼的那个，而且这个不对称等于给 LLM 留了一个能绕开复核的开关。改成
同一条归宿：park，boundary=`finish_park`，判决说 success 才终结。

## 收窄到 root

与 S5 同一个办法：子任务的 `finish_task` 今天走前台 LLM observe，那条路有真复核、
`FinalizeStep` 也照常跑，先别动。代价是同一个子任务两种收尾走两条路。

## 本文件的判据从哪来

`exit_reason == "actor_done"` 是 act 从 `state.task.actor_done` 读出来的（真工具的副作用），
所以这里用一个只做那一件副作用的 gateway 替身把它摆出来——真 `finish_task` 的其余副作用
（写产出、发事件）与「这一回合该去 observe 还是该让位」这条路由无关。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.loop import background as background_pkg
from ctx_weft.core.loop.background import runner
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps.act import ActStep
from ctx_weft.protocols import ToolCall
from ctx_weft.protocols.hitl import PREFACE_NORMAL, UserTurnDelivery
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from tests.integration.test_interactive_task import _act_state_ctx

pytestmark = pytest.mark.asyncio


class _FinishTaskGateway:
    """只做 `finish_task` 的一件副作用：置 `task.actor_done`。

    act 的 `exit_reason = "actor_done"` 就是从这个字段读出来的（`ActStep.execute` 里
    `if state.task.actor_done`）。真工具还会写产出、发事件、登记 close synth，与本文件要钉的
    路由都无关——摆一个最小替身，免得把一条路由测试变成 control 工具的集成测试。
    """

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def invoke(self, *, tool_name, arguments, state, ctx, tool_call_id=None):
        self.invoked.append(tool_name)
        if tool_name.endswith("finish_task"):
            state.task.actor_done = True
        return SimpleNamespace(content="Task finished.", is_error=False, metadata={})


def _finish_llm() -> MockLLMAdapter:
    """一个 act 回合：带正文 + 一次 `finish_task`。

    正文不是装饰：`_synthesize_final_outputs` 拿它当 `task.outputs`，而那是 park 之前
    必须做完的事（见 `test_outputs_synthesized_before_park`）。
    """
    return MockLLMAdapter(responses=[MockResponse(
        text="都做完了，这是结果。",
        tool_calls=[ToolCall(id="tc_fin", name="control__finish_task",
                             arguments={"deliverables_summary": "d"})],
    )])


def _spy_launch(monkeypatch) -> list[str]:
    """拦下 background observe，只记 boundary。act 是在函数体内 import 的，patch 模块属性即中。"""
    import asyncio

    
    seen: list[str] = []

    def _fake(state, ctx, *, boundary=""):
        seen.append(boundary)
        return asyncio.ensure_future(asyncio.sleep(0))

    monkeypatch.setattr(background_pkg, "launch_recap", _fake, raising=False)
    return seen


# ── ① root + 有人在场：让位，boundary=finish_park ─────────────────────────────

async def test_root_finish_task_parks_with_the_finish_park_boundary(monkeypatch) -> None:
    launched = _spy_launch(monkeypatch)
    gw = _FinishTaskGateway()
    state, ctx, task, hitl, _mem = _act_state_ctx(False, _finish_llm(), gateway=gw)

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert gw.invoked == ["control__finish_task"], "前提不成立：那次工具调用没发生"
    assert launched == ["finish_park"], (
        "root 的 finish_task 必须起 `finish_park` 边界的后台判定；"
        f"实为 {launched}——空=压根没让位，'plain_text'=走错了让位入口")

    pend = hitl.list_pending("s1")
    assert len(pend) == 1
    assert pend[0].form == "wait"
    assert pend[0].delivery == UserTurnDelivery(task_id="t1", preface=PREFACE_NORMAL), (
        "让位的气泡形态必须与纯文本那条逐字相同——两者归宿一样，投递路径也该一样")

    # park 不写 task 状态（Task 4）：AWAITING_HUMAN 由 TaskManager 据 RunOutcome 落。
    assert task.status == "ACTIVE"
    # 产出必须在 park **之前**合成好：否则 `report_task_outcome` 的 success-without-outputs
    # 护栏会把判决改判 retry，这个 task 永远终结不了（2026-09-24 那个坑的同形复现）。
    assert task.outputs, "park 之前没合成 outputs——判决会被护栏改判 retry"


async def test_no_observe_role_means_no_yielding(monkeypatch) -> None:
    """模板没有 ROLE → **不让位**，照旧回 observe（2026-09-28）。

    这条不是「少一个可选特性」，是防挂死：park 之后前台 ObserveStep 跑不到
    （`HitlPark` 在 `runtime._run_loop` unwind 的是 `driver.run` 整个循环），于是 park
    之后唯一的判决来源就是后台那次带外判决；而没有 ROLE 时后台判定档已降成只摘要档
    （`_judges` 与 `has_observe_role` 相与）。两头一凑就是「让了位却没人判」，task 永远
    停在 AWAITING_HUMAN。

    不让位则一切照旧：机械判决拿 `actor_done` 判 success，`FinalizeStep` 正常跑——即
    S-b 之前那条本来就在的路。
    """
    launched = _spy_launch(monkeypatch)
    gw = _FinishTaskGateway()
    state, ctx, task, hitl, _mem = _act_state_ctx(
        False, _finish_llm(), gateway=gw, observe_role=False)

    outcome = await ActStep().execute(state, ctx)  # 不抛 HitlPark

    assert gw.invoked == ["control__finish_task"], "前提不成立：那次工具调用没发生"
    assert outcome.next_step == "observe"
    assert outcome.state_patch["act_exit_reason"] == "actor_done"
    assert launched == [], "没让位就不该起 finish_park 的后台判定"
    assert hitl.list_pending("s1") == [], "没让位就不该挂等人的气泡"


async def test_the_round_ends_with_two_completed_events(monkeypatch) -> None:
    """这一回合发**两条** `ACT_TURN_COMPLETED`：先 `tool_calls_processed`、再 `await_user`。

    刻意的，别为了「一回合一条」删掉哪一条：工具确实处理完了，然后这一回合以让位收尾，
    两句都是实话。那条「一回合一条」的纪律来自纯文本路径——那里 `await_user` 与 `stop` 是
    互斥的两个结局，本路径不适用。
    """
    from ctx_weft.protocols.events import EventType

    _spy_launch(monkeypatch)
    seen: list[str] = []
    state, ctx, _task, _hitl, _mem = _act_state_ctx(
        False, _finish_llm(), gateway=_FinishTaskGateway())

    real_emit = ctx.event_bus.emit

    async def _emit(event):
        if event.type == EventType.ACT_TURN_COMPLETED:
            seen.append((event.payload or {}).get("reason"))
        await real_emit(event)

    ctx.event_bus.emit = _emit
    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert seen == ["tool_calls_processed", "await_user"], seen


# ── ② 三条反例：不该让位的照旧走 observe ──────────────────────────────────────

async def test_a_subtask_finish_task_does_not_park(monkeypatch) -> None:
    """子任务照旧走前台 observe——那条路有真 LLM 复核，S-b 刻意没动它。"""
    launched = _spy_launch(monkeypatch)
    state, ctx, _task, hitl, _mem = _act_state_ctx(
        False, _finish_llm(), parent_task_id="tsk_parent", gateway=_FinishTaskGateway())

    outcome = await ActStep().execute(state, ctx)      # 不抛 HitlPark

    assert outcome.next_step == "observe"
    assert outcome.state_patch["act_exit_reason"] == "actor_done"
    assert launched == [], "子任务不让位，就不该起让位那条后台判定"
    assert hitl.list_pending("s1") == []


async def test_an_unattended_root_finish_task_does_not_park(monkeypatch) -> None:
    """没人在场，让位就是 park 到死——判据与纯文本那条逐字相同。"""
    launched = _spy_launch(monkeypatch)
    state, ctx, _task, hitl, _mem = _act_state_ctx(
        True, _finish_llm(), gateway=_FinishTaskGateway())

    outcome = await ActStep().execute(state, ctx)

    assert outcome.next_step == "observe"
    assert launched == []
    assert hitl.list_pending("s1") == []


async def test_without_a_hitl_service_it_falls_back_to_observe(monkeypatch) -> None:
    """没有 hitl 就没有让位这回事（嵌入式调用方可以完全不装 HITL）。"""
    launched = _spy_launch(monkeypatch)
    state, ctx, _task, _hitl, _mem = _act_state_ctx(
        False, _finish_llm(), gateway=_FinishTaskGateway())
    ctx.hitl = None

    outcome = await ActStep().execute(state, ctx)

    assert outcome.next_step == "observe"
    assert launched == []


async def test_a_suspending_round_still_goes_to_suspend(monkeypatch) -> None:
    """派发了子任务的那一轮该去 suspend，不该被这条新路由截走。

    `suspend_requested` 与 `actor_done` 可以同时为真（actor 一轮里既派发又宣布完成），
    而路由的第一个判据是 `next_step == "observe"`——挂起优先。
    """
    launched = _spy_launch(monkeypatch)

    class _DelegatingGateway(_FinishTaskGateway):
        async def invoke(self, **kw):
            res = await super().invoke(**kw)
            kw["state"].task.suspend_requested = True
            return res

    state, ctx, _task, hitl, _mem = _act_state_ctx(
        False, _finish_llm(), gateway=_DelegatingGateway())

    outcome = await ActStep().execute(state, ctx)

    assert outcome.next_step == "suspend"
    assert launched == [], "挂起的那一轮不该让位"
    assert hitl.list_pending("s1") == []
