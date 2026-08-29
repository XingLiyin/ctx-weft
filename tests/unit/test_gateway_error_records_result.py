"""Gateway 错误出口必须补一条配对 TOOL_RESULT，避免悬挂 tool_call（orphan）。

回归 spec/06 §4 无损重建：act 循环在派发**之前**就把 assistant LLM_RESPONSE（含 tool_calls）
落 memory。若 gateway.invoke 在执行前的错误出口（未授权 / 非法参数 / 未知工具 / 无 provider）
直接 return error_result 而不落 TOOL_RESULT，则该 tool_call 在持久内存里没有配对 result——
当某次 prompt 纯从 memory 重组（observe at max_turns / resume 恢复）时，会还原出一条
assistant(tool_calls=[id]) 却无应答 tool 消息 → OpenAI 400「insufficient tool messages
following tool_calls message」。

live 消息列表当场是配平的（error_result 被 append 进 current_messages），所以运行中的 act 不报错，
唯独从 memory 重组时炸——故必须在错误出口也落一条配对 TOOL_RESULT。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.core.auth.authorizer import AuthorizationDecision, Authorizer
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.orchestrator.capability_cache import CapabilityCache
from ctx_weft.protocols import MemoryEventType, MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider


class _Echo(ToolCapabilityProvider):
    name = "mcp:a"

    def __init__(self, schema: dict | None = None) -> None:
        self.invoked = False
        self._schema = schema or {}

    def _cap(self) -> ToolCapability:
        return ToolCapability(id="mcp:a:search", name="search", description="s",
                              input_schema=self._schema)

    async def list(self, ctx): return [self._cap()]
    async def retrieve(self, ctx): return [self._cap()]
    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        async def _run():
            self.invoked = True
            yield CapabilityEvent(kind="result", payload={"content": "ok"})
        return _run()

    async def cancel(self, invocation_id, ctx) -> None: return None


class _BlockAll(Authorizer):
    """镜像生产里工作目录守卫的拒绝（path 在 workspace 之外，已拒绝写入）。"""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, message="路径在工作目录之外，已拒绝写入")


def _state_ctx():
    mem = InMemoryMemoryProvider()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(
        run_id="r1", session=SimpleNamespace(id="s1", tenant_id="default"),
        task=SimpleNamespace(id="tsk_1"), agent=SimpleNamespace(id="agt_1", template_id="t"),
        scope=scope,
    )
    ctx = LoopContext(
        assembler=None, llm=None, memory=mem, event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1"),
    )
    return mem, state, ctx, scope


def _gw(provider, mem, default_authorizer=None):
    cache = CapabilityCache()
    cache.put("agt_1", [provider._cap()])
    return CapabilityGateway(
        capability_cache=cache, capability_providers=[provider],
        memory=mem, event_bus=InProcessEventBus(),
        default_authorizer=default_authorizer,
    )


async def _tool_result_ids(mem, scope, ctx) -> list[str]:
    recs = await mem.recall_recent(scope, [MemoryEventType.TOOL_RESULT], 100, ctx.provider_ctx)
    return [r.metadata.get("tool_call_id") for r in recs]


async def test_blocked_call_records_paired_tool_result() -> None:
    # 授权器拒绝（生产触发点）→ gateway 返回 error，但 memory 必须有配对 TOOL_RESULT，否则悬挂。
    p = _Echo()
    mem, state, ctx, scope = _state_ctx()
    res = await _gw(p, mem, default_authorizer=_BlockAll()).invoke(
        "mcp__a__search", {"path": "C:/temp/x.vbs"}, state, ctx, tool_call_id="call_blocked"
    )
    assert res.is_error is True
    assert p.invoked is False  # 安全不变式：拒绝绝不下发 provider
    assert "call_blocked" in await _tool_result_ids(mem, scope, ctx)


async def test_invalid_args_records_paired_tool_result() -> None:
    # 非法参数错误出口同样必须落配对 TOOL_RESULT。
    p = _Echo({"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
    mem, state, ctx, scope = _state_ctx()
    res = await _gw(p, mem).invoke("mcp__a__search", {}, state, ctx, tool_call_id="call_badargs")
    assert res.is_error is True
    assert "call_badargs" in await _tool_result_ids(mem, scope, ctx)


async def test_unknown_tool_records_paired_tool_result() -> None:
    # 未知工具（LLM 幻觉）也会落进 LLM_RESPONSE.tool_calls，故同样需配对 TOOL_RESULT。
    p = _Echo()
    mem, state, ctx, scope = _state_ctx()
    res = await _gw(p, mem).invoke("mcp__a__nope", {}, state, ctx, tool_call_id="call_unknown")
    assert res.is_error is True
    assert "call_unknown" in await _tool_result_ids(mem, scope, ctx)
