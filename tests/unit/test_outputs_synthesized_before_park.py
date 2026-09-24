"""回归（2026-09-24）：park 之前必须先合成 `task.outputs`。

## bug 的形状

`_cold_park` 抛 `HitlPark` 释放协程，`ActStep.execute` 就地中断——循环**之后**那次
`_synthesize_final_outputs` 永远执行不到。于是 park 的 task 恒 `outputs=None`。

`report_task_outcome` 有一道 success-without-outputs 护栏（「没有最终产出就不许判成功」），
它读的正是 `task.outputs`。两者一凑：**park 的 task 永远不会被判成功终结**——observer
报 success，工具就地改判 retry，task 停在 park 等一个其实已经不需要的人，DAG 后继永远
放不行。

线上日志里的形态是同一次工具调用的这两行对不上：

    arguments = {'task_status': 'success', ...}
    result    = "Assessment recorded: outcome=retry. ..."

这条缺陷让 S5/S6 的核心行为（judge success → 终结 + 放行后继）从未生效过。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.capabilities.control_tools import (
    ControlContext, ControlMetaKey as K, report_task_outcome,
)
from ctx_weft.core.loop.steps.act import _synthesize_final_outputs
from ctx_weft.core.models.task import NormalTaskSettings, Task


def _state(task: Task) -> SimpleNamespace:
    return SimpleNamespace(task=task, extra={})


def _task(**kw) -> Task:
    return Task(id="t1", session_id="s1", status="ACTIVE",
                settings=NormalTaskSettings(), **kw)


def _turn(text: str) -> SimpleNamespace:
    return SimpleNamespace(turn=1, messages_sent=[], assistant_text=text,
                           tool_calls=[], usage=None)


# ── 合成本身 ──────────────────────────────────────────────────────────────────

def test_plain_text_turn_becomes_outputs() -> None:
    state = _state(_task())
    _synthesize_final_outputs(state, [_turn("这就是给用户的答复")])
    assert state.task.outputs == "这就是给用户的答复"
    assert state.extra["final_body"] == "这就是给用户的答复"


def test_is_idempotent() -> None:
    """循环里 park 前调一次、循环外收尾再调一次——两次结果必须一致。"""
    state = _state(_task())
    transcript = [_turn("答复")]
    _synthesize_final_outputs(state, transcript)
    first = state.task.outputs
    _synthesize_final_outputs(state, transcript)
    assert state.task.outputs == first


def test_suspended_task_is_skipped() -> None:
    """delegate-suspend 不产最终输出，维持既有行为。"""
    task = _task()
    task.suspend_requested = True
    state = _state(task)
    _synthesize_final_outputs(state, [_turn("半截话")])
    assert state.task.outputs is None


def test_empty_transcript_is_skipped() -> None:
    state = _state(_task())
    _synthesize_final_outputs(state, [])
    assert state.task.outputs is None


# ── 与护栏的合流：bug 的核心 ──────────────────────────────────────────────────

def _verdict(task: Task) -> str:
    """模拟后台 observe（readonly）报 success，返回工具**实际**落定的判决。"""
    ctx = ControlContext(session_id="s1", task_id="t1", agent_id="ag1", task=task,
                         task_manager=None, session=None, readonly=True)
    res = report_task_outcome(task_status="success", act_recap="说了段话",
                              task_summary="小结", ctx=ctx)
    return res.metadata[K.OBSERVER_OUTCOME]


def test_without_outputs_the_guard_downgrades_success() -> None:
    """前提复现：没有 outputs 时护栏确实把 success 改判 retry。"""
    assert _verdict(_task()) == "retry"


def test_with_outputs_synthesized_success_survives() -> None:
    """**本次修复的要害**：park 前合成过 outputs，同一份 success 就能活下来。"""
    state = _state(_task())
    _synthesize_final_outputs(state, [_turn("给用户的答复")])
    assert _verdict(state.task) == "success"


@pytest.mark.parametrize("unattended", [False, True])
async def test_act_sets_outputs_on_plain_text_turn(unattended: bool) -> None:
    """端到端走一遍 ActStep：有人在场（park）与无人值守（直接收尾）都必须有 outputs。

    `unattended=False` 那一格正是回归点——它会 park，而 park 抛 `HitlPark`。
    """
    from ctx_weft.core.loop.park import HitlPark
    from ctx_weft.core.loop.steps.act import ActStep
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
    from tests.integration.test_interactive_task import _act_state_ctx

    llm = MockLLMAdapter(responses=[MockResponse(text="给用户的答复")])
    state, ctx, task, _hitl, _mem = _act_state_ctx(unattended, llm)

    try:
        await ActStep().execute(state, ctx)
    except HitlPark:
        pass                       # 有人在场时 park 是预期出口

    assert task.outputs == "给用户的答复", (
        f"unattended={unattended} 时 outputs 没合成——park 路径上它会让护栏把 "
        f"success 改判 retry，task 永远终结不了")
