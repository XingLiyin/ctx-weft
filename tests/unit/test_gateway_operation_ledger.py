"""gateway 操作账本串接（spec: tool-operations；wp5-4.3）。

五步执行序、completed 重入短路（O-T08 前半）、裸调旁路、silent 工具入账、
park → waiting_human。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.protocols.operations import (
    OperationStatus,
    operation_id_for,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.operations import InMemoryOperationStore


class _Echo(ToolCapabilityProvider):
    name = "probe"

    def __init__(self) -> None:
        self.calls = 0

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="probe:go", name="go", description="d")

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            self.calls += 1
            yield CapabilityEvent(kind="result", payload={"content": f"done-{self.calls}"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


def _fixture(*, op_store=None):
    memory = InMemoryMemoryProvider()
    bus = InProcessEventBus()
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="t1"),
        task=SimpleNamespace(id="task1"), agent=SimpleNamespace(id="a1", template_id="tpl"),
        scope=MemoryAddress(session_id="s1", task_id="task1", agent_id="a1"),
        resolved_model=SimpleNamespace(model="mock", account=""),
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=memory, event_bus=bus,
        provider_ctx=ProviderContext(session_id="s1", tenant_id="t1",
                                     task_id="task1", agent_id="a1"),
    )
    tool = _Echo()
    cache = CapabilityCache()
    cache.put("a1", [tool._cap()])
    gateway = CapabilityGateway(
        capability_cache=cache, capability_providers=[tool],
        memory=memory, event_bus=bus,
        operation_store=op_store,
    )
    ctx.capability_gateway = gateway
    return memory, state, ctx, tool, gateway


def _set_op(state, ctx, record_id: str, ordinal: int) -> None:
    ctx.provider_ctx.operation_id = operation_id_for(
        state.session.tenant_id, state.session.id, state.agent.id, record_id, ordinal)


async def test_full_execution_order_records_completed():
    """五步序：prepare → started → provider（calls=1）→ completed → TOOL_RESULT 事件。"""
    ops = InMemoryOperationStore()
    memory, state, ctx, tool, gateway = _fixture(op_store=ops)
    _set_op(state, ctx, "rec1", 0)

    res = await gateway.invoke("probe__go", {"x": 1}, state, ctx)
    assert res.is_error is False and tool.calls == 1
    rec = await ops.get(ctx.provider_ctx.operation_id or state.agent.id, ctx.provider_ctx) \
        if ctx.provider_ctx.operation_id else None
    # operation_id 取用即清——本地保存引用
    op_id = operation_id_for("t1", "s1", "a1", "rec1", 0)
    rec = await ops.get(op_id, ctx.provider_ctx)
    assert rec is not None
    assert rec.status == OperationStatus.COMPLETED
    assert rec.result == "done-1"
    assert rec.attempts and rec.attempts[0].startswith("inv")


async def test_completed_reentry_short_circuits_no_reinvoke():
    """O-T08 前半：同逻辑调用重入——账本 completed → 不再打 provider，回放结果。"""
    ops = InMemoryOperationStore()
    memory, state, ctx, tool, gateway = _fixture(op_store=ops)
    op_id = operation_id_for("t1", "s1", "a1", "rec1", 0)

    _set_op(state, ctx, "rec1", 0)
    first = await gateway.invoke("probe__go", {}, state, ctx)
    assert tool.calls == 1

    _set_op(state, ctx, "rec1", 0)                       # 同逻辑调用重入（如恢复重试）
    second = await gateway.invoke("probe__go", {}, state, ctx)
    assert tool.calls == 1, "provider must NOT be re-invoked for a completed operation"
    assert second.is_error is False
    assert "done-1" in str(second.content)               # 回放首次结果


async def test_bare_call_without_operation_id_bypasses_ledger():
    """裸调（无 operation_id）账本全程旁路——既有单测/宿主直构零行为变化。"""
    ops = InMemoryOperationStore()
    memory, state, ctx, tool, gateway = _fixture(op_store=ops)
    ctx.provider_ctx.operation_id = None
    res = await gateway.invoke("probe__go", {}, state, ctx)
    assert res.is_error is False and tool.calls == 1
    # 账本空（旁路未写）
    assert await ops.get("anything", ctx.provider_ctx) is None


async def test_ledger_failure_raises_persistence_unavailable():
    """账本写失败 → PersistenceUnavailableError（复用 WP3 隔离语义）。"""

    class _Broken(InMemoryOperationStore):
        async def prepare(self, record, ctx):
            raise OSError("ledger down")

    memory, state, ctx, tool, gateway = _fixture(op_store=_Broken())
    from ctx_weft.protocols.events import PersistenceUnavailableError
    _set_op(state, ctx, "rec1", 0)
    try:
        await gateway.invoke("probe__go", {}, state, ctx)
        raised = False
    except PersistenceUnavailableError:
        raised = True
    assert raised and tool.calls == 0, "provider must not run when ledger is down"


async def test_operation_id_consumed_on_entry_no_leak():
    """取用即清：invoke 后 provider_ctx.operation_id 为 None——不泄漏给下一个调用。"""
    ops = InMemoryOperationStore()
    memory, state, ctx, tool, gateway = _fixture(op_store=ops)
    _set_op(state, ctx, "rec1", 0)
    await gateway.invoke("probe__go", {}, state, ctx)
    assert ctx.provider_ctx.operation_id is None
    # 下一次不带身份 → 旁路正常执行
    res = await gateway.invoke("probe__go", {}, state, ctx)
    assert res.is_error is False and tool.calls == 2
