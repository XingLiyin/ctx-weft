"""Root-task fold: at an agent root's finalize, supersede its sub-task dispatch
records in the agent layer, keeping only the root self-experience (and any prior
roots' self-experiences). Sub-task = any task spawned during the root's lifetime,
both use_subagent kinds.

Discriminator (no tagging, no parent-chain walk): a dispatch record is KEPT iff the
task it points at is itself an agent root —
    parent_task_id is None  OR  (assigned_agent_id == A and creator != assigned)
— otherwise it is folded (superseded).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.loop.steps.finalize import (
    _is_agent_root_task,
    fold_root_subtree,
    record_root_self_experience,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str, agent_id: str = "A") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


def _task(task_id, *, parent=None, assigned="A", creator="A") -> Task:
    return Task(
        id=task_id, session_id="s1", status="ACTIVE", tenant_id="default",
        assigned_agent_id=assigned, creator_agent_id=creator, parent_task_id=parent,
        title=task_id, description="", user_prompt="do it", settings=NormalTaskSettings(),
    )


# ── _is_agent_root_task ─────────────────────────────────────────────────────


def test_session_root_is_agent_root() -> None:
    assert _is_agent_root_task(_task("R", parent=None, assigned="A", creator="A"), "A")


def test_subagent_root_is_agent_root() -> None:
    # delegated across agent boundary: assigned to A, created by its parent agent
    assert _is_agent_root_task(_task("R", parent="p", assigned="A", creator="P"), "A")


def test_self_derived_task_is_not_agent_root() -> None:
    # use_subagent=False: ran on A, created by A
    assert not _is_agent_root_task(_task("C", parent="R", assigned="A", creator="A"), "A")


def test_subagent_child_is_not_root_from_creators_view() -> None:
    # use_subagent=True child D (assigned to B) is NOT A's root when judged from A
    assert not _is_agent_root_task(_task("D", parent="R", assigned="B", creator="A"), "A")


# ── provider.supersede ──────────────────────────────────────────────────────


async def test_supersede_hides_events_by_id() -> None:
    mem = InMemoryMemoryProvider()
    sc = _sc("t1")
    id1 = await mem.ingest(MemoryEvent(type=T.TASK_DISPATCH, scope=sc, content="a",
                                       timestamp=_BASE, role="assistant",
                                       metadata={"tool_call_id": "tc1"}), _ctx())
    await mem.ingest(MemoryEvent(type=T.TASK_DISPATCH, scope=sc, content="b",
                                 timestamp=_BASE + timedelta(seconds=1), role="assistant",
                                 metadata={"tool_call_id": "tc2"}), _ctx())

    n = await mem.supersede([id1], _ctx())
    assert n == 1
    remaining = await mem.recall_recent(sc, [T.TASK_DISPATCH], 100, _ctx())
    assert [r.content for r in remaining] == ["b"]


# ── fold_root_subtree ───────────────────────────────────────────────────────


async def _seed_dispatch_pair(mem, agent_scope, tool_call_id, child_task_id, ts) -> None:
    """A delegated a child: dispatch call + its result, both in A's agent layer."""
    await mem.ingest(MemoryEvent(type=T.TASK_DISPATCH, scope=agent_scope, content="delegate",
                                 timestamp=_BASE + timedelta(seconds=ts), role="assistant",
                                 metadata={"tool_call_id": tool_call_id}), _ctx())
    await mem.ingest(MemoryEvent(type=T.TASK_DISPATCH_RESULT, scope=agent_scope, content="child out",
                                 timestamp=_BASE + timedelta(seconds=ts + 1), role="tool",
                                 metadata={"tool_call_id": tool_call_id, "child_task_id": child_task_id}), _ctx())


async def test_fold_supersedes_both_subtask_kinds_keeps_prior_root() -> None:
    mem = InMemoryMemoryProvider()
    R = _task("R", parent=None, assigned="A", creator="A")  # A's root
    agent_scope = _sc("R", "A")

    # sub-tasks spawned during R (both kinds) → must be folded
    await _seed_dispatch_pair(mem, agent_scope, "tcC", "C", ts=0)   # use_subagent=False child
    await _seed_dispatch_pair(mem, agent_scope, "tcD", "D", ts=2)   # use_subagent=True child
    # a *prior* root's self-experience pair (points at a root task) → must be kept
    await _seed_dispatch_pair(mem, agent_scope, "tcRk", "Rk", ts=4)

    tasks = {
        "C": _task("C", parent="R", assigned="A", creator="A"),   # not a root
        "D": _task("D", parent="R", assigned="B", creator="A"),   # not A's root
        "Rk": _task("Rk", parent="pp", assigned="A", creator="P"),  # a prior A-root
    }
    n = await fold_root_subtree(mem, agent_scope, R, tasks.get, _ctx())

    assert n == 4  # C's pair (2) + D's pair (2)
    remaining = await mem.recall_recent(agent_scope, [T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT], 100, _ctx())
    # only the prior root's pair survives
    tcids = {r.metadata.get("tool_call_id") for r in remaining}
    assert tcids == {"tcRk"}
