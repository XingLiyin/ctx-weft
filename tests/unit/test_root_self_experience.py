"""root 自经验胶囊（新形态）：交错时间线镜像 + finish_task 对（spec §3.3）。

新行为（Task 6 重写后）：
- 镜像幸存 task 层 USER_PROMPT/TASK_COMPACT_SUMMARY/LLM_RESPONSE/TOOL_RESULT
  → agent 层 AGENT_CONVERSATION_TURN，保留原始 timestamp。
- TASK_COMPACT_SUMMARY 渲染 role=assistant（继承存储 role=assistant）。
- 末尾追加 finish 对（assistant finish_task tool_call + tool Process Report）。
- user_prompt 来自 task 层幸存记录（不再从 task.user_prompt 字段静态取）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _task_sc(task_id="t1", agent="ag1") -> MemoryScope:
    """task 层 scope（含 task_id）——写入 task 层事件用。"""
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent)


def _agent_sc(agent="ag1") -> MemoryScope:
    """agent 层 scope（task_id=None）——_synthesize_dispatch_pair 写入目标。"""
    return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _task(task_id="t1", agent="ag1", prompt="帮我把这个ppt写成pdf") -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id=agent, creator_agent_id=agent, parent_task_id=None,
                title="PPTX转PDF", description="转换为 PDF", user_prompt=prompt,
                settings=NormalTaskSettings())


async def _agent_turns(mem, agent_scope):
    """返回 agent scope 的 AGENT_CONVERSATION_TURN 记录（chronological）。"""
    recs = await mem.recall_recent(
        agent_scope, [T.AGENT_CONVERSATION_TURN], 2000, _ctx())
    return list(reversed(recs))


async def test_user_turn_from_surviving_task_layer_record():
    """幸存 task 层 USER_PROMPT 被镜像为 user 回合；superseded 的不出现。

    新行为：user_prompt 来自 task 层幸存记录（非 task.user_prompt 字段）。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_sc()
    asc = _agent_sc()
    # 原始 prompt 已被 supersede
    orig = await mem.ingest(_ev(T.USER_PROMPT, tsc, "帮我把这个ppt写成pdf", 0, role="user"), _ctx())
    await mem.supersede([orig], _ctx())
    # 幸存的 UP（HITL 打断语）
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "你为什么不使用技能呢", 5, role="user"), _ctx())
    task = _task(prompt="帮我把这个ppt写成pdf")

    await _synthesize_dispatch_pair(mem, asc, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    users = [r for r in turns if r.role == "user"]
    # 只有幸存的 HITL prompt 被镜像
    assert len(users) == 1
    assert users[0].content == "你为什么不使用技能呢"
    assert users[0].metadata.get("origin_task_id") == "t1"


async def test_assistant_summary_turn_carries_compaction_summary():
    """存活的 task_compact_summary 被镜像成 assistant 回合（继承存储 role=assistant）。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_sc()
    asc = _agent_sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "### 会话目标\n转 PDF\n### 已完成工作\n- 试过 COM", 1, role="assistant"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    summ = [r for r in turns if r.role == "assistant" and "会话目标" in r.content]
    assert len(summ) == 1, f"expected 1 summary assistant turn, got {len(summ)}: {[(r.role, r.content[:40]) for r in turns]}"
    assert summ[0].metadata.get("origin_task_id") == "t1"


async def test_capsule_order_user_summary_finish_pair():
    """胶囊渲染序（新形态）：
    user(UP) → assistant(TASK_COMPACT_SUMMARY) → assistant(finish_task) → tool(Process Report)。
    全部为 AGENT_CONVERSATION_TURN（不再有 TASK_DISPATCH/TASK_DISPATCH_RESULT）。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_sc()
    asc = _agent_sc()
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "帮我转", 0, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "### 会话目标\n转 PDF", 1, role="assistant"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    roles = [r.role for r in turns]
    # 4 turns: user(UP) + assistant(summary) + assistant(finish_task) + tool(Process Report)
    assert roles == ["user", "assistant", "assistant", "tool"], f"got roles={roles}"
    # all AGENT_CONVERSATION_TURN
    assert all(r.type == T.AGENT_CONVERSATION_TURN for r in turns)
    # finish pair at end
    assert turns[-2].metadata.get("tool_calls", [{}])[0].get("name", "").endswith("finish_task")
    assert turns[-1].content.startswith("Process Report:")


async def test_no_compaction_no_summary_turn():
    """没被 compact（无 task_compact_summary）→ 不写 assistant summary 回合，
    只有 user(UP) + assistant(finish_task) + tool。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_sc()
    asc = _agent_sc()
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "帮我转", 0, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    roles = [r.role for r in turns]
    assert roles == ["user", "assistant", "tool"], f"got roles={roles}"
    assert all(r.type == T.AGENT_CONVERSATION_TURN for r in turns)


async def test_all_compact_summaries_mirrored_in_order():
    """多条 task_compact_summary 时全部按时间序镜像（非只取最新）。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_sc()
    asc = _agent_sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "旧摘要", 1, role="assistant"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "新摘要", 9, role="assistant"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    summ = [r for r in turns if r.role == "assistant" and r.metadata.get("tool_calls", None) is None or
            (r.role == "assistant" and not r.metadata.get("tool_calls"))]
    # 过滤掉 finish_task assistant turn（有 tool_calls）
    summary_turns = [r for r in turns
                     if r.role == "assistant" and not any(
                         tc.get("name", "").endswith("finish_task")
                         for tc in r.metadata.get("tool_calls", [])
                     )]
    assert len(summary_turns) == 2, f"expected 2 summary turns, got {summary_turns}"
    assert summary_turns[0].content == "旧摘要"
    assert summary_turns[1].content == "新摘要"


async def test_empty_user_prompt_in_task_scope_means_no_user_mirror():
    """task 层无 USER_PROMPT → 无 user mirror 回合（只 finish pair）。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_sc()
    task = _task(prompt="")

    await _synthesize_dispatch_pair(mem, asc, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, asc)
    users = [r for r in turns if r.role == "user"]
    assert users == []
    # finish pair still written
    assert len(turns) == 2
    assert turns[-2].role == "assistant"
    assert turns[-1].role == "tool"
