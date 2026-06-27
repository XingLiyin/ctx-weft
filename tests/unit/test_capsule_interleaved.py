"""Task 6 TDD：_synthesize_dispatch_pair 交错时间线胶囊。

形态：镜像幸存 task 层对话（USER_PROMPT/TASK_COMPACT_SUMMARY/LLM_RESPONSE/TOOL_RESULT）
→ agent 层 AGENT_CONVERSATION_TURN（保留原始 timestamp + role + tool 元数据），
末尾追加合成 finish 对（assistant finish_task tool_call + tool Process Report）。

spec §3.3 step1-3，角色映射约定（任务指令修正版）：
  USER_PROMPT         → role="user"
  TASK_COMPACT_SUMMARY→ role="assistant"  (覆盖 DB 存的 role="user")
  LLM_RESPONSE        → role="assistant"
  TOOL_RESULT         → role="tool"
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


def _task_scope(task_id="t1", agent="ag1") -> MemoryScope:
    """task 层 scope（含 task_id）。"""
    return MemoryScope(session_id="s1", task_id=task_id, agent_id=agent)


def _agent_scope(agent="ag1") -> MemoryScope:
    """agent 层 scope（task_id=None）。"""
    return MemoryScope(session_id="s1", task_id=None, agent_id=agent)


def _ev(type_, scope, content, t, role=None, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=type_, scope=scope, content=content,
        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta,
    )


def _task(task_id="t1", agent="ag1", prompt="初始请求") -> Task:
    return Task(
        id=task_id, session_id="s1", status="FINISHED", tenant_id="default",
        assigned_agent_id=agent, creator_agent_id=agent, parent_task_id=None,
        title="测试任务", description="", user_prompt=prompt,
        settings=NormalTaskSettings(),
        outputs="最终答复",
    )


async def _caps(mem, agent_scope):
    """召回 agent 层 AGENT_CONVERSATION_TURN，时间序（oldest first）。"""
    recs = await mem.recall_recent(agent_scope, [T.AGENT_CONVERSATION_TURN], 100, _ctx())
    return list(reversed(recs))


# ── 主场景：完整交错时间线 ────────────────────────────────────────────────

async def test_interleaved_capsule_order_and_roles():
    """情况 2/5 形态：[user UP1][assistant 段①][user HITL][assistant 段②]
    + [assistant finish_task(tool_calls)] [tool Process Report]。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    # task 层：模拟后台 observe 已折好后的幸存事件
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "UP1原文", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "段①摘要", 2, role="user"), _ctx())   # stored as user, must render assistant
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "HITL原文", 3, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "段②摘要", 4, role="user"), _ctx())   # ditto

    task = _task(prompt="UP1原文")
    mem_content = "最终答复\n\nProcess Report: 过程报告"
    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _ctx())

    caps = await _caps(mem, asc)

    # 6 turns total: 4 mirrored + 2 finish pair
    assert len(caps) == 6, f"expected 6 turns, got {len(caps)}: {[(c.role, c.content[:30]) for c in caps]}"

    assert caps[0].role == "user" and caps[0].content == "UP1原文"
    assert caps[1].role == "assistant" and "段①" in caps[1].content
    assert caps[2].role == "user" and caps[2].content == "HITL原文"
    assert caps[3].role == "assistant" and "段②" in caps[3].content

    # finish pair
    assert caps[-2].role == "assistant"
    tool_calls = caps[-2].metadata.get("tool_calls", [])
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"].endswith("finish_task")
    assert tool_calls[0]["input"]["result"] == "最终答复"

    assert caps[-1].role == "tool"
    assert caps[-1].content.startswith("Process Report:")

    # all have origin_task_id
    assert all(c.metadata.get("origin_task_id") == "t1" for c in caps)


# ── 原始 timestamp 保留 ───────────────────────────────────────────────────

async def test_original_timestamps_preserved():
    """镜像记录保留原始 timestamp，finish 对用 now_utc()。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    t1 = _BASE + timedelta(seconds=10)
    t2 = _BASE + timedelta(seconds=20)
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=tsc, content="q", timestamp=t1, role="user"), _ctx())
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tsc, content="s", timestamp=t2, role="user"), _ctx())

    task = _task(prompt="q")
    await _synthesize_dispatch_pair(mem, asc, task, "出了\n\nProcess Report: r", "success", _ctx())

    caps = await _caps(mem, asc)
    assert caps[0].timestamp == t1
    assert caps[1].timestamp == t2
    # finish pair timestamps >= t2 (now_utc at call time, after the task events)
    assert caps[-2].timestamp >= t2
    assert caps[-1].timestamp >= t2


# ── LLM_RESPONSE / TOOL_RESULT 携带工具元数据 ────────────────────────────

async def test_llm_response_and_tool_result_metadata_preserved():
    """LLM_RESPONSE → assistant with tool_calls; TOOL_RESULT → tool with tool_call_id。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    tc_calls = [{"id": "tc1", "name": "some_tool", "input": {}}]
    await mem.ingest(MemoryEvent(
        type=T.LLM_RESPONSE, scope=tsc, content="", timestamp=_BASE + timedelta(seconds=1),
        role="assistant", metadata={"tool_calls": tc_calls},
    ), _ctx())
    await mem.ingest(MemoryEvent(
        type=T.TOOL_RESULT, scope=tsc, content="tool out", timestamp=_BASE + timedelta(seconds=2),
        role="tool", metadata={"tool_call_id": "tc1"},
    ), _ctx())

    task = _task(prompt="q")
    await _synthesize_dispatch_pair(mem, asc, task, "o\n\nProcess Report: r", "success", _ctx())

    caps = await _caps(mem, asc)
    # caps[0] = user (from task.user_prompt — but UP1 not in memory, so no UP mirror)
    # Actually no UP in task scope → only 2 mirrored + 2 finish = 4
    llm = [c for c in caps if c.role == "assistant" and c.metadata.get("tool_calls") and
           any(tc.get("name") == "some_tool" for tc in c.metadata["tool_calls"])]
    assert len(llm) == 1
    assert llm[0].metadata["tool_calls"] == tc_calls

    tool_r = [c for c in caps if c.role == "tool" and c.metadata.get("tool_call_id") == "tc1"]
    assert len(tool_r) == 1
    assert tool_r[0].content == "tool out"


# ── TASK_COMPACT_SUMMARY role 映射：覆盖存储值 role="user" → 渲染 "assistant" ─

async def test_task_compact_summary_renders_as_assistant_not_user():
    """TASK_COMPACT_SUMMARY 存储 role='user' 但必须镜像成 role='assistant'。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    # store with role="user" as apply_compact does
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "段摘要文本", 1, role="user"), _ctx())
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "o\n\nProcess Report: r", "success", _ctx())

    caps = await _caps(mem, asc)
    summary_turn = next(c for c in caps if "段摘要文本" in c.content)
    assert summary_turn.role == "assistant", (
        f"TASK_COMPACT_SUMMARY must mirror as assistant, got role={summary_turn.role!r}"
    )


# ── finish 对的 tool_call_id 配对 ─────────────────────────────────────────

async def test_finish_pair_tool_call_id_matches():
    """finish 对：assistant.tool_calls[0].id == tool.tool_call_id。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "ans\n\nProcess Report: rpt", "success", _ctx())

    caps = await _caps(mem, asc)
    finish_assistant = caps[-2]
    finish_tool = caps[-1]
    tc_id = finish_assistant.metadata["tool_calls"][0]["id"]
    assert finish_tool.metadata.get("tool_call_id") == tc_id


# ── fail 结局在 tool content 里有 [outcome=fail] 前缀 ─────────────────────

async def test_fail_outcome_prefix_in_tool_content():
    """outcome=fail 时 tool Process Report 内容带 [outcome=fail] 前缀。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task()
    task.outputs = None

    await _synthesize_dispatch_pair(mem, asc, task, "失败报告", "fail", _ctx())

    caps = await _caps(mem, asc)
    tool_content = caps[-1].content
    assert "[outcome=fail]" in tool_content


# ── 无幸存 task 层记录时 finish 对仍写出 ─────────────────────────────────

async def test_no_task_records_still_writes_finish_pair():
    """task 层为空（清空或短 task 场景）时，仍写出 finish pair（2条）。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task(prompt="")  # empty prompt means no UP mirror either
    task.outputs = "答"

    await _synthesize_dispatch_pair(mem, asc, task, "答\n\nProcess Report: r", "success", _ctx())

    caps = await _caps(mem, asc)
    assert len(caps) == 2, f"expected 2 (finish pair only), got {len(caps)}"
    assert caps[-2].role == "assistant"
    assert caps[-1].role == "tool"


# ── 回归：dict-list outputs 须正确写入 finish result（不得为空）───────────────

async def test_finish_result_dict_list_outputs():
    """回归：task.outputs=[{"type":"text","text":...}] 时，finish tool_call input["result"]
    须等于文本内容，不能为空（旧 content_to_text 返回 "" 的 bug）。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task()
    task.outputs = [{"type": "text", "text": "结构化答复"}]

    await _synthesize_dispatch_pair(
        mem, asc, task, "结构化答复\n\nProcess Report: rpt", "success", _ctx()
    )

    caps = await _caps(mem, asc)
    finish_assistant = caps[-2]
    tool_calls = finish_assistant.metadata.get("tool_calls", [])
    assert len(tool_calls) == 1
    result = tool_calls[0]["input"]["result"]
    assert result == "结构化答复", (
        f"finish result must be '结构化答复' but got {result!r}; "
        "old content_to_text bug would yield ''"
    )
