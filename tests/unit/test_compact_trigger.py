"""PrepareStep._should_compact 的累积增长触发（spec/06 §7）。

根因：原实现只数 **agent 层**（派发日志）增长，task 层（USER_PROMPT/LLM_RESPONSE/
TOOL_RESULT）无界增长却没有任何触发——observe 的 max_turns 压缩又被 HITL park 的异常
unwind 绕过（park → SUSPENDED，不经 observe）。HITL 密集的叶子任务因此永不压缩。

修复：_should_compact 对 **agent 层与 task 层分别**按可折叠 active 条数触发，达到
compact_message_delta 即压。用绝对条数（无 baseline）：冷 resume 后 loop_guard 归零，
delta-from-baseline 永远点不着；而压缩会把 active 条数降到 keep_last 以下，绝对阈值自纠偏。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop.steps.prepare import PrepareStep
from ctx_weft.protocols import MemoryEvent, MemoryEventType, MemoryScope, ProviderContext
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio
T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _scope() -> MemoryScope:
    return MemoryScope(session_id="s1", task_id="t1", agent_id="a1")


def _pctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


async def _ingest_n(mem, type_, n: int, role: str) -> None:
    for i in range(n):
        await mem.ingest(
            MemoryEvent(type=type_, scope=_scope(), content=f"c{i}",
                        timestamp=_BASE + timedelta(seconds=i), role=role),
            _pctx(),
        )


def _ctx(mem):
    return SimpleNamespace(memory=mem, provider_ctx=_pctx(), task_manager=None)


def _state(*, delta=20, ratio=0.8, context_limit=100000):
    agent = SimpleNamespace(
        id="a1",
        loop_config=SimpleNamespace(compact_token_ratio=ratio, compact_message_delta=delta),
        loop_guard=SimpleNamespace(context_limit=context_limit),
    )
    return SimpleNamespace(agent=agent, scope=_scope())


async def test_task_layer_growth_triggers_compaction() -> None:
    """task 层 active 条数达 delta、agent 层为空、token 不高 → 必须触发（核心修复）。"""
    mem = InMemoryMemoryProvider()
    await _ingest_n(mem, T.LLM_RESPONSE, 20, role="assistant")  # 纯 task 层
    assert await PrepareStep()._should_compact(_state(delta=20), _ctx(mem), token_estimate=10) is True


async def test_agent_layer_growth_still_triggers() -> None:
    """回归：agent 层派发日志达 delta 仍触发。"""
    mem = InMemoryMemoryProvider()
    await _ingest_n(mem, T.TASK_DISPATCH_RESULT, 20, role="tool")
    assert await PrepareStep()._should_compact(_state(delta=20), _ctx(mem), token_estimate=10) is True


async def test_below_delta_no_trigger() -> None:
    """两层都不足 delta、token 不高 → 不触发。"""
    mem = InMemoryMemoryProvider()
    await _ingest_n(mem, T.LLM_RESPONSE, 5, role="assistant")
    await _ingest_n(mem, T.TASK_DISPATCH_RESULT, 5, role="tool")
    assert await PrepareStep()._should_compact(_state(delta=20), _ctx(mem), token_estimate=10) is False


async def test_token_ratio_still_triggers() -> None:
    """token 比例达阈值 → 触发，与条数无关。"""
    mem = InMemoryMemoryProvider()
    state = _state(ratio=0.8, context_limit=1000)
    assert await PrepareStep()._should_compact(state, _ctx(mem), token_estimate=900) is True
