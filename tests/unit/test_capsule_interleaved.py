"""_synthesize_dispatch_pair 合成 finish 对（task-resident，spec 2026-06-28 §3.1）。

形态翻转（Task 1）：close **不再镜像** task 层 body 进 agent 层——只写 finish 对
（assistant finish_task tool_call + tool Process Report）。body 留各自 task 层。

本文件原「交错时间线镜像」相关测试（镜像顺序/原始 timestamp/LLM 与 TOOL 元数据保留/段摘要
role 映射）随 mirror 删除而移除——其验证的镜像机制已不存在。保留 finish-对结构/配对/result
切割/fail 前缀等仍适用于新模型的断言。
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


# ── task-resident：合成只写 finish 对、不镜像 body ────────────────────────────

async def test_synthesize_writes_only_finish_pair():
    """task-resident：task 层有 UP/段摘要/LLM/TOOL 时，agent 层仍只写 finish 对（2 条）。
    body 不镜像、留 task 层。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    # task 层：模拟后台 observe 已折好后的幸存事件（不会被镜像）
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "UP1原文", 1, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "段①摘要", 2, role="assistant"), _ctx())
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "HITL原文", 3, role="user"), _ctx())
    await mem.ingest(_ev(T.TASK_COMPACT_SUMMARY, tsc, "段②摘要", 4, role="assistant"), _ctx())

    task = _task(prompt="UP1原文")
    mem_content = "最终答复\n\nProcess Report: 过程报告"
    await _synthesize_dispatch_pair(mem, asc, task, "最终答复", "Process Report: 过程报告", "success", _ctx())

    caps = await _caps(mem, asc)

    # 仅 finish 对（assistant finish_task + tool Process Report）
    assert len(caps) == 2, f"expected only finish pair; got {[(c.role, c.content[:30]) for c in caps]}"
    assert caps[-2].role == "assistant"
    tool_calls = caps[-2].metadata.get("tool_calls", [])
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"].endswith("finish_task")
    assert tool_calls[0]["input"]["result"] == "最终答复"
    assert caps[-1].role == "tool"
    assert caps[-1].content.startswith("Process Report:")
    assert all(c.metadata.get("origin_task_id") == "t1" for c in caps)

    # body 不镜像、留 task 层
    body = await mem.recall_recent(tsc, [T.USER_PROMPT, T.TASK_COMPACT_SUMMARY], 100, _ctx())
    assert len(body) == 4, "task-layer body must stay (not mirrored/superseded)"


async def test_finish_pair_timestamp_anchors_close():
    """finish 对 timestamp 锚 close 时刻（now_utc），落在 task 层 body 之后。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_scope()
    asc = _agent_scope()

    t2 = _BASE + timedelta(seconds=20)
    await mem.ingest(MemoryEvent(type=T.USER_PROMPT, scope=tsc, content="q",
                                 timestamp=_BASE + timedelta(seconds=10), role="user"), _ctx())
    await mem.ingest(MemoryEvent(type=T.TASK_COMPACT_SUMMARY, scope=tsc, content="s",
                                 timestamp=t2, role="assistant"), _ctx())

    task = _task(prompt="q")
    await _synthesize_dispatch_pair(mem, asc, task, "出了\n\nProcess Report: r", "", "success", _ctx())

    caps = await _caps(mem, asc)
    assert len(caps) == 2
    # finish pair timestamps >= t2 (now_utc at call time, after the task events)
    assert caps[-2].timestamp >= t2
    assert caps[-1].timestamp >= t2


# ── finish 对的 tool_call_id 配对 ─────────────────────────────────────────

async def test_finish_pair_tool_call_id_matches():
    """finish 对：assistant.tool_calls[0].id == tool.tool_call_id。"""
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task()

    await _synthesize_dispatch_pair(mem, asc, task, "ans\n\nProcess Report: rpt", "", "success", _ctx())

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

    await _synthesize_dispatch_pair(mem, asc, task, "失败报告", "", "fail", _ctx())

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

    await _synthesize_dispatch_pair(mem, asc, task, "答\n\nProcess Report: r", "", "success", _ctx())

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
        mem, asc, task, "结构化答复\n\nProcess Report: rpt", "", "success", _ctx()
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


async def test_embedded_process_report_in_outputs():
    """outputs 文本本身包含 "Process Report: " 时，必须用 full separator rsplit
    从最后一个分隔符切割，防止误切。

    示例：
      outputs = "I wrote a Process Report: draft"
      summary = "real report"
      mem_content = "I wrote a Process Report: draft\\n\\nProcess Report: real report"

    旧代码用 split("Process Report: ", 1)[-1] 取第一个分隔符后的内容：
      "draft\\n\\nProcess Report: real report"  (错误，report 被 outputs 污染)

    新代码用 rsplit("\\n\\nProcess Report: ", 1)[-1] 从最后分隔符切割：
      "real report"  (正确)
    """
    mem = InMemoryMemoryProvider()
    asc = _agent_scope()
    task = _task()
    task.outputs = "I wrote a Process Report: draft"

    # _build_memory_content 会拼成：
    # "I wrote a Process Report: draft\\n\\nProcess Report: real report"
    mem_content = "I wrote a Process Report: draft\n\nProcess Report: real report"

    await _synthesize_dispatch_pair(mem, asc, task, "I wrote a Process Report: draft", "Process Report: real report", "success", _ctx())

    caps = await _caps(mem, asc)
    finish_tool = caps[-1]

    # 验证 tool content 从完整分隔符后切割，即只含 "real report"
    # 而非 "draft\\n\\nProcess Report: real report"
    tool_content = finish_tool.content
    assert tool_content == "Process Report: real report", (
        f"tool content should be 'Process Report: real report' but got {tool_content!r}; "
        "old split logic would include embedded separator content"
    )
