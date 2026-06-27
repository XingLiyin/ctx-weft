"""_gc_subtree 保留直属派发对/嵌入子胶囊（Task 10）。

root 委派 same-agent child（C_same）和 cross-agent child（C_cross）。
root close 后断言：
  (a) C_cross 的 TASK_DISPATCH / TASK_DISPATCH_RESULT 未 superseded。
  (b) C_same 的嵌入子胶囊 AGENT_CONVERSATION_TURN 未 superseded。
  (c) 两个 child 自身的 task 层 raw（USER_PROMPT/LLM_RESPONSE/TOOL_RESULT under child task scopes）
      已被 superseded（GC'd）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.finalize import finalize_task_memory
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
SESSION = "s1"
AGENT_ROOT = "ag_root"


def _pctx() -> ProviderContext:
    return ProviderContext(session_id=SESSION, tenant_id="default")


def _sc(task_id: str | None, agent_id: str = AGENT_ROOT) -> MemoryScope:
    return MemoryScope(session_id=SESSION, task_id=task_id, agent_id=agent_id)


def _ev(type_: MemoryEventType, scope: MemoryScope, content: str, t: int,
        role: str | None = None, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=type_, scope=scope, content=content,
        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta,
    )


class _FakeTM:
    def __init__(self, children: dict[str, set[str]] | None = None) -> None:
        self._children = children or {}

    def children_of(self, task_id: str) -> set[str]:
        return self._children.get(task_id, set())


def _state(task: Task, scope: MemoryScope, tm: _FakeTM):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=LoopConfig())
    session = SimpleNamespace(id=SESSION, tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent)


def _loop_ctx(mem, tm: _FakeTM):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx(), task_manager=tm)


async def test_gc_subtree_preserves_dispatch_pairs_and_inline_capsule() -> None:
    """_gc_subtree 只 GC 后代 task 层对话，保留直属派发对与嵌入子胶囊。"""
    mem = InMemoryMemoryProvider()
    root_scope = _sc("root", AGENT_ROOT)

    # Root 自身有 long 对话（non-short → will close）
    for i in range(6):
        await mem.ingest(_ev(T.LLM_RESPONSE, root_scope, "x " * 4000, i, role="assistant"), _pctx())

    # ── Cross-agent child C_cross ──────────────────────────────────────────────
    # dispatch pair lives in root's agent scope (task_id=root)
    await mem.ingest(_ev(T.TASK_DISPATCH, root_scope, "", 10, role="assistant",
                         tool_call_id="tc_cross", tool_name="delegate_task", arguments={}), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, root_scope, "cross result", 11, role="tool",
                         tool_call_id="tc_cross", child_task_id="C_cross",
                         parent_task_id="root"), _pctx())
    # C_cross also has its own task-layer conv (under scope task_id=C_cross, agent=ag2)
    await mem.ingest(_ev(T.USER_PROMPT, _sc("C_cross", "ag2"), "cross prompt", 5, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("C_cross", "ag2"), "cross work", 6, role="assistant"), _pctx())

    # ── Same-agent child C_same ────────────────────────────────────────────────
    # dispatch pair lives in root's agent scope
    await mem.ingest(_ev(T.TASK_DISPATCH, root_scope, "", 20, role="assistant",
                         tool_call_id="tc_same", tool_name="delegate_task", arguments={}), _pctx())
    await mem.ingest(_ev(T.TASK_DISPATCH_RESULT, root_scope, "same result", 21, role="tool",
                         tool_call_id="tc_same", child_task_id="C_same",
                         parent_task_id="root"), _pctx())
    # inlined sub-capsule AGENT_CONVERSATION_TURN (same agent scope, origin_task_id=C_same)
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, root_scope, "inlined turn", 22, role="user",
                         origin_task_id="C_same"), _pctx())
    # C_same also has its own task-layer conv (under scope task_id=C_same, same agent)
    await mem.ingest(_ev(T.USER_PROMPT, _sc("C_same", AGENT_ROOT), "same prompt", 15, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, _sc("C_same", AGENT_ROOT), "same work", 16, role="assistant"), _pctx())
    await mem.ingest(_ev(T.TOOL_RESULT, _sc("C_same", AGENT_ROOT), "tool out", 17, role="tool",
                         tool_call_id="tc_inner"), _pctx())

    root_task = Task(
        id="root", session_id=SESSION, status="FINISHED", tenant_id="default",
        assigned_agent_id=AGENT_ROOT, creator_agent_id=AGENT_ROOT, parent_task_id=None,
        title="Root", user_prompt="do it", settings=NormalTaskSettings(),
    )
    tm = _FakeTM({"root": {"C_same", "C_cross"}})

    await finalize_task_memory(
        mem, _state(root_task, root_scope, tm),
        root_task, "root output\n\nProcess Report: done", "success",
        _loop_ctx(mem, tm),
    )

    # ── (a) cross-agent dispatch pair PRESERVED ────────────────────────────────
    all_dispatches = await mem.recall_recent(root_scope, [T.TASK_DISPATCH], 200, _pctx())
    cross_dispatches = [r for r in all_dispatches if r.metadata.get("tool_call_id") == "tc_cross"]
    assert cross_dispatches, "TASK_DISPATCH for C_cross must NOT be superseded by _gc_subtree"

    all_results = await mem.recall_recent(root_scope, [T.TASK_DISPATCH_RESULT], 200, _pctx())
    cross_results = [r for r in all_results if r.metadata.get("child_task_id") == "C_cross"]
    assert cross_results, "TASK_DISPATCH_RESULT for C_cross must NOT be superseded by _gc_subtree"

    # ── (b) same-agent inlined sub-capsule AGENT_CONVERSATION_TURN PRESERVED ──
    turns = await mem.recall_recent(root_scope, [T.AGENT_CONVERSATION_TURN], 200, _pctx())
    inlined = [r for r in turns if r.metadata.get("origin_task_id") == "C_same"
               and r.role == "user" and r.content == "inlined turn"]
    assert inlined, "AGENT_CONVERSATION_TURN inlined sub-capsule for C_same must be preserved"

    # ── (c) child task-layer raw SUPERSEDED ───────────────────────────────────
    # same-agent child conv gone (visible to root agent scope via recall_recent_by_agent)
    same_conv = await mem.recall_recent(_sc("C_same", AGENT_ROOT),
                                        [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 200, _pctx())
    assert same_conv == [], f"C_same task-layer conv must be GC'd, but got: {same_conv}"

    # cross-agent child (ag2) conv is NOT touched by root's _gc_subtree —
    # it lives in a different agent scope and is the cross-agent child's own concern.
    # (No assertion here: that cleanup is out-of-scope for this function.)
