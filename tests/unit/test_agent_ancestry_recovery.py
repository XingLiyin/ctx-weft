"""Recovery derives ancestry from relationships, never task clocks or ordering."""

from datetime import UTC, datetime
from itertools import permutations

import pytest

from ctx_weft.core.control.reducers import apply_events
from ctx_weft.core.control.types import AgentView, RunStateView, SessionView, TaskView


@pytest.mark.parametrize("order", list(permutations(["parent", "child", "grandchild"])))
@pytest.mark.parametrize("times", [
    [None, None, None],
    [datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC),
     datetime(2026, 1, 1, tzinfo=UTC)],
    [datetime(2026, 3, 1, tzinfo=UTC), None, datetime(2026, 1, 1)],
])
def test_ancestry_ignores_task_order_and_timestamps(order, times):
    view = RunStateView(run_id="s", session_id="s", task_id="", agent_id="", sessions={
        "s": SessionView(id="s", root_agent_id="root")})
    creators = {"parent": "root", "child": "parent", "grandchild": "child"}
    tasks = {
        aid: TaskView(id=aid, session_id="s", assigned_agent_id=aid,
                      creator_agent_id=creators[aid], settings_raw={"use_subagent": True},
                      created_at=timestamp)
        for aid, timestamp in zip(creators, times, strict=True)
    }
    view.tasks = {aid: tasks[aid] for aid in order}
    apply_events([], view)
    for aid, depth in [("parent", 1), ("child", 2), ("grandchild", 3)]:
        assert view.agents[aid].parent_agent_id == creators[aid]
        assert view.agents[aid].spawn_depth == depth


def test_snapshot_ancestry_survives_originating_task_pruning():
    view = RunStateView(run_id="s", session_id="s", task_id="", agent_id="", sessions={
        "s": SessionView(id="s", root_agent_id="root")}, agents={
        "parent": AgentView(id="parent", parent_agent_id="root", spawn_depth=1),
        "child": AgentView(id="child", parent_agent_id="parent", spawn_depth=2),
    }, tasks={
        "conversation": TaskView(id="conversation", session_id="s",
                                 assigned_agent_id="child", creator_agent_id="child"),
        "new": TaskView(id="new", session_id="s", assigned_agent_id="grandchild",
                        creator_agent_id="child", settings_raw={"use_subagent": True}),
    })
    apply_events([], view)
    assert view.agents["child"].parent_agent_id == "parent"
    assert view.agents["child"].spawn_depth == 2
    assert view.agents["grandchild"].spawn_depth == 3
