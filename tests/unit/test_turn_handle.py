"""TurnHandle：句柄以 agent + task 为轴（2026-09-04 spec §3）。

run 是引擎内部一轮循环的相关性 id，句柄的职责是「指着一个外部可寻址的对象」。
agent + task 已经够定位，events() 与 wait_for_finish() 都不需要 run_id——
把它放进句柄只会多一个无法诚实填写的字段。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ctx_weft.core.runtime import SessionStartParams, TurnHandle
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEventType,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    ToolCall,
)
from ctx_weft.protocols.events import Event
from ctx_weft.providers.events.bus.in_process.bus import InProcessEventBus
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

pytestmark = pytest.mark.asyncio


def _ev(**kw) -> Event:
    base = dict(
        id="evt_1", run_id="run_1", sequence=0, session_id="s1",
        type="TaskStarted", timestamp=datetime.now(UTC), origin="runtime",
        agent_id="agt_1", task_id="tsk_1",
    )
    base.update(kw)
    return Event(**base)


async def test_handle_has_no_run_id_field():
    """结构性守卫：run_id 不在对外句柄上。"""
    assert "run_id" not in TurnHandle.__dataclass_fields__


async def test_events_filters_by_agent_and_task():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    got: list[str] = []

    async def _consume():
        async for ev in h.events():
            got.append(ev.id)
            if len(got) == 2:
                return

    t = asyncio.create_task(_consume())
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1"))
    await bus.emit(_ev(id="e2", agent_id="agt_2"))          # 别的 agent
    await bus.emit(_ev(id="e3", task_id="tsk_2"))           # 同 agent 别的 task
    await bus.emit(_ev(id="e4"))
    await asyncio.wait_for(t, timeout=2.0)

    assert got == ["e1", "e4"]


async def test_wait_for_finish_returns_on_task_terminal_event():
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus, _state=None)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=2.0))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="TaskFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)


async def test_wait_for_finish_ignores_run_finished():
    """RunFinished 不再是判据——一轮 run 结束不等于这条 task 结束（可能还要 finalize）。"""
    bus = InProcessEventBus()
    h = TurnHandle(session_id="s1", agent_id="agt_1", task_id="tsk_1",
                   template_id="tpl", event_bus=bus)
    waiter = asyncio.create_task(h.wait_for_finish(timeout=0.3))
    await asyncio.sleep(0)
    await bus.emit(_ev(id="e1", type="RunFinished"))
    await asyncio.wait_for(waiter, timeout=2.0)   # 靠超时返回，不是靠 RunFinished


class _CloseFoldLLM(MockLLMAdapter):
    """单 root task：finish_task 收尾触发 close 边界后台 observe（launch_background_observe
    fire-and-forget），验证 `wait_for_finish` 返回**那一刻**该次折叠已经落地——不是靠
    调用方碰巧多等了一会儿。按 request.tools 路由：recognize_intent → 空文本；
    background observe（`control__collect_process_report`）→ 折叠摘要；其余（act）→
    finish_task 收尾。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def _id(self, kind: str) -> str:
        self._n += 1
        return f"{kind}{self._n}"

    def complete(self, request, stream=True):
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:  # recognize_intent
            return self._stream(MockResponse(text=""), request)
        if "control__collect_process_report" in names:  # background observe
            return self._stream(MockResponse(tool_calls=[
                ToolCall(id=self._id("bg"), name="control__collect_process_report",
                         arguments={"act_recap": "CLOSE段摘要"}),
            ]), request)
        return self._stream(MockResponse(text="done", tool_calls=[
            ToolCall(id=self._id("fin"), name="control__finish_task",
                     arguments={"deliverables_summary": "done"}),
        ]), request)


async def test_wait_for_finish_returns_after_deferred_fold_lands():
    """回归守卫（2026-09-04）：root task 收尾会 fire-and-forget 一个后台 observe 去折叠/
    胶囊化本段 raw（spec 2026-07-20 延迟折叠）。这个折叠任务的登记先于 TaskFinished 发出
    （同一协程内、无 await 间隔），但它的执行体何时真正跑完，相对 TaskFinished 谁先谁后
    是一场纯粹的 asyncio 调度竞态——`wait_for_finish` 若只等 TaskFinished 就返回，会在
    折叠完工前把控制权交还调用方（`test_dispatch_boundary_recap_e2e` 曾经~1/3 概率
    因此失败：断言时看到本该被折掉的 raw）。

    本测试不靠任何额外 `sleep` 或轮询掩盖窗口：`wait_for_finish` 一返回就立刻查 memory，
    折叠结果必须已经可见。"""
    llm = _CloseFoldLLM()
    resolver = InlineAgentTemplateProvider()
    template = make_echo_template()
    # 关短任务/短段免折两道门（_is_short_leaf 用 short_task_token_threshold，close 边界
    # 走这条；is_short_segment 用 short_segment_token_threshold，非 close 边界走那条）——
    # 都关掉才能强制走延迟折叠，而不是「短 → 原文即胶囊」的免折捷径。
    template.loop_config.short_task_token_threshold = 0
    template.loop_config.short_segment_token_threshold = 0
    resolver.register(template)
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)

    handle = await runtime.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="say hello", context_limit=100_000,
    ))
    await handle.wait_for_finish(timeout=8.0)

    # close 边界的后台 observe 不写 TASK_COMPACT_SUMMARY（那是非 close 边界 segment_fold 的
    # 产物）——它把真报告替换进 finish 对（AGENT scope 的 conversation turn，见
    # background_observe._replace_finish_report / finalize.build_finish_slots），随后
    # 补删 task scope 的末段 raw（_supersede_final_raw_segment）。两处都得在 wait_for_finish
    # 返回那一刻已经落地，而不是靠调用方之后又多等了一会儿才凑巧看到。
    task_scope = MemoryAddress(session_id=handle.session_id, task_id=handle.task_id,
                               agent_id=handle.agent_id)
    agent_scope = MemoryAddress(session_id=handle.session_id, agent_id=handle.agent_id)
    pctx = ProviderContext(session_id=handle.session_id, tenant_id="default",
                           task_id=handle.task_id, agent_id=handle.agent_id)

    turns = await memory.load_view(
        agent_scope, MemoryScope.AGENT, pctx, kinds=[MemoryKind.CONVERSATION_TURN])
    recap = [r for r in turns if r.role == "assistant"
             and r.metadata.get("origin_task_id") == handle.task_id
             and not r.metadata.get("final_reply")]
    assert any("CLOSE段摘要" in r.content for r in recap), \
        ("wait_for_finish 返回时，close 边界后台 observe 的真报告必须已经替换进 finish 对，"
         f"实得 {[r.content for r in recap]}")

    raws = await memory.recall_recent(task_scope, [MemoryEventType.LLM_RESPONSE], 100, pctx)
    assert raws == [], \
        f"wait_for_finish 返回时，本段 raw 应已被折掉/胶囊化，实得 {[r.content for r in raws]}"


async def test_start_session_returns_turn_handle():
    """裁定 A（控制方 2026-09-04）：brief 原版 setup（`template_id="echo"` + 未注册
    memory provider）在本仓库跑不起来——`start_session` 会在模板解析阶段先炸。改用
    `test_runtime_agent_api.py::test_start_session_agent_id_is_addressable_root_agent`
    验证过的搭台手法：`template_id="agent:tpl_echo"`、注册 memory provider、LLM 回
    `control__finish_task` 让 run 能终结。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[MockResponse(tool_calls=[
        ToolCall(id="tc1", name="control__finish_task", arguments={"result": "done"})])])
    rt = make_runtime(llm=llm, agent_provider=resolver)
    rt.providers.register_memory(InMemoryMemoryProvider())

    handle = await rt.start_session(SessionStartParams.create(
        template_id="agent:tpl_echo", user_prompt="hi", context_limit=100_000,
    ))
    await handle.wait_for_finish(timeout=5.0)

    assert isinstance(handle, TurnHandle)
    assert handle.agent_id and handle.task_id and handle.session_id and handle.template_id
