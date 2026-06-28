"""Task 1: task-resident 模型翻转 — close 只写 finish 对、留 raw body。

复用 test_capsule_golden 的 helper（InMemoryMemoryProvider, _ev, _task_scope,
_agent_scope, _state, _loop_ctx, _make_task, _pctx, T, finalize_task_memory, LoopConfig）。

核心不变量（本 task 后）：
  每个结束 task = task 层 raw body（不被 supersede）+ agent 层 finish 对（2 条）。
"""
from __future__ import annotations

import pytest

from ctx_weft.protocols.template import LoopConfig

from .test_capsule_golden import (
    T,
    _agent_scope,
    _ev,
    _loop_ctx,
    _make_task,
    _pctx,
    _state,
    _task_scope,
    finalize_task_memory,
    InMemoryMemoryProvider,
)

pytestmark = pytest.mark.asyncio


async def test_close_writes_finish_pair_keeps_body() -> None:
    mem = InMemoryMemoryProvider()
    tsc = _task_scope("t1")
    asc = _agent_scope()
    await mem.ingest(_ev(T.USER_PROMPT, tsc, "做X", 1, role="user"), _pctx())
    await mem.ingest(_ev(T.LLM_RESPONSE, tsc, "在做", 2, role="assistant"), _pctx())
    await mem.ingest(_ev(T.TOOL_RESULT, tsc, "ok", 3, role="tool"), _pctx())
    task = _make_task(outputs="完成")
    await finalize_task_memory(
        mem, _state(task, tsc, LoopConfig()), task,
        "完成\n\nProcess Report: 成功", "success", _loop_ctx(mem),
    )
    # agent 层：恰 2 条 finish 对
    caps = await mem.recall_recent(asc, [T.AGENT_CONVERSATION_TURN], 500, _pctx())
    assert len(caps) == 2
    assert caps[0].role == "tool"  # newest-first: Process Report tool 在前
    asst = [c for c in caps if c.role == "assistant"][0]
    assert asst.metadata["tool_calls"][0]["name"].endswith("finish_task")
    # task 层 body 仍在（未 supersede）
    body = await mem.recall_recent(
        tsc, [T.USER_PROMPT, T.LLM_RESPONSE, T.TOOL_RESULT], 500, _pctx(),
    )
    assert len(body) == 3
