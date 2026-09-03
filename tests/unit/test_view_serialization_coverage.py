"""快照必须覆盖每个 View 的全部字段（总账 A2 / B3）。

serialize_view / deserialize_view 是手写字典字面量，没有 dataclasses.fields()
遍历 —— 漏一个字段完全静默。这道守卫把「漏」变成红灯。
"""

from __future__ import annotations

import dataclasses

from ctx_weft.core.control.reducers import deserialize_view, serialize_view
from ctx_weft.core.control.types import AgentView, RunStateView, SessionView, TaskView

#: 有意不进快照的字段 —— 每条都要写明理由。
_EXEMPT: dict[str, set[str]] = {
    "RunStateView": {
        "sessions", "tasks", "agents",   # 容器，各自单独序列化
        "target_event_id", "events_replayed",  # replay 专用，不属于快照状态
    },
}


def _field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _make_view() -> RunStateView:
    view = RunStateView(run_id="run_1", session_id="sess_1", task_id="tsk_1", agent_id="agt_1")
    view.sessions["sess_1"] = SessionView(id="sess_1")
    view.tasks["tsk_1"] = TaskView(id="tsk_1", session_id="sess_1")
    view.agents["agt_1"] = AgentView(
        id="agt_1", spawn_depth=1, parent_agent_id="agt_0",
        template_id="tpl", llm_account="acct", llm_model="mdl",
    )
    return view


def test_serialize_covers_every_agent_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["agents"]["agt_1"])
    missing = _field_names(AgentView) - written - _EXEMPT.get("AgentView", set())
    assert missing == set(), f"AgentView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_task_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["tasks"]["tsk_1"])
    missing = _field_names(TaskView) - written - _EXEMPT.get("TaskView", set())
    assert missing == set(), f"TaskView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_session_view_field():
    blob = serialize_view(_make_view())
    written = set(blob["sessions"]["sess_1"])
    missing = _field_names(SessionView) - written - _EXEMPT.get("SessionView", set())
    assert missing == set(), f"SessionView 字段没进快照: {sorted(missing)}"


def test_serialize_covers_every_run_state_view_field():
    blob = serialize_view(_make_view())
    missing = _field_names(RunStateView) - set(blob) - _EXEMPT["RunStateView"]
    assert missing == set(), f"RunStateView 字段没进快照: {sorted(missing)}"


def test_agent_llm_choice_survives_snapshot_round_trip():
    """A2 的正面表述：模型选择必须过得了快照往返。"""
    rebuilt = deserialize_view(serialize_view(_make_view()))
    agent = rebuilt.agents["agt_1"]
    assert agent.llm_account == "acct"
    assert agent.llm_model == "mdl"


def test_round_trip_preserves_all_agent_fields():
    original = _make_view().agents["agt_1"]
    rebuilt = deserialize_view(serialize_view(_make_view())).agents["agt_1"]
    assert dataclasses.asdict(rebuilt) == dataclasses.asdict(original)
