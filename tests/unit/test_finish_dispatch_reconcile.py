"""finish_task 与 delegate/replan 同批出现时的批末仲裁：finish 胜出。

锁定 ActStep._reconcile_finish_vs_dispatch：
- 两类工具都出现 → 当前 task 复位为非 SUSPENDED（路由走 observe 收尾），
  被派发任务经 detach_staged 改投到当前 task 的 parent，task.outputs 保留。
- 只出现一类 → 不仲裁，行为不变。
- 顺序无关（finish 在前 / delegate 在前结果一致）。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.loop.steps.act import _reconcile_finish_vs_dispatch
from ctx_weft.core.orchestrator.control_capability import (
    DELEGATE_TASK_NAME,
    FINISH_TASK_NAME,
    REPORT_TASK_OUTCOME_NAME,
)
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.protocols import ToolCall


class _FakeTM:
    def __init__(self) -> None:
        self.detached: list[tuple[str, str | None]] = []

    def detach_staged(self, from_parent_id: str, to_parent_id: str | None) -> None:
        self.detached.append((from_parent_id, to_parent_id))


def _state(parent: str | None):
    settings = NormalTaskSettings()
    settings.spawn_titles = ["child"]
    task = SimpleNamespace(
        id="cur", parent_task_id=parent, status="SUSPENDED",
        actor_done=True, settings=settings, outputs="my final reply",
    )
    return SimpleNamespace(task=task)


def _tc(name: str) -> ToolCall:
    return ToolCall(id=f"tc_{name}", name=name, arguments={})


def _run(state, tm, tool_calls):
    ctx = SimpleNamespace(task_manager=tm)
    _reconcile_finish_vs_dispatch(state, ctx, tool_calls)


def test_finish_plus_delegate_finish_wins() -> None:
    state, tm = _state(parent="G"), _FakeTM()
    _run(state, tm, [_tc(FINISH_TASK_NAME), _tc(DELEGATE_TASK_NAME)])

    assert tm.detached == [("cur", "G")]              # 改投到当前 task 的 parent
    assert state.task.status == "ACTIVE"             # 撤销 SUSPENDED → 路由走 observe
    assert state.task.settings.spawn_titles == []    # 清脏状态
    assert state.task.outputs == "my final reply"    # finish 的产出保留


def test_root_task_detaches_to_top_level() -> None:
    state, tm = _state(parent=None), _FakeTM()
    _run(state, tm, [_tc(FINISH_TASK_NAME), _tc(DELEGATE_TASK_NAME)])

    assert tm.detached == [("cur", None)]            # root → 改投为顶层独立 root
    assert state.task.status == "ACTIVE"


def test_order_independent() -> None:
    """delegate 在前、finish 在后，结果与反序一致（只看本批是否两类都出现）。"""
    state, tm = _state(parent="G"), _FakeTM()
    _run(state, tm, [_tc(DELEGATE_TASK_NAME), _tc(FINISH_TASK_NAME)])

    assert tm.detached == [("cur", "G")]
    assert state.task.status == "ACTIVE"


def test_finish_only_no_reconcile() -> None:
    state, tm = _state(parent="G"), _FakeTM()
    _run(state, tm, [_tc(FINISH_TASK_NAME)])

    assert tm.detached == []                          # 没派发 → 不仲裁
    assert state.task.status == "SUSPENDED"           # 原样不动（此处仅断言未被仲裁触碰）


def test_delegate_only_no_reconcile() -> None:
    """仅 delegate（无 finish）→ 维持 suspend 派发语义，不被仲裁干预。"""
    state, tm = _state(parent="G"), _FakeTM()
    _run(state, tm, [_tc(DELEGATE_TASK_NAME)])

    assert tm.detached == []
    assert state.task.status == "SUSPENDED"
    assert state.task.settings.spawn_titles == ["child"]


def test_non_dispatch_control_tool_does_not_trigger() -> None:
    """finish + 非派发控制工具（report_task_outcome）不触发改投。"""
    state, tm = _state(parent="G"), _FakeTM()
    _run(state, tm, [_tc(FINISH_TASK_NAME), _tc(REPORT_TASK_OUTCOME_NAME)])

    assert tm.detached == []
    assert state.task.status == "SUSPENDED"
