"""Golden scenarios for root-task experience fold across dispatch topologies.

Verifies "nothing gets lost": across self-work, use_subagent=False/True children,
nested sub-agents, and an agent running multiple sequential roots —
  - live (before root finalize): a child's dispatch result is recallable;
  - after root finalize: the root's own experience survives; its sub-tasks are
    folded (superseded); other roots' experiences are NOT collateral-folded.

Drives the real finalize-order (fold_root_subtree → record_root_self_experience)
against InMemoryMemoryProvider with a fake task store. >3 assistant turns is used
so self-experience takes the synthesized dispatch-pair form — the case that shares
a type with the records being folded, hence the one most at risk of loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.loop.steps.finalize import (
    fold_root_subtree,
    record_root_self_experience,
)
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str, agent_id: str) -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


@dataclass
class World:
    """A tiny task tree + the shared memory, enough to drive finalize realistically."""

    mem: InMemoryMemoryProvider
    tasks: dict[str, Task] = field(default_factory=dict)
    _t: int = 0

    def task(self, tid: str, *, parent: str | None, assigned: str, creator: str) -> Task:
        t = Task(
            id=tid, session_id="s1", status="ACTIVE", tenant_id="default",
            assigned_agent_id=assigned, creator_agent_id=creator, parent_task_id=parent,
            title=tid, description="", user_prompt=f"do {tid}", settings=NormalTaskSettings(),
        )
        self.tasks[tid] = t
        return t

    def get(self, tid: str) -> Task | None:
        return self.tasks.get(tid)

    def _tick(self) -> int:
        self._t += 1
        return self._t

    async def report_child(self, parent_agent: str, parent_task: str, child_id: str) -> None:
        """A child finished and reported back into the parent agent's layer (dispatch pair)."""
        scope = _sc(parent_task, parent_agent)
        tcid = f"tc-{child_id}"
        await self.mem.ingest(MemoryEvent(type=T.TASK_DISPATCH, scope=scope, content="delegate",
                                          timestamp=_BASE + timedelta(seconds=self._tick()),
                                          role="assistant", metadata={"tool_call_id": tcid}), _ctx())
        await self.mem.ingest(MemoryEvent(type=T.TASK_DISPATCH_RESULT, scope=scope, content=f"{child_id} out",
                                          timestamp=_BASE + timedelta(seconds=self._tick()), role="tool",
                                          metadata={"tool_call_id": tcid, "child_task_id": child_id}), _ctx())

    async def seed_conversation(self, root: Task, n_assistant: int) -> None:
        scope = _sc(root.id, root.assigned_agent_id)
        await self.mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=scope, content=root.user_prompt,
                                          timestamp=_BASE + timedelta(seconds=self._tick()), role="user"), _ctx())
        for _ in range(n_assistant):
            await self.mem.ingest(MemoryEvent(type=T.LLM_RESPONSE, scope=scope, content="reply",
                                              timestamp=_BASE + timedelta(seconds=self._tick()),
                                              role="assistant", metadata={"tool_calls": []}), _ctx())

    async def finalize_root(self, root: Task, n_assistant: int = 5) -> tuple[int, dict]:
        """Mirror FinalizeStep order for an agent root: fold subtree, then record self-experience."""
        await self.seed_conversation(root, n_assistant)
        scope = _sc(root.id, root.assigned_agent_id)
        folded = await fold_root_subtree(self.mem, scope, root, self.get, _ctx())
        info = await record_root_self_experience(self.mem, scope, root, f"{root.id} out", "success", _ctx())
        return folded, info


async def _agent_result_children(mem, agent_id: str) -> list[str]:
    """child_task_id of the non-superseded TASK_DISPATCH_RESULT records in an agent's layer."""
    recs = await mem.recall_recent(_sc("ignored", agent_id), [T.TASK_DISPATCH_RESULT], 1000, _ctx())
    return sorted(r.metadata.get("child_task_id") for r in recs)


# ── Scenario 1: agent does the work itself, no sub-tasks ─────────────────────


async def test_self_work_only_records_experience_folds_nothing() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    R = w.task("R", parent=None, assigned="A", creator="A")  # session root

    folded, info = await w.finalize_root(R, n_assistant=5)

    assert folded == 0
    assert info["mode"] == "dispatch"  # >3 turns → synthesized self pair
    # R's own experience is present (nothing lost), and it points at R itself
    assert await _agent_result_children(mem, "A") == ["R"]


# ── Scenario 2: live visibility, then fold of use_subagent=False children ────


async def test_subagent_false_children_visible_live_then_folded() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    R = w.task("R", parent=None, assigned="A", creator="A")
    w.task("C1", parent="R", assigned="A", creator="A")
    w.task("C2", parent="R", assigned="A", creator="A")
    await w.report_child("A", "R", "C1")
    await w.report_child("A", "R", "C2")

    # live: both children's results are visible to the agent
    assert await _agent_result_children(mem, "A") == ["C1", "C2"]

    folded, _ = await w.finalize_root(R, n_assistant=5)

    assert folded == 4  # C1 + C2, dispatch & result each
    # after finalize: only R's own experience remains
    assert await _agent_result_children(mem, "A") == ["R"]


# ── Scenario 3: use_subagent=True child folded from creator's layer ──────────


async def test_subagent_true_child_folded_from_parent_layer() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    R = w.task("R", parent=None, assigned="A", creator="A")
    w.task("D", parent="R", assigned="B", creator="A")  # ran on sub-agent B
    await w.report_child("A", "R", "D")

    assert await _agent_result_children(mem, "A") == ["D"]
    folded, _ = await w.finalize_root(R, n_assistant=5)
    assert folded == 2
    assert await _agent_result_children(mem, "A") == ["R"]


# ── Scenario 4: nested sub-agent recursion ──────────────────────────────────


async def test_nested_subagent_each_layer_keeps_own_root() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    # A's root R delegates D to sub-agent B; D (B's root) delegates E to itself on B
    R = w.task("R", parent=None, assigned="A", creator="A")
    D = w.task("D", parent="R", assigned="B", creator="A")
    w.task("E", parent="D", assigned="B", creator="B")

    # E reported into B's layer; D reported up into A's layer
    await w.report_child("B", "D", "E")
    await w.report_child("A", "R", "D")

    # D (B's root) finalizes: fold E in B's layer, record D self-experience in B's layer
    folded_b, _ = await w.finalize_root(D, n_assistant=5)
    assert folded_b == 2  # E folded
    assert await _agent_result_children(mem, "B") == ["D"]  # B keeps its own root D, not E

    # R (A's root) finalizes: D is A's sub-task → folded from A's layer
    folded_a, _ = await w.finalize_root(R, n_assistant=5)
    assert folded_a == 2  # D folded
    assert await _agent_result_children(mem, "A") == ["R"]
    # B's layer is untouched by A's fold — D's experience still there
    assert await _agent_result_children(mem, "B") == ["D"]


# ── Scenario 5: one agent runs multiple sequential roots ─────────────────────


async def test_sequential_roots_keep_each_others_experience() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    # A is a sub-agent that receives two root tasks R1, R2 from parent P
    R1 = w.task("R1", parent="p1", assigned="A", creator="P")
    R2 = w.task("R2", parent="p2", assigned="A", creator="P")
    w.task("C1", parent="R1", assigned="A", creator="A")
    w.task("C2", parent="R2", assigned="A", creator="A")

    # R1's child, then R1 finalizes
    await w.report_child("A", "R1", "C1")
    folded1, _ = await w.finalize_root(R1, n_assistant=5)
    assert folded1 == 2  # C1 folded
    assert await _agent_result_children(mem, "A") == ["R1"]  # R1 experience kept

    # R2's child, then R2 finalizes — must NOT collateral-fold R1's experience
    await w.report_child("A", "R2", "C2")
    assert await _agent_result_children(mem, "A") == ["C2", "R1"]  # live: C2 + kept R1
    folded2, _ = await w.finalize_root(R2, n_assistant=5)
    assert folded2 == 2  # only C2 folded
    # both roots' experiences survive; neither child remains
    assert await _agent_result_children(mem, "A") == ["R1", "R2"]


# ── Scenario 6: short root preserves conversation (not a dispatch pair) ──────


async def test_short_root_preserves_conversation_and_folds_children() -> None:
    mem = InMemoryMemoryProvider()
    w = World(mem)
    R = w.task("R", parent=None, assigned="A", creator="A")
    w.task("C1", parent="R", assigned="A", creator="A")
    await w.report_child("A", "R", "C1")

    folded, info = await w.finalize_root(R, n_assistant=2)  # ≤3 → preserve conversation

    assert folded == 2
    assert info["mode"] == "conversation"
    # child dispatch result folded; no leftover dispatch results
    assert await _agent_result_children(mem, "A") == []
    # but the preserved conversation survives as agent-layer experience
    turns = await mem.recall_recent(_sc("ignored", "A"), [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    assert len(turns) == 3  # 1 user + 2 assistant
