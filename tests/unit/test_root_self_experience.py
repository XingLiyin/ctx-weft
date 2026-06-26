"""root 自经验胶囊：换源（task.user_prompt）+ 承载 compaction summary（§2.1）。"""
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


def _sc(task_id="t1", agent="ag1") -> MemoryScope:
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(type=type_, scope=scope, content=content,
                       timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def _task(task_id="t1", agent="ag1", prompt="帮我把这个ppt写成pdf") -> Task:
    return Task(id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
                assigned_agent_id=agent, creator_agent_id=agent, parent_task_id=None,
                title="PPTX转PDF", description="转换为 PDF", user_prompt=prompt,
                settings=NormalTaskSettings())


async def _agent_turns(mem, scope):
    """返回 agent scope 的 AGENT_CONVERSATION_TURN 记录（chronological）。"""
    recs = await mem.recall_recent(
        scope, [T.AGENT_CONVERSATION_TURN, T.TASK_DISPATCH, T.TASK_DISPATCH_RESULT], 2000, _ctx())
    return list(reversed(recs))


async def test_user_turn_uses_task_prompt_not_superseded_recall():
    """根因 A：原始 prompt 被 compaction superseded 后，仍从 task.user_prompt 取，而非打断语。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    # 原始 prompt 已被 supersede；剩下未 superseded 的最旧 USER_PROMPT 是打断语
    orig = await mem.ingest(_ev(T.USER_PROMPT, scope, "帮我把这个ppt写成pdf", 0, role="user"), _ctx())
    await mem.supersede([orig], _ctx())
    await mem.ingest(_ev(T.USER_PROMPT, scope, "你为什么不使用技能呢", 5, role="user"), _ctx())
    task = _task(prompt="帮我把这个ppt写成pdf")

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    users = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "user"]
    assert len(users) == 1
    assert users[0].content == "帮我把这个ppt写成pdf"          # 不是「你为什么不使用技能呢」
    assert users[0].metadata.get("origin_task_id") == "t1"


async def test_assistant_summary_turn_carries_compaction_summary():
    """根因 B：存活的 task_compact_summary 被镜像成 assistant 回合。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "### 会话目标\n转 PDF\n### 已完成工作\n- 试过 COM", 1, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    summ = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "assistant"]
    assert len(summ) == 1
    assert "会话目标" in summ[0].content
    assert summ[0].metadata.get("origin_task_id") == "t1"


async def test_capsule_order_user_summary_dispatch_result():
    """胶囊渲染序：user → assistant(summary) → assistant(dispatch) → tool(result)，靠 seq_no。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "### 会话目标\n转 PDF", 1, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)  # chronological（同 ts → seq_no 序）
    kinds = [(r.type, r.role) for r in turns]
    assert kinds == [
        (T.AGENT_CONVERSATION_TURN, "user"),
        (T.AGENT_CONVERSATION_TURN, "assistant"),
        (T.TASK_DISPATCH, "assistant"),
        (T.TASK_DISPATCH_RESULT, "tool"),
    ]
    # 同一时间戳
    assert len({r.timestamp for r in turns}) == 1


async def test_no_compaction_no_summary_turn():
    """没被 compact（无 task_compact_summary）→ 不写 assistant summary 回合。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "## PDF 已完成", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    kinds = [(r.type, r.role) for r in turns]
    assert kinds == [
        (T.AGENT_CONVERSATION_TURN, "user"),
        (T.TASK_DISPATCH, "assistant"),
        (T.TASK_DISPATCH_RESULT, "tool"),
    ]


async def test_latest_summary_wins_when_multiple():
    """多条 task_compact_summary 时取最新一条。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "旧摘要", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, scope, "新摘要", 9, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, scope, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    summ = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "assistant"]
    assert summ[0].content == "新摘要"


async def test_empty_user_prompt_skips_user_turn():
    """task.user_prompt 为空 → 跳过 user 回合（防御）。"""
    mem = InMemoryMemoryProvider()
    scope = _sc()
    task = _task(prompt="")

    await _synthesize_dispatch_pair(mem, scope, task, "out", "success", _ctx())

    turns = await _agent_turns(mem, scope)
    users = [r for r in turns if r.type == T.AGENT_CONVERSATION_TURN and r.role == "user"]
    assert users == []
