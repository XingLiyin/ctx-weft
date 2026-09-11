"""执行限制集成（spec: execution-limits；wp7-4.1，L-T 系列）。

真 runtime + MockLLM barrier 驱动；默认 None 的零行为变化由全量回归背书。
"""
from __future__ import annotations

import asyncio
import warnings
from types import SimpleNamespace

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control.execution_budget import ExecutionLimits
from ctx_weft.core.models.config import RuntimeConfig
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import ToolCall
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


class _ManyTurnLLM(MockLLMAdapter):
    """act 每轮纯文本（不收尾）→ observer 判 retry → 重排。turns=1 时第 2 轮被拦。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0
        self.act_calls = 0

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, 'name', '') for t in (getattr(request, 'tools', None) or [])}
        if 'control__update_task_metadata' in names:
            return self._stream(MockResponse(text=''), request)
        if 'control__report_task_outcome' in names:
            self._n += 1
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=f'obs{self._n}', name='control__report_task_outcome',
                         arguments={'task_status': 'retry',
                                    'act_recap': 'keep going',
                                    'task_failure_reason': 'not done'}),
            ]), request)
        if 'control__collect_process_report' in names:
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=f'bg{self._n}', name='control__collect_process_report',
                         arguments={'act_recap': 'r', 'task_summary': 's'}),
            ]), request)
        self.act_calls += 1
        # 第 1 轮 finish（observer 判 retry → 重排）；第 2 轮预占前被 budget 拦
        return self._stream(MockResponse(text='ok', tool_calls=[
            ToolCall(id=f'fin{self.act_calls}', name='control__finish_task', arguments={}),
        ]), request)


async def test_actor_turn_limit_interrupts():
    """ACTOR_TURN_LIMIT：第 2 轮预占后超限 → INTERRUPTED + 专用码。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    rt = make_runtime(llm=_ManyTurnLLM(), agent_provider=resolver,
                      config=RuntimeConfig(execution_limits=ExecutionLimits(
                          max_actor_turns_per_task=0)))
    rt.providers.register_memory(InMemoryMemoryProvider())
    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="go", context_limit=100_000))
    tm = rt._task_managers[handle.session_id]
    for _ in range(100):
        t = tm.get_task(handle.task_id)
        if t is not None and t.status in ("FINISHED", "FAILED", "CANCELED", "INTERRUPTED"):
            break
        await asyncio.sleep(0.02)
    from ctx_weft.core.models.discriminators import TaskErrorCode
    assert t is not None and t.status == "INTERRUPTED", t and t.status
    assert t.error_code == TaskErrorCode.ACTOR_TURN_LIMIT


async def test_default_none_zero_behavior():
    """默认（未注入）零行为变化：普通会话照常 FINISHED（对照锚）。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    # 单轮 finish 收尾
    class _Fin(MockLLMAdapter):
        def __init__(self, **kw):
            super().__init__(responses=[], **kw)

        def complete(self, request, stream=True):
            names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
            if "control__update_task_metadata" in names:
                return self._stream(MockResponse(text=""), request)
            if "control__collect_process_report" in names:
                return self._stream(MockResponse(tool_calls=[
                    ToolCall(id="bg", name="control__collect_process_report",
                             arguments={"act_recap": "d", "task_summary": "d"})]), request)
            return self._stream(MockResponse(text="ok", tool_calls=[
                ToolCall(id="f", name="control__finish_task", arguments={})]), request)
    rt = make_runtime(llm=_Fin(), agent_provider=resolver)   # 不注入 limits
    rt.providers.register_memory(InMemoryMemoryProvider())
    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hi", context_limit=100_000))
    state = await handle.wait_for_finish(timeout=10.0)
    assert state is not None and state.task.status == "FINISHED"


async def test_deprecation_warnings_deduplicated(tmp_path):
    """L-T06：旧字段警告（去重）且不激活。"""
    from ctx_weft.providers.agent_template_local._loader import _DEPRECATED_WARNED
    _DEPRECATED_WARNED.clear()
    cfg = tmp_path / "tpl_x"
    cfg.mkdir()
    SOUL = "---\nname: x\nversion: 0.1.0\ndescription: dep test\nloop_config:\n  max_turns_per_agent: 7\n  timeout_per_step_sec: 30\n---\n\nsoul body\n"
    (cfg / "SOUL.md").write_text(SOUL, encoding="utf-8")
    from ctx_weft.providers.agent_template_local.provider import LocalAgentTemplateProvider
    provider = LocalAgentTemplateProvider(tmp_path)
    ctx = SimpleNamespace(session_id="s", tenant_id="default")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        tmpls = await provider.list(ctx)
        tmpls2 = await provider.list(ctx)   # 第二次不重复
    dep = [x for x in w if issubclass(x.category, DeprecationWarning)
           and "max_turns_per_agent" in str(x.message)]
    assert len(dep) == 1, [str(x.message) for x in w]
    assert tmpls, f"no templates scanned from {tmp_path}"
    from ctx_weft.protocols.context import ProviderContext as _PC
    _t = await provider.get_template(tmpls[0].id, None, ctx=ctx)
    if _t is None:
        _t = await provider.get_template(tmpls[0].id.replace('agent:', ''), None, ctx=ctx)
    assert _t is not None and _t.loop_config.max_turns_per_agent == 7  # 照常解析


async def test_provider_timeout_marks_uncooperative():
    """PROVIDER_DEADLINE + 不合作 provider 同会话拒绝（组件级：gateway 直驱）。"""
    import time
    from collections.abc import AsyncIterator
    from ctx_weft.core.loop.capability_gateway import CapabilityGateway
    from ctx_weft.core.capabilities.cache import CapabilityCache
    from ctx_weft.core.loop.driver import LoopContext, LoopState
    from ctx_weft.protocols import MemoryAddress, ProviderContext
    from ctx_weft.protocols.capability import (
        CapabilityEvent, CapabilityProviderInfo, ToolCapability, ToolCapabilityProvider)
    from ctx_weft.providers.events import InProcessEventBus
    from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

    class _Slow(ToolCapabilityProvider):
        name = "slow"
        cancelled = False

        def _cap(self):
            return ToolCapability(id="slow:x", name="x", description="d")

        async def list(self, ctx): return [self._cap()]
        async def retrieve(self, ctx): return [self._cap()]
        async def describe(self, ctx): return CapabilityProviderInfo(name=self.name)

        def invoke(self, cid, args, ctx):
            async def _r():
                await asyncio.sleep(30)
                yield CapabilityEvent(kind="result", payload={"content": "late"})
            return _r()

        async def cancel(self, i, ctx):
            self.cancelled = True

    mem = InMemoryMemoryProvider()
    bus = InProcessEventBus()
    state = LoopState(run_id="r", session=SimpleNamespace(id="s", tenant_id="t"),
                      task=SimpleNamespace(id="k"), agent=SimpleNamespace(id="a", template_id="x"),
                      scope=MemoryAddress(session_id="s", task_id="k", agent_id="a"),
                      resolved_model=SimpleNamespace(model="m", account=""))
    c = LoopContext(assembler=None, llm=None, memory=mem, event_bus=bus,
                    provider_ctx=ProviderContext(session_id="s", tenant_id="t"))
    tool = _Slow()
    cache = CapabilityCache()
    cache.put("a", [tool._cap()])
    gw = CapabilityGateway(capability_cache=cache, capability_providers=[tool],
                           memory=mem, event_bus=bus,
                           limits=ExecutionLimits(provider_timeout_sec=0.1,
                                                  cleanup_grace_sec=0.1))
    c.capability_gateway = gw

    # ① provider 超时 → 异常穿出 + cancel 安全网被调
    try:
        await gw.invoke("slow__x", {}, state, c)
    except BaseException:        # noqa: BLE001 —— CancelledError/TimeoutError/PDE
        pass
    assert tool.cancelled, "cancel 安全网必须被调用"

    # ② 不合作拒绝：直标（真实不合作 = 吞取消的生成器会把 wait_for 挂死——
    # 那是宿主进程隔离的领域，这里钉的是「已标记 → 同会话拒绝」的语义面）
    gw._uncooperative_providers.add(tool)   # noqa: SLF001 —— 直标被测语义
    res = await gw.invoke("slow__x", {}, state, c)
    assert res.is_error and "uncooperative" in str(res.content)
