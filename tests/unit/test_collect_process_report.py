"""collect_process_report：零状态写——只回传内容，不碰 task 任何状态字段。"""
from __future__ import annotations

from ctx_weft.core.capabilities.control_tools import (
    collect_process_report, BACKGROUND_PROCESS_REPORT_NAME,
)
from ctx_weft.core.models.task import NormalTaskSettings, Task


def _task() -> Task:
    return Task(id="t1", session_id="s1", status="RUNNING", assigned_agent_id="a1",
                creator_agent_id="a1", title="T", settings=NormalTaskSettings())


class _Ctx:
    def __init__(self, task): self.task = task


def test_collect_process_report_returns_content_unchanged():
    res = collect_process_report("段①：读了 auth，改了 3 处。", ctx=_Ctx(_task()))
    assert res.content == "段①：读了 auth，改了 3 处。"


def test_collect_process_report_zero_state_write():
    t = _task()
    before = (t.status, t.observer_outcome, t.actor_done, t.process_report, t.error)
    collect_process_report("任意报告", ctx=_Ctx(t))
    after = (t.status, t.observer_outcome, t.actor_done, t.process_report, t.error)
    assert before == after, "collect_process_report 不得写 task 任何状态字段"


def test_name_qualified():
    assert BACKGROUND_PROCESS_REPORT_NAME.endswith("collect_process_report")
