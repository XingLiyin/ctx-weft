"""close 路径 A1：机会性用结果槽 / 占位+异步替换 finish 对 Process Report。

三个测试：
1. test_a1_slot_hit_uses_background_report — bg 先完成，_close_report 预置；
   _synthesize_dispatch_pair 合成的 finish tool 记录 == "Process Report: 好报告"。
2. test_a1_placeholder_then_async_replace — 槽空 → 先用占位；随后 bg 回调替换；
   旧记录被 supersede，新记录 content=="Process Report: 好报告"，tool_call_id 配对完整。
3. test_a1_no_await_blocking — _synthesize_dispatch_pair 不调 await_pending_background_observe。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps import background_observe as bg_mod
from ctx_weft.core.loop.steps.background_observe import (
    _replace_finish_report,
    pop_close_report,
    register_close_synth,
)
from ctx_weft.core.loop.steps.finalize import _synthesize_dispatch_pair
from ctx_weft.core.state.models import NormalTaskSettings, Task
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)
SESSION = "s1"


# ─── 辅助 ─────────────────────────────────────────────────────────────────────

def _pctx() -> ProviderContext:
    return ProviderContext(session_id=SESSION, tenant_id="default")


def _task_scope(task_id: str = "t1", agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id=SESSION, task_id=task_id, agent_id=agent_id)


def _agent_scope(agent_id: str = "ag1") -> MemoryScope:
    return MemoryScope(session_id=SESSION, task_id=None, agent_id=agent_id)


def _ev(type_: MemoryEventType, scope: MemoryScope, content: str, t: int,
        role: str | None = None, **meta) -> MemoryEvent:
    return MemoryEvent(
        type=type_, scope=scope, content=content,
        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta,
    )


def _make_task(task_id: str = "t1", agent_id: str = "ag1",
               outputs: str | None = "最终答复") -> Task:
    return Task(
        id=task_id, session_id=SESSION, status="FINISHED", tenant_id="default",
        assigned_agent_id=agent_id, creator_agent_id=agent_id, parent_task_id=None,
        title="测试任务", description="", user_prompt="初始请求",
        settings=NormalTaskSettings(), outputs=outputs,
    )


async def _get_finish_tool(mem: InMemoryMemoryProvider, scope: MemoryScope) -> MemoryEvent | None:
    """取 agent scope 内最新的 tool role AGENT_CONVERSATION_TURN（finish 记录）。"""
    recs = await mem.recall_recent(scope, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    tool_recs = [r for r in recs if r.role == "tool"]
    return tool_recs[0] if tool_recs else None  # recall_recent returns newest-first


# ─── autouse fixture：清 module-level 字典 ─────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_bg_dicts():
    """每个测试前后清空 _close_report 和 _close_synth，隔离状态。"""
    bg_mod._close_report.clear()
    if hasattr(bg_mod, "_close_synth"):
        bg_mod._close_synth.clear()
    yield
    bg_mod._close_report.clear()
    if hasattr(bg_mod, "_close_synth"):
        bg_mod._close_synth.clear()


# ─── TEST 1: slot hit ─────────────────────────────────────────────────────────

async def test_a1_slot_hit_uses_background_report() -> None:
    """bg 先完成：_close_report[task.id] 预置 "好报告"；
    _synthesize_dispatch_pair 合成的 finish tool 内容 == "Process Report: 好报告"。
    无后续替换（slot 命中即直接用）。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    await mem.ingest(_ev(T.USER_PROMPT, tsc, "初始请求", 1, role="user"), _pctx())

    task = _make_task()
    mem_content = "最终答复\n\nProcess Report: 占位摘要"  # placeholder from observer

    # 预置 bg 结果（background 先到）
    bg_mod._close_report["t1"] = "好报告"

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    finish_tool = await _get_finish_tool(mem, asc)
    assert finish_tool is not None, "finish tool record must exist"
    assert finish_tool.content == "Process Report: 好报告", (
        f"slot hit: finish tool content must be 'Process Report: 好报告'; got {finish_tool.content!r}"
    )

    # slot 已被 pop，无残留
    assert pop_close_report("t1") is None, "_close_report slot must be cleared after pop"

    # _close_synth 不应登记（slot 命中不需要异步替换）
    if hasattr(bg_mod, "_close_synth"):
        assert "t1" not in bg_mod._close_synth, (
            "slot hit must NOT register _close_synth (no async replace needed)"
        )


# ─── TEST 2: placeholder + async replace ──────────────────────────────────────

async def test_a1_placeholder_then_async_replace() -> None:
    """槽空 → finalize 先合成用占位 + 登记 _close_synth；
    随后 bg 回调（直接调 _replace_finish_report）替换 finish tool 记录：
    - 旧记录被 supersede（不再出现）
    - 新记录 content == "Process Report: 好报告"
    - tool_call_id 配对相同（不产生悬空 pair）
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    await mem.ingest(_ev(T.USER_PROMPT, tsc, "初始请求", 1, role="user"), _pctx())

    task = _make_task()
    mem_content = "最终答复\n\nProcess Report: 占位摘要"

    # 槽空（_close_report 为空）→ 占位路径
    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    # 占位记录已写入
    finish_tool_placeholder = await _get_finish_tool(mem, asc)
    assert finish_tool_placeholder is not None, "placeholder finish tool must exist"
    assert "Process Report:" in finish_tool_placeholder.content, (
        f"placeholder must contain 'Process Report:'; got {finish_tool_placeholder.content!r}"
    )
    placeholder_tool_call_id = finish_tool_placeholder.metadata.get("tool_call_id")
    assert placeholder_tool_call_id, "placeholder finish tool must have tool_call_id"

    # _close_synth 已登记
    assert hasattr(bg_mod, "_close_synth"), "background_observe must have _close_synth dict"
    assert "t1" in bg_mod._close_synth, (
        "_close_synth must have t1 registered after placeholder path"
    )
    synth = bg_mod._close_synth["t1"]
    registered_tool_call_id = synth[0]
    registered_scope = synth[1]
    assert registered_tool_call_id == placeholder_tool_call_id, (
        "registered tool_call_id must match placeholder finish tool"
    )

    # 模拟 bg 回调：先 pop_close_synth（正如 _run_background_observe 那样），再调 _replace_finish_report
    from ctx_weft.core.loop.steps.background_observe import pop_close_synth
    popped = pop_close_synth("t1")
    assert popped is not None, "pop_close_synth must return the registered synth"
    p_tool_call_id, p_scope, p_outcome = popped
    await _replace_finish_report(
        mem, _pctx(), p_scope, "t1",
        p_tool_call_id, "好报告", p_outcome,
    )

    # 旧占位记录应被 supersede（不再出现）
    all_tool_recs = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    active_tool_recs = [r for r in all_tool_recs if r.role == "tool"]
    assert len(active_tool_recs) == 1, (
        f"after replace, exactly 1 active finish tool record expected; got {len(active_tool_recs)}: "
        f"{[r.content for r in active_tool_recs]}"
    )
    new_finish_tool = active_tool_recs[0]
    assert new_finish_tool.content == "Process Report: 好报告", (
        f"new finish tool content must be 'Process Report: 好报告'; got {new_finish_tool.content!r}"
    )
    assert new_finish_tool.metadata.get("tool_call_id") == placeholder_tool_call_id, (
        "replaced record must keep same tool_call_id for proper pair matching"
    )

    # _close_synth 应已被 pop_close_synth 清空
    assert "t1" not in bg_mod._close_synth, (
        "_close_synth must be cleared after pop_close_synth in bg callback simulation"
    )


# ─── TEST 3: no blocking await ────────────────────────────────────────────────

async def test_a1_no_await_blocking(monkeypatch) -> None:
    """_synthesize_dispatch_pair 不调 await_pending_background_observe（非阻塞）。"""
    called = []

    async def _fake_await(task_id: str) -> None:
        called.append(task_id)

    # Monkeypatch the function in the background_observe module
    monkeypatch.setattr(bg_mod, "await_pending_background_observe", _fake_await)

    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()

    await mem.ingest(_ev(T.USER_PROMPT, tsc, "初始请求", 1, role="user"), _pctx())

    task = _make_task()
    mem_content = "最终答复\n\nProcess Report: 摘要"

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    assert called == [], (
        f"_synthesize_dispatch_pair must NOT call await_pending_background_observe (A1 non-blocking); "
        f"called with: {called}"
    )


# ─── TEST 4/5: 方案2 最终段 raw 折叠 ───────────────────────────────────────────

async def _ingest_user_plus_raw(mem: InMemoryMemoryProvider, tsc: MemoryScope) -> None:
    """task 层：user 锚点 + 最终段 raw（LLM_RESPONSE + TOOL_RESULT，close 边界未折）。"""
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "初始请求", 1, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "我在读目录…", 2, role="assistant"), _pctx())
    await mem.ingest(_ev(T.TOOL_RESULT, tsc, "目录内容…", 3, role="tool"), _pctx())


async def test_slot_hit_replaces_report_no_raw_mirror() -> None:
    """task-resident：close 不镜像 body（无 final_segment_raw 镜像）；slot hit 时 finish tool
    用真实 report；user 锚点 + 最终段 raw 留 task 层（不进 agent 层）。

    注：原「方案2 折最终段 raw 镜像」随 mirror 删除而失效——agent 层从不写 final_segment_raw，
    body raw 留 task 层（长任务压缩由 Task 2 处理）。
    """
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()
    await _ingest_user_plus_raw(mem, tsc)

    task = _make_task()
    mem_content = "最终答复\n\nProcess Report: 占位摘要"
    bg_mod._close_report["t1"] = "真实段总结"  # background 先完成

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    recs = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    # agent 层从不写 final_segment_raw 镜像（task-resident）
    assert [r for r in recs if r.metadata.get("final_segment_raw")] == []
    # agent 层只有 finish 对（无 body 镜像）
    assert len(recs) == 2 and {r.role for r in recs} == {"assistant", "tool"}
    finish_tool = await _get_finish_tool(mem, asc)
    assert finish_tool.content == "Process Report: 真实段总结"
    # user 锚点 + 最终段 raw 留 task 层
    body = await mem.recall_recent(tsc, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 500, _pctx())
    assert any(r.role == "user" and "初始请求" in r.content for r in body), "user 锚点留 task 层"
    assert len(body) == 3, "最终段 raw body 留 task 层（task-resident）"


async def test_degraded_keeps_task_layer_raw_body() -> None:
    """task-resident：close 不镜像 body——降级（槽空）时 agent 层仍只 finish 对，
    最终段 raw body 留 task 层（不被 supersede）。"""
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()
    await _ingest_user_plus_raw(mem, tsc)

    task = _make_task()
    mem_content = "最终答复\n\nProcess Report: 占位摘要"
    # 槽空 → 占位 + register

    await _synthesize_dispatch_pair(mem, asc, task, mem_content, "success", _pctx())

    recs = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    # 无 final_segment_raw 镜像（agent 层不再镜像 body）
    assert [r for r in recs if r.metadata.get("final_segment_raw")] == []
    assert len(recs) == 2 and {r.role for r in recs} == {"assistant", "tool"}
    # 最终段 raw body 留 task 层
    body = await mem.recall_recent(tsc, [T.LLM_RESPONSE, T.TOOL_RESULT], 500, _pctx())
    assert len(body) == 2, "降级时最终段 raw body 留 task 层（task-resident）"
