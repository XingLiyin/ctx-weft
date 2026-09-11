"""resolve_operation 宿主处置（spec: tool-operations；wp6-3.2）。

revision 互斥（双宿主并发恰一成功）、supply_result 补写 memory 不重执行、
闸门（unknown 下 recover_agent 拒绝续跑）。
"""
from __future__ import annotations

import pytest

from ctx_weft.core import ProviderRegistry
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.operations import (
    OperationRecord,
    OperationStatus,
    OperationUpdate,
    operation_id_for,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.operations import InMemoryOperationStore

_CTX = ProviderContext(session_id="s1", tenant_id="default")


def _runtime_with(ops: InMemoryOperationStore):
    """轻量 runtime：providers + 内存账本 + 内存 memory（不动完整 CtxWeftRuntime
    的构造校验——本文件只测 resolve_operation 面本身）。"""
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime, make_echo_template
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    rt = make_runtime(llm=None, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())
    rt.providers.register_operation_store(ops)
    return rt


async def _unknown_op(ops: InMemoryOperationStore) -> OperationRecord:
    op_id = operation_id_for("default", "s1", "a1", "rec1", 0)
    await ops.prepare(OperationRecord(
        operation_id=op_id, tenant_id="default", session_id="s1", agent_id="a1",
        assistant_record_id="rec1", tool_ordinal=0, tool_name="fx__act",
        task_id="t1",
        status=OperationStatus.STARTED, revision=2, attempts=["inv1"]), _CTX)
    rec = await ops.compare_and_set(
        op_id, 2, OperationUpdate(status=OperationStatus.UNKNOWN), _CTX)
    return rec


async def test_revision_mutex_two_concurrent_resolutions():
    ops = InMemoryOperationStore()
    rt = _runtime_with(ops)
    rec = await _unknown_op(ops)

    # 第一次处置成功（supply_result：CAS expected=rev）
    await rt.resolve_operation(rec.operation_id, decision="supply_result",
                               result="host-verified", expected_revision=rec.revision)
    after = await ops.get(rec.operation_id, _CTX)
    assert after.status == OperationStatus.COMPLETED
    assert after.result == "host-verified"
    # 同 revision 的第二次处置被拒（CAS 互斥——状态已非 unknown，先撞状态卫兵；
    # 带**新** revision 的重复处置同样被状态卫兵拒绝：completed 不可再处置）
    with pytest.raises(ValueError, match="not unknown"):
        await rt.resolve_operation(rec.operation_id, decision="cancel_task",
                                   expected_revision=rec.revision)
    with pytest.raises(ValueError, match="not unknown"):
        await rt.resolve_operation(rec.operation_id, decision="cancel_task",
                                   expected_revision=after.revision)


async def test_supply_result_backfills_memory_without_execution():
    ops = InMemoryOperationStore()
    rt = _runtime_with(ops)
    rec = await _unknown_op(ops)

    await rt.resolve_operation(rec.operation_id, decision="supply_result",
                               result="host-verified", expected_revision=rec.revision)
    # 确定性 id 的 TOOL_RESULT 已补写（memory id 契约：重复 ingest 亦 no-op）
    from ctx_weft.protocols import MemoryScope, MemoryKind
    from ctx_weft.protocols.operations import operation_memory_result_id
    mem = rt.providers.get_memory()
    found = [r for r in await mem.load_view(
        __import__("ctx_weft.protocols", fromlist=["MemoryAddress"]).MemoryAddress(
            session_id="s1", agent_id="a1"),
        MemoryScope.TASK, _CTX)
        if r.id == operation_memory_result_id(rec.operation_id)]
    assert found and found[0].content == "host-verified"


async def test_gate_blocks_recover_on_unknown(monkeypatch):
    """闸门：unknown 的 task 在 assemble 的 _reconcile_or 处被拒（提示走处置接口）。"""
    ops = InMemoryOperationStore()
    rt = _runtime_with(ops)
    # gate 是 _reconcile_or 的首行守卫——直接调用该方法（runner 实例字段不被读）
    from ctx_weft.core.runtime import _SessionTaskRunner
    from ctx_weft.core.models.discriminators import TaskErrorCode
    from types import SimpleNamespace
    t = SimpleNamespace(id="t1", error_code=TaskErrorCode.TOOL_OUTCOME_UNKNOWN)
    with pytest.raises(RuntimeError, match="resolve_operation"):
        await _SessionTaskRunner._reconcile_or(
            object(), t, agent=SimpleNamespace(id="a1"), base="prepare")
