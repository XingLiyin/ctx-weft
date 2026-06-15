"""interactive vs auto task + finish_task + 临时 guidance 注入。

- interactive 任务 actor 纯文本 → HITL input 冷 park（等用户），不产出、不完成。
- auto 任务 actor 纯文本 → 旧行为：文本即 outputs，路由 observe。
- finish_task → 写 outputs + 完成（端到端经 gateway/observe/finalize）。
- 临时 guidance（title/description/后继/完成方式）只进发送的 prompt，不入 memory。
"""

from __future__ import annotations

import pytest

from loomex_core.core import LoomeXRuntime
from loomex_core.core.assembler.assembler import AssembledPrompt
from loomex_core.core.events.bus import InProcessEventBus
from loomex_core.core.loop.driver import LoopContext, LoopState
from loomex_core.core.loop.park import HitlPark
from loomex_core.core.loop.steps.act import ActStep
from loomex_core.core.orchestrator.hitl_manager import HitlManager
from loomex_core.core.state.models import Agent, NormalTaskSettings, Session, Task
from loomex_core.protocols import (
    LLMMessage, MemoryEventType, MemoryScope, ProviderContext, ToolCall,
)
from loomex_core.providers.llm.mock import MockLLMAdapter, MockResponse
from loomex_core.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import InMemoryTemplateResolver, make_echo_template

pytestmark = pytest.mark.asyncio


def _act_state_ctx(interaction_mode: str, llm: MockLLMAdapter):
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl = HitlManager(event_bus=bus)
    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING", token_budget=0)
    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        title="Greet", description="say hi politely",
        interaction_mode=interaction_mode, settings=NormalTaskSettings(),
    )
    agent = Agent(id="ag1", session_id="s1", template_id="t", template_version="1", status="RUNNING")
    scope = MemoryScope(session_id="s1", task_id="t1", agent_id="ag1")
    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content="hi")], tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope, assembled_prompt=prompt,
    )
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1"),
        hitl_manager=hitl,
    )
    return state, ctx, task, hitl, mem


async def test_interactive_plain_text_parks_for_user() -> None:
    llm = MockLLMAdapter(responses=[MockResponse(text="Hi! Anything else?")])
    state, ctx, task, hitl, mem = _act_state_ctx("interactive", llm)

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    assert task.status == "SUSPENDED"
    assert task.outputs is None                       # 纯文本不是产出
    pend = hitl.list_pending("s1")
    assert len(pend) == 1
    assert pend[0].kind == "input"
    assert pend[0].capability_id.endswith(":wait_for_user")

    # 临时 guidance 只在发送的 prompt，不入 memory
    sent = llm.last_request.messages[-1].content
    assert "## Your current task" in sent and "finish_task" in sent
    recs = await mem.recall_recent(state.scope, [MemoryEventType.LLM_RESPONSE], 10, ctx.provider_ctx)
    assert recs and all("## Your current task" not in (r.content or "") for r in recs)


async def test_auto_plain_text_completes() -> None:
    llm = MockLLMAdapter(responses=[MockResponse(text="Hi! Anything else?")])
    state, ctx, task, hitl, _mem = _act_state_ctx("auto", llm)

    outcome = await ActStep().execute(state, ctx)

    assert outcome.next_step == "observe"
    assert task.status != "SUSPENDED"
    assert task.outputs == "Hi! Anything else?"        # auto: 文本即产出（旧行为）
    assert hitl.list_pending("s1") == []


async def test_finish_task_finishes_task_end_to_end() -> None:
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[
        MockResponse(tool_calls=[ToolCall(
            id="tc1", name="control__finish_task", arguments={"result": "computed: 42"},
        )]),
    ])
    runtime = LoomeXRuntime(llm=llm, template_resolver=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    _handle, state = await runtime.run_single_task(template_id="tpl_echo", user_prompt="compute")

    assert state.task.status == "FINISHED"
    assert state.task.outputs == "computed: 42"


async def test_guidance_injected_into_prompt_not_memory() -> None:
    resolver = InMemoryTemplateResolver()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[
        MockResponse(tool_calls=[ToolCall(
            id="tc1", name="control__finish_task", arguments={"result": "ok"},
        )]),
    ])
    runtime = LoomeXRuntime(llm=llm, template_resolver=resolver)
    mem = InMemoryMemoryProvider()
    runtime.providers.register_memory(mem)

    _handle, state = await runtime.run_single_task(template_id="tpl_echo", user_prompt="hello")

    # 发送的 prompt 含 guidance
    sent = llm.last_request.messages[-1].content
    assert "finish_task" in sent
    # memory 的 USER_PROMPT 不含 guidance（仅原始用户输入）
    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await mem.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert user_recs and all("finish_task" not in (r.content or "") for r in user_recs)
