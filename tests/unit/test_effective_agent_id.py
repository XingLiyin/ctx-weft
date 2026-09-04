"""effective_agent_id 纯函数——「任务跑在哪个 agent scope」的单一真相。

五分支：subagent 已 assigned / subagent 未 assigned（占位 token）/
assigned / creator / root / 全空 per-task 兜底。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.task.runner import effective_agent_id
from ctx_weft.core.domain.models import NormalTaskSettings, Task


def _task(tid: str = "T", **kw) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", **kw)


def test_none_task_is_empty() -> None:
    assert effective_agent_id(None, "root") == ""


def test_subagent_assigned_wins() -> None:
    t = _task(settings=NormalTaskSettings(use_subagent=True))
    t.assigned_agent_id = "agt_sub"
    assert effective_agent_id(t, "root") == "agt_sub"


def test_subagent_unassigned_gets_unique_placeholder() -> None:
    a = _task("A", settings=NormalTaskSettings(use_subagent=True))
    b = _task("B", settings=NormalTaskSettings(use_subagent=True))
    assert effective_agent_id(a, "root") == "__sub__A"
    assert effective_agent_id(b, "root") == "__sub__B"


def test_non_subagent_assigned_over_creator_over_root() -> None:
    t = _task()
    t.assigned_agent_id = "agt_a"
    t.creator_agent_id = "agt_c"
    assert effective_agent_id(t, "root") == "agt_a"
    t.assigned_agent_id = ""
    assert effective_agent_id(t, "root") == "agt_c"
    t.creator_agent_id = ""
    assert effective_agent_id(t, "root") == "root"


def test_all_empty_falls_back_to_per_task_token() -> None:
    assert effective_agent_id(_task("T9"), "") == "__task__T9"
