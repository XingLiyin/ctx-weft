"""close(task)：短任务保留、长任务残留、子树 GC（spec 2026-06-23）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import finalize_task_memory, _descendant_task_ids
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryScope,
    ProviderContext,
)
from ctx_weft.protocols.template import LoopConfig
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str, agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


class _FakeTM:
    def __init__(self, children: dict[str, set[str]] | None = None) -> None:
        self._children = children or {}

    def children_of(self, task_id: str) -> set[str]:
        return self._children.get(task_id, set())


def _state(task: Task, scope: MemoryScope, loop_config: LoopConfig, tm: _FakeTM):
    # 字段须满足 make_event：run_id / sequence_counter / session.id / session.tenant_id /
    # task.id / agent.id（finalize_task_memory 内部用 make_event 构造事件）。
    agent = SimpleNamespace(id=scope.agent_id, loop_config=loop_config)
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent)


def _loop_ctx(mem, tm: _FakeTM):
    return SimpleNamespace(memory=mem, provider_ctx=_ctx(), task_manager=tm)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


async def _seed_conv(mem, scope, n_assistant: int, big: bool = False) -> None:
    await mem.ingest(_ev(T.USER_PROMPT, scope, "hello", 0, role="user"), _ctx())
    for i in range(n_assistant):
        text = ("x " * 4000) if big else f"reply {i}"
        await mem.ingest(_ev(T.LLM_RESPONSE, scope, text, i + 1, role="assistant"), _ctx())


def _root_task(task_id="t1", agent="ag1") -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id=agent, creator_agent_id=agent, parent_task_id=None,
                title="Root", user_prompt="hello", settings=NormalTaskSettings())


async def test_short_root_leaf_keeps_full_conversation() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=2)  # few turns, small → short
    task = _root_task()

    await finalize_task_memory(mem, _state(task, scope, LoopConfig(), _FakeTM()),
                               task, "final out", "success", _loop_ctx(mem, _FakeTM()))

    # conversation NOT superseded; no synthesized residue written
    convs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE], 100, _ctx())
    assert {c.content for c in convs} >= {"hello", "reply 0", "reply 1"}
    residues = await mem.recall_recent(scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert residues == []


async def test_long_root_leaf_closes_into_residue() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)  # >turn_cap and big → not short
    task = _root_task()
    cfg = LoopConfig()

    await finalize_task_memory(mem, _state(task, scope, cfg, _FakeTM()),
                               task, "final out", "success", _loop_ctx(mem, _FakeTM()))

    # own conversation superseded; a synthesized capsule (AGENT_CONVERSATION_TURN finish pair) exists
    convs = await mem.recall_recent(scope, [T.USER_PROMPT, T.LLM_RESPONSE], 100, _ctx())
    assert convs == []
    # new shape: finish pair written as AGENT_CONVERSATION_TURN (tool role holds Process Report)
    caps = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    finish_tools = [r for r in caps if r.role == "tool" and r.metadata.get("origin_task_id") == "t1"]
    assert finish_tools, "expected finish-pair tool turn in agent capsule"


async def test_root_close_gcs_subtree_residues() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)
    # a same-agent sub-task t2 residue already in the agent scope
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 10, role="assistant",
                         tool_call_id="d2", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "t2 out", 11, role="tool",
                         tool_call_id="d2", child_task_id="t2", parent_task_id="t1"), _ctx())
    # t2 also has its own task-layer conversation tagged agent ag1
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("t2"), "t2 work", 12, role="assistant"), _ctx())

    tm = _FakeTM({"t1": {"t2"}})
    task = _root_task()  # non-leaf via tm
    await finalize_task_memory(mem, _state(task, scope, LoopConfig(), tm),
                               task, "final out", "success", _loop_ctx(mem, tm))

    # t2's residue + t2's conversation are GC'd; only the root's own residue remains
    all_results = await mem.recall_recent(scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert all(r.content != "t2 out" for r in all_results)
    t2_conv = await mem.recall_recent_by_agent(_sc("x", "ag1"), [T.LLM_RESPONSE], 100, _ctx())
    assert all(r.metadata.get("task_id") != "t2" for r in t2_conv)


async def test_cross_agent_child_bubbles_unconditionally_even_when_short() -> None:
    mem = InMemoryMemoryProvider()
    child_scope = _sc("c1", agent_id="ag2")
    await _seed_conv(mem, child_scope, n_assistant=1)  # short leaf
    # parent dispatch marker lives in ag1's agent layer
    await mem.ingest(_ev(T.TASK_DISPATCH, _sc("p1", "ag1"), "", 0, role="assistant",
                         tool_call_id="oc1", tool_name="delegate_task", arguments={}), _ctx())
    child = Task(id="c1", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag2", creator_agent_id="ag1", parent_task_id="p1",
                 origin_tool_call_id="oc1", title="Child", user_prompt="sub",
                 settings=NormalTaskSettings())

    await finalize_task_memory(mem, _state(child, child_scope, LoopConfig(), _FakeTM()),
                               child, "child out", "success", _loop_ctx(mem, _FakeTM()))

    # parent (ag1) received the bubble even though the child was short
    parent_scope = _sc("p1", "ag1")
    res = await mem.recall_recent(parent_scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert any(r.metadata.get("tool_call_id") == "oc1" and r.content == "child out" for r in res)
    # child's own conversation preserved (short, own scope) for potential reentry
    own = await mem.recall_recent(child_scope, [T.LLM_RESPONSE], 100, _ctx())
    assert own != []


async def test_same_agent_short_leaf_no_bubble_no_close() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t2", "ag1")
    await _seed_conv(mem, scope, n_assistant=1)  # short leaf
    # dispatch marker for t2 in the shared ag1 scope
    await mem.ingest(_ev(T.TASK_DISPATCH, _sc("t1", "ag1"), "", 0, role="assistant",
                         tool_call_id="oc2", tool_name="delegate_task", arguments={}), _ctx())
    child = Task(id="t2", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="t1",
                 origin_tool_call_id="oc2", title="Sub", user_prompt="sub",
                 settings=NormalTaskSettings())

    await finalize_task_memory(mem, _state(child, scope, LoopConfig(), _FakeTM()),
                               child, "t2 out", "success", _loop_ctx(mem, _FakeTM()))

    # no residue written (same-agent short = keep open), conversation preserved
    res = await mem.recall_recent(_sc("t1", "ag1"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert all(r.metadata.get("tool_call_id") != "oc2" for r in res)
    own = await mem.recall_recent(scope, [T.LLM_RESPONSE], 100, _ctx())
    assert own != []


async def test_same_agent_nonshort_child_bubbles_without_self_residue() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t2", "ag1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)  # over turn cap → not short
    # dispatch marker for t2 lives in the shared ag1 agent scope (parent t1)
    await mem.ingest(_ev(T.TASK_DISPATCH, _sc("t1", "ag1"), "", 0, role="assistant",
                         tool_call_id="oc2", tool_name="delegate_task", arguments={}), _ctx())
    child = Task(id="t2", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="t1",
                 origin_tool_call_id="oc2", title="Sub", user_prompt="sub",
                 settings=NormalTaskSettings())
    await finalize_task_memory(mem, _state(child, scope, LoopConfig(), _FakeTM()),
                               child, "t2 out", "success", _loop_ctx(mem, _FakeTM()))

    # bubble residue written, paired with oc2, marked as a SUB-task residue (parent_task_id="t1")
    res = await mem.recall_recent(_sc("t1", "ag1"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    bubble = [r for r in res if r.metadata.get("tool_call_id") == "oc2"]
    assert bubble and bubble[0].metadata.get("parent_task_id") == "t1"
    # own conversation superseded (not short → closed)
    own = await mem.recall_recent(scope, [T.LLM_RESPONSE], 100, _ctx())
    assert own == []
    # NO self-residue: agent scope must contain no residue with parent_task_id is None
    agent_res = await mem.recall_recent(_sc("t2", "ag1"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert all(r.metadata.get("parent_task_id") is not None for r in agent_res)


async def test_root_close_gcs_deep_nested_subtree() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)
    # A residue (child A, parent t1) + A1 residue (grandchild, child A1, parent A) + A1 leftover conv
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 10, role="assistant",
                         tool_call_id="dA", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "A out", 11, role="tool",
                         tool_call_id="dA", child_task_id="A", parent_task_id="t1"), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 12, role="assistant",
                         tool_call_id="dA1", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "A1 out", 13, role="tool",
                         tool_call_id="dA1", child_task_id="A1", parent_task_id="A"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("A1", "ag1"), "A1 work", 14, role="assistant"), _ctx())
    tm = _FakeTM({"t1": {"A"}, "A": {"A1"}})
    task = _root_task()
    await finalize_task_memory(mem, _state(task, scope, LoopConfig(), tm),
                               task, "final out", "success", _loop_ctx(mem, tm))
    results = await mem.recall_recent(scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    contents = {r.content for r in results}
    assert "A out" not in contents and "A1 out" not in contents   # whole subtree (incl grandchild) GC'd
    # root self-residue now written as AGENT_CONVERSATION_TURN (finish pair), not TASK_DISPATCH_RESULT
    caps = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    assert any(r.metadata.get("origin_task_id") == "t1" for r in caps)  # root capsule survives
    convs = await mem.recall_recent_by_agent(_sc("x", "ag1"), [T.LLM_RESPONSE], 100, _ctx())
    assert all(r.metadata.get("task_id") != "A1" for r in convs)   # grandchild conversation gone


async def test_intermediate_close_collapses_grandchild() -> None:
    mem = InMemoryMemoryProvider()
    a_scope = _sc("A", "ag1")
    await _seed_conv(mem, a_scope, n_assistant=5, big=True)  # A's own conversation (not short)
    # parent t1's dispatch marker for A (so A's bubble pairs)
    await mem.ingest(_ev(T.TASK_DISPATCH, _sc("t1", "ag1"), "", 0, role="assistant",
                         tool_call_id="ocA", tool_name="delegate_task", arguments={}), _ctx())
    # A1 residue (child A1, parent A) + A1 conv
    await mem.ingest(_ev(T.TASK_DISPATCH, a_scope, "", 5, role="assistant",
                         tool_call_id="dA1", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, a_scope, "A1 out", 6, role="tool",
                         tool_call_id="dA1", child_task_id="A1", parent_task_id="A"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("A1", "ag1"), "A1 work", 7, role="assistant"), _ctx())
    A = Task(id="A", session_id="s1", status="FINISHED", tenant_id="default",
             assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="t1",
             origin_tool_call_id="ocA", title="A", user_prompt="a", settings=NormalTaskSettings())
    tm = _FakeTM({"A": {"A1"}})
    await finalize_task_memory(mem, _state(A, a_scope, LoopConfig(), tm),
                               A, "A out", "success", _loop_ctx(mem, tm))
    results = await mem.recall_recent(_sc("any", "ag1"), [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert all(r.content != "A1 out" for r in results)            # grandchild residue collapsed at A's close
    convs = await mem.recall_recent_by_agent(_sc("x", "ag1"), [T.LLM_RESPONSE], 100, _ctx())
    assert all(r.metadata.get("task_id") != "A1" for r in convs)  # grandchild conv gone
    assert any(r.metadata.get("tool_call_id") == "ocA" and r.metadata.get("parent_task_id") == "t1"
               for r in results)                                  # A bubbles its own residue to t1


async def test_root_close_gcs_multichild_plan() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)
    for c in ("A", "B", "C"):
        await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 10, role="assistant",
                             tool_call_id=f"d{c}", tool_name="delegate_task", arguments={}), _ctx())
        await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, f"{c} out", 11, role="tool",
                             tool_call_id=f"d{c}", child_task_id=c, parent_task_id="t1"), _ctx())
    tm = _FakeTM({"t1": {"A", "B", "C"}})
    task = _root_task()
    await finalize_task_memory(mem, _state(task, scope, LoopConfig(), tm),
                               task, "final out", "success", _loop_ctx(mem, tm))
    results = await mem.recall_recent(scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    contents = {r.content for r in results}
    assert not ({"A out", "B out", "C out"} & contents)          # all plan children GC'd
    # root self-residue now written as AGENT_CONVERSATION_TURN finish pair
    caps = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    assert any(r.metadata.get("origin_task_id") == "t1" for r in caps)  # root capsule remains


async def test_root_close_gcs_mixed_same_and_cross_agent_children() -> None:
    mem = InMemoryMemoryProvider()
    scope = _sc("t1")
    await _seed_conv(mem, scope, n_assistant=5, big=True)
    # same-agent child S: residue in ag1 + leftover conv (task_id=S, agent ag1)
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 10, role="assistant",
                         tool_call_id="dS", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "S out", 11, role="tool",
                         tool_call_id="dS", child_task_id="S", parent_task_id="t1"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("S", "ag1"), "S work", 12, role="assistant"), _ctx())
    # cross-agent child X: only a bubbled residue in ag1 (X ran in ag2, black box — no conv in ag1)
    await mem.ingest(_ev(T.TASK_DISPATCH, scope, "", 13, role="assistant",
                         tool_call_id="dX", tool_name="delegate_task", arguments={}), _ctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, scope, "X out", 14, role="tool",
                         tool_call_id="dX", child_task_id="X", parent_task_id="t1"), _ctx())
    tm = _FakeTM({"t1": {"S", "X"}})
    task = _root_task()
    await finalize_task_memory(mem, _state(task, scope, LoopConfig(), tm),
                               task, "final out", "success", _loop_ctx(mem, tm))
    results = await mem.recall_recent(scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    contents = {r.content for r in results}
    assert not ({"S out", "X out"} & contents)                   # both children residues GC'd
    convs = await mem.recall_recent_by_agent(_sc("x", "ag1"), [T.LLM_RESPONSE], 100, _ctx())
    assert all(r.metadata.get("task_id") != "S" for r in convs)  # same-agent child conv GC'd


async def test_descendant_task_ids_multilevel_bfs() -> None:
    tm = _FakeTM({"t1": {"A", "B"}, "A": {"A1"}, "B": {"B1"}, "A1": {"A1a"}})
    assert _descendant_task_ids("t1", tm) == {"A", "B", "A1", "B1", "A1a"}
    assert _descendant_task_ids("A", tm) == {"A1", "A1a"}
    assert _descendant_task_ids("leaf", tm) == set()
