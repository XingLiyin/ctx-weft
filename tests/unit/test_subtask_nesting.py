"""Task 9: sub-task bubble split + same-agent nested capsule.

同 agent child close:
  - parent scope TASK_DISPATCH_RESULT content == "Sub-task '<title>' scheduled."
  - parent scope 含 child 的 AGENT_CONVERSATION_TURN (origin_task_id=child.id)
    排在 dispatch pair 之后

跨 agent child close:
  - parent scope TASK_DISPATCH_RESULT content == mem_content (含 "Process Report:")
  - parent scope **无** origin_task_id=child.id 的 AGENT_CONVERSATION_TURN
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


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _sc(task_id: str, agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent_id)


class _FakeTM:
    def children_of(self, task_id: str) -> set[str]:
        return set()


def _state(task: Task, scope: MemoryScope, loop_config: LoopConfig):
    agent = SimpleNamespace(id=scope.agent_id, loop_config=loop_config)
    session = SimpleNamespace(id="s1", tenant_id="default")
    return SimpleNamespace(run_id="run1", sequence_counter=0, session=session,
                           scope=scope, task=task, agent=agent)


def _loop_ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_ctx(), task_manager=_FakeTM())


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


async def _seed_conv_nonshort(mem, scope) -> None:
    """Seed enough turns to be non-short (> turn_cap or too many tokens)."""
    await mem.ingest(_ev(T.USER_PROMPT, scope, "hello child", 1, role="user"), _ctx())
    for i in range(5):
        big_text = "x " * 4000
        await mem.ingest(_ev(T.LLM_RESPONSE, scope, big_text, i + 2, role="assistant"), _ctx())


async def test_same_agent_child_keeps_delegate_and_writes_ack() -> None:
    """§2.5：同 agent child close 保留 delegate 回合、配对写入静态 ack（back-dated to delegate timestamp）。"""
    mem = InMemoryMemoryProvider()
    child_scope = _sc("c1", "ag1")
    await _seed_conv_nonshort(mem, child_scope)
    # gateway-written delegate turn for c1 in parent scope (新表示：AGENT_CONVERSATION_TURN, §2.3)
    await mem.ingest(_ev(T.AGENT_CONVERSATION_TURN, _sc("p1", "ag1"), "", 0, role="assistant",
                         origin_task_id="p1", parent_task_id=None,
                         tool_calls=[{"id": "oc1", "name": "delegate_task", "input": {}}]), _ctx())

    child = Task(id="c1", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="p1",
                 origin_tool_call_id="oc1", title="My Sub Task", user_prompt="do sub",
                 settings=NormalTaskSettings())

    mem_content = "sub outputs\n\nProcess Report: sub summary"
    await finalize_task_memory(
        mem, _state(child, child_scope, LoopConfig()),
        child, mem_content, "success", _loop_ctx(mem),
        act_recap="sub summary", task_summary="",
    )

    parent_scope = _sc("p1", "ag1")
    turns = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    from ctx_weft.core.loop.steps.finalize import _DISPATCH_ACK
    # §2.5: delegate turn KEPT (not superseded)
    delegate = [r for r in turns if r.role == "assistant"
                and any(tc.get("id") == "oc1" for tc in (r.metadata.get("tool_calls") or []))]
    assert delegate, "§2.5: delegate turn must be KEPT (not superseded)"
    # §2.5: static ack written, paired with oc1, content=_DISPATCH_ACK, timestamp=delegate ts
    ack = [r for r in turns if r.role == "tool" and r.metadata.get("tool_call_id") == "oc1"]
    assert ack and ack[0].content == _DISPATCH_ACK, (
        f"§2.5: static ack must be written with content={_DISPATCH_ACK!r}; got {[r.content for r in ack]}"
    )
    assert ack[0].timestamp == delegate[0].timestamp, (
        f"§2.5: ack timestamp must equal delegate turn timestamp; "
        f"ack={ack[0].timestamp}, delegate={delegate[0].timestamp}"
    )


async def test_same_agent_child_finish_pair_written_into_parent_scope() -> None:
    """task-resident：同 agent child 的 finish 对（AGENT_CONVERSATION_TURN, origin_task_id=child.id）
    写入 parent agent scope；**不镜像 body**（无 user 锚点），child raw body 留 child task 层。"""
    mem = InMemoryMemoryProvider()
    child_scope = _sc("c1", "ag1")
    await _seed_conv_nonshort(mem, child_scope)

    child = Task(id="c1", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="p1",
                 origin_tool_call_id="oc1", title="Child Task", user_prompt="do sub",
                 settings=NormalTaskSettings())

    mem_content = "sub outputs\n\nProcess Report: sub summary"
    await finalize_task_memory(
        mem, _state(child, child_scope, LoopConfig()),
        child, mem_content, "success", _loop_ctx(mem),
        act_recap="sub summary", task_summary="",
    )

    parent_scope = _sc("p1", "ag1")

    # child finish pair (AGENT_CONVERSATION_TURN, origin_task_id=child.id) must exist in parent scope
    caps = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    child_turns = [r for r in caps if r.metadata.get("origin_task_id") == "c1"]
    assert len(child_turns) == 2, (
        f"expected child finish pair (2 turns) in parent scope; got {[(r.role, r.content[:30]) for r in child_turns]}"
    )

    # task-resident: NO body mirror (no user anchor)
    assert not any(r.role == "user" for r in child_turns), (
        "task-resident: child finish pair must NOT mirror body (no user anchor)"
    )

    # must include finish pair: tool role (content = act_recap fallback since task_summary="")
    finish_tool = [r for r in child_turns if r.role == "tool"]
    assert finish_tool, "expected finish-pair tool turn in parent scope capsule"
    assert finish_tool[0].content, (
        f"finish tool content must be non-empty, got: {finish_tool[0].content!r}"
    )

    # child raw body stays in child task layer (not mirrored/superseded)
    child_body = await mem.recall_recent(child_scope, [T.USER_PROMPT, T.LLM_RESPONSE], 100, _ctx())
    assert any(r.role == "user" and "hello child" in r.content for r in child_body), (
        "child raw body (user anchor) must stay in child task layer"
    )


async def test_cross_agent_child_bubble_is_conversation_turn() -> None:
    """§2.3：跨 agent child 的 dispatch result 写成 AGENT_CONVERSATION_TURN（tool 回合），
    与 gateway 写的 delegate assistant 回合靠 tool_call_id 配对；origin_task_id=delegating
    task（与同单元 finish 对同 origin、同命运一起折）；不再写 TASK_DISPATCH_RESULT enum。"""
    mem = InMemoryMemoryProvider()
    child_scope = _sc("c2", "ag2")
    # cross-agent child: short is fine since cross_agent always bubbles
    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "hello", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "reply", 2, role="assistant"), _ctx())

    child = Task(id="c2", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag2", creator_agent_id="ag1",  # cross-agent
                 parent_task_id="p1", origin_tool_call_id="oc2",
                 title="Cross Agent Child", user_prompt="do cross",
                 settings=NormalTaskSettings())

    mem_content = "cross outputs\n\nProcess Report: cross summary"
    await finalize_task_memory(
        mem, _state(child, child_scope, LoopConfig()),
        child, mem_content, "success", _loop_ctx(mem),
        act_recap="cross summary", task_summary="",
    )

    parent_scope = _sc("p1", "ag1")
    # dispatch result == 普通 conversation turn（tool），配对 oc2、归 delegating task(p1) 单元
    turns = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    result = [r for r in turns
              if r.role == "tool" and r.metadata.get("tool_call_id") == "oc2"]
    assert result, "expected dispatch result as AGENT_CONVERSATION_TURN (tool) paired with oc2"
    assert result[0].metadata.get("origin_task_id") == "p1", (
        "dispatch result 须归 delegating task 单元（与 finish 对同 origin、同命运）"
    )
    assert result[0].content == mem_content, (
        f"expected full mem_content (black box), got: {result[0].content!r}"
    )
    # 不再写 legacy TASK_DISPATCH_RESULT enum
    legacy = await mem.recall_recent(parent_scope, [T.TASK_DISPATCH_RESULT], 100, _ctx())
    assert legacy == [], "cross-agent dispatch result must not write TASK_DISPATCH_RESULT enum"


async def test_same_agent_keeps_delegate_and_writes_backdated_ack() -> None:
    """§2.5：同 agent close：delegate 回合保留（不 supersede）+ 配对静态 ack（timestamp = delegate 时刻）。"""
    from ctx_weft.core.loop.steps.finalize import _close_one, _DISPATCH_ACK

    mem = InMemoryMemoryProvider()
    child_scope = _sc("c1", "ag1")
    parent_scope = _sc("p1", "ag1")
    await _seed_conv_nonshort(mem, child_scope)

    delegate_ts = _BASE + timedelta(seconds=0)
    # seed delegate assistant turn in parent scope
    await mem.ingest(MemoryEvent(
        type=T.AGENT_CONVERSATION_TURN, scope=parent_scope,
        content="", timestamp=delegate_ts, role="assistant",
        metadata={"origin_task_id": "p1", "parent_task_id": None,
                  "tool_calls": [{"id": "oc1", "name": "delegate_task", "input": {}}]},
    ), _ctx())

    child = Task(id="c1", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag1", creator_agent_id="ag1", parent_task_id="p1",
                 origin_tool_call_id="oc1", title="My Sub Task", user_prompt="do sub",
                 settings=NormalTaskSettings())
    state = _state(child, child_scope, LoopConfig())

    await _close_one(mem, state, child, "out\n\nProcess Report: r", "success", _loop_ctx(mem),
                     short=True, act_recap="本段做了 X", task_summary="整段总结")

    turns = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())

    # delegate 回合仍在（未被 supersede）
    delegate = [r for r in turns if r.role == "assistant"
                and any(tc.get("id") == child.origin_tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    assert delegate, "delegate 回合不应被 supersede"

    # 配对静态 result：content=_DISPATCH_ACK、tool_call_id 配对、timestamp == delegate 时刻
    ack = [r for r in turns if r.role == "tool" and r.metadata.get("tool_call_id") == child.origin_tool_call_id]
    assert ack and ack[0].content == _DISPATCH_ACK, f"expected static ack with content={_DISPATCH_ACK!r}, got {[r.content for r in ack]}"
    assert ack[0].timestamp == delegate[0].timestamp, f"ack.timestamp={ack[0].timestamp} must equal delegate.timestamp={delegate[0].timestamp}"

    # stray-ack guard：no OTHER tool record carries ack content
    assert _DISPATCH_ACK not in {
        r.content for r in turns
        if r.metadata.get("tool_call_id") != child.origin_tool_call_id
    }, "ack content must only appear in the paired tool_call_id record"


async def test_cross_agent_result_carries_outputs_and_task_summary() -> None:
    """Task 5: 跨 agent dispatch result（cross_agent bubble, mem_content）须含 outputs + task_summary，
    不掺 act_recap（mem_content 的 report 部分改用 task_summary）。"""
    from ctx_weft.core.loop.steps.finalize import FinalizeStep
    from ctx_weft.core.loop.steps.observe import Verdict

    mem = InMemoryMemoryProvider()
    child_scope = _sc("c99", "ag2")

    # seed minimal conv so FinalizeStep can run
    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "do cross", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "done", 2, role="assistant"), _ctx())

    child = Task(id="c99", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag2", creator_agent_id="ag1",  # cross-agent
                 parent_task_id="p99", origin_tool_call_id="oc99",
                 title="Cross Agent Child", user_prompt="do cross",
                 settings=NormalTaskSettings())
    child.outputs = "最终产出给 user"

    verdict = Verdict(task_outcome="success", act_recap="本段", task_summary="综合 process report")

    agent = SimpleNamespace(id="ag2", loop_config=LoopConfig())
    session = SimpleNamespace(id="s1", tenant_id="default")
    state = SimpleNamespace(
        run_id="run99", sequence_counter=0, session=session,
        scope=child_scope, task=child, agent=agent, verdict=verdict,
    )

    await FinalizeStep().execute(state, _loop_ctx(mem))

    parent_scope = _sc("p99", "ag1")
    turns = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    result = [r for r in turns if r.role == "tool"
              and r.metadata.get("tool_call_id") == "oc99"]
    assert result
    body = result[0].content
    assert "最终产出给 user" in body          # 最终输出
    assert "综合 process report" in body       # task_summary 承载 process report
    assert "本段" not in body                   # 不掺 act_recap


async def test_cross_agent_child_no_nested_capsule_in_parent_scope() -> None:
    """跨 agent child: parent scope 无 origin_task_id=child.id 的 AGENT_CONVERSATION_TURN。"""
    mem = InMemoryMemoryProvider()
    child_scope = _sc("c2", "ag2")
    await mem.ingest(_ev(T.USER_PROMPT, child_scope, "hello", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, child_scope, "reply", 2, role="assistant"), _ctx())

    child = Task(id="c2", session_id="s1", status="FINISHED", tenant_id="default",
                 assigned_agent_id="ag2", creator_agent_id="ag1",
                 parent_task_id="p1", origin_tool_call_id="oc2",
                 title="Cross Agent Child", user_prompt="do cross",
                 settings=NormalTaskSettings())

    mem_content = "cross outputs\n\nProcess Report: cross summary"
    await finalize_task_memory(
        mem, _state(child, child_scope, LoopConfig()),
        child, mem_content, "success", _loop_ctx(mem),
        act_recap="cross summary", task_summary="",
    )

    parent_scope = _sc("p1", "ag1")
    caps = await mem.recall_recent(parent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    child_turns = [r for r in caps if r.metadata.get("origin_task_id") == "c2"]
    assert not child_turns, (
        f"cross-agent child should NOT write capsule into parent scope, found: {child_turns}"
    )
