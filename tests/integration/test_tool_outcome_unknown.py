"""已知缺陷（H3）：工具副作用已发生、结果没写进 memory，恢复时被再执行一遍。

现状：ReconcileStep（core/loop/steps/reconcile.py）把「最近 assistant 回合里没有配对
TOOL_RESULT 的 tool_call」一律经 gateway 重新执行，不区分两种 dangling：
  - HITL park：工具从未执行（安全不变式保证），重跑是对的；
  - 执行中/执行后崩溃：外部副作用已经发生，只是结果没落库——重跑会把非幂等操作做两遍。
gateway 在执行**前**已写 TOOL_AUDIT（`_record_invocation`），「这次调用已经开始过」的证据
其实在，只是 reconcile 没用它。

本文件断言**应有行为**（已开始过的副作用调用恢复时不得盲目重跑），用 `xfail(strict=True)`
标记为已知缺陷；修好后 XPASS 报红，届时删掉标记。前置条件用 `pytest.fail`，不被 xfail 吞掉。
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.steps.reconcile import ReconcileStep
from ctx_weft.protocols import MemoryAddress, MemoryEvent, MemoryEventType, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

pytestmark = pytest.mark.asyncio


class _ExternalEffectTool(ToolCapabilityProvider):
    """每次 invoke 做一次「外部副作用」（计数 +1）。"""

    name = "probe"

    def __init__(self) -> None:
        self.effects = 0

    def capability(self) -> ToolCapability:
        return ToolCapability(
            id="probe:record", name="record", description="Simulated external operation",
            side_effects=True,
        )

    async def list(self, ctx):
        return [self.capability()]

    async def retrieve(self, ctx):
        return await self.list(ctx)

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx):
        async def _run():
            self.effects += 1
            yield CapabilityEvent(kind="result", payload={"content": "operation completed"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None:
        return None


def _fixture():
    memory = InMemoryMemoryProvider()
    bus = InProcessEventBus()
    state = LoopState(
        run_id="r1",
        session=type("S", (), {"id": "s1", "tenant_id": "default"})(),
        task=type("T", (), {"id": "task1"})(),
        agent=type("A", (), {"id": "agent1", "template_id": "template"})(),
        scope=MemoryAddress(session_id="s1", task_id="task1", agent_id="agent1"),
        resolved_model=type("M", (), {"model": "mock", "account": ""})(),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=memory, event_bus=bus,
        provider_ctx=ProviderContext(session_id="s1", task_id="task1", agent_id="agent1"),
    )
    tool = _ExternalEffectTool()
    cache = CapabilityCache()
    cache.put("agent1", [tool.capability()])
    ctx.capability_gateway = CapabilityGateway(
        capability_cache=cache, capability_providers=[tool],
        memory=memory, event_bus=bus,
    )
    return memory, state, ctx, tool


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="H3 未修复：ReconcileStep 对已开始执行的 dangling tool_call 盲目重跑",
)
async def test_reconcile_does_not_rerun_started_side_effect():
    memory, state, ctx, tool = _fixture()

    # 崩溃前已持久化 assistant 回合（带 tool_call），TOOL_RESULT 尚未写入
    await memory.ingest(MemoryEvent(
        type=MemoryEventType.LLM_RESPONSE, address=state.scope, content="",
        role="assistant", timestamp=datetime.now(UTC),
        metadata={"tool_calls": [{
            "id": "effect_test", "name": "probe__record",
            "input": {"operation": "increment"},
        }]},
    ), ctx.provider_ctx)

    # 第一次执行：副作用成功；写结果（role=tool 的 ingest）时模拟崩溃
    ingest = memory.ingest

    async def _fail_tool_writes(item, provider_ctx):
        if item.role == "tool":
            raise OSError("simulated tool-result persistence failure")
        return await ingest(item, provider_ctx)

    with patch.object(memory, "ingest", side_effect=_fail_tool_writes):
        with pytest.raises(OSError):
            await ctx.capability_gateway.invoke(
                "probe__record", {"operation": "increment"}, state, ctx,
                tool_call_id="effect_test",
            )
    if tool.effects != 1:
        pytest.fail(f"fixture broken: first execution should run once, ran {tool.effects}")

    # 恢复：真实 ReconcileStep，只绕过能力发现（能力已绑定进 cache）
    with patch("ctx_weft.core.loop.steps.reconcile.resolve_and_bind", new=AsyncMock()):
        await ReconcileStep().execute(state, ctx)

    assert tool.effects == 1, (
        f"reconcile re-ran a side effect that had already happened (effects={tool.effects})"
    )
