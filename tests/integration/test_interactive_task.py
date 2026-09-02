"""interactive vs auto task + finish_task + 临时 guidance 注入。

- interactive 任务 actor 纯文本 → HITL input 冷 park（等用户），不产出、不完成。
- auto 任务 actor 纯文本 → 文本即 outputs，路由 observe。
- finish_task 收尾标记 → 答复正文即 outputs + 完成（端到端经 gateway/observe/finalize）。
- 运行时 guidance 现由装配管线注入（PrepareStep → extra["act_guidance"] →
  GuidanceSource → composer 末条 user 尾部）；本文件 fixture 按该形态预拼进
  assembled prompt，验证 ActStep 原样发送、guidance 不入 memory。
"""

from __future__ import annotations

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.assembler.assembler import AssembledPrompt
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.loop.steps.act import ActStep
from ctx_weft.core.loop.steps.act_guidance import build_act_guidance
from ctx_weft.core.state.models import Agent, NormalTaskSettings, Session, Task
from ctx_weft.protocols import (
    LLMMessage, MemoryEventType, MemoryAddress, ProviderContext, ToolCall,
)
from ctx_weft.protocols.hitl import PREFACE_NORMAL, UserTurnDelivery
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.hitl_env import make_hitl
from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_echo_template, make_runtime

pytestmark = pytest.mark.asyncio


def _act_state_ctx(interaction_mode: str, llm: MockLLMAdapter):
    """返回 (state, ctx, task, registry, mem)。第 4 项是 `HitlRegistry`——`list_pending`
    的持有者从 `HitlManager` 换成了它，断言口径不变（仍是「这个 session 上挂着哪些未决
    请求」）。"""
    bus = InProcessEventBus()
    mem = InMemoryMemoryProvider()
    hitl_service, hitl = make_hitl(bus)
    session = Session(id="s1", tenant_id="default", user_prompt="hi", status="RUNNING", token_budget=0)
    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        title="Greet", description="say hi politely",
        interaction_mode=interaction_mode, settings=NormalTaskSettings(),
    )
    agent = Agent(id="ag1", session_id="s1", template_id="t", template_version="1", status="RUNNING")
    scope = MemoryAddress(session_id="s1", task_id="t1", agent_id="ag1")
    # composer 形态：guidance 已拼在末条 user 尾部（ActStep 不再自行注入）。
    guidance = build_act_guidance(task, None)
    prompt = AssembledPrompt(
        system="", messages=[LLMMessage(role="user", content=f"hi\n\n{guidance}")],
        tools=[], token_count=1,
    )
    state = LoopState(
        run_id="r1", session=session, task=task, agent=agent, scope=scope, assembled_prompt=prompt,
    )
    ctx = LoopContext(
        assembler=None, llm=llm, memory=mem, event_bus=bus,
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default", task_id="t1", agent_id="ag1"),
        hitl=hitl_service,
    )
    return state, ctx, task, hitl, mem


async def test_interactive_plain_text_parks_for_user() -> None:
    llm = MockLLMAdapter(responses=[MockResponse(text="Hi! Anything else?")])
    state, ctx, task, hitl, mem = _act_state_ctx("interactive", llm)

    with pytest.raises(HitlPark):
        await ActStep().execute(state, ctx)

    # park 不写 task 状态（Task 4）：AWAITING_HUMAN 由 TaskManager 据 RunOutcome 落。
    assert task.status == "ACTIVE"
    assert task.outputs is None                       # 纯文本不是产出
    pend = hitl.list_pending("s1")
    assert len(pend) == 1
    assert pend[0].form == "wait"
    # 续跑方式由 delivery 显式声明——取代旧的 `capability_id` sentinel（spec §5）。
    assert pend[0].delivery == UserTurnDelivery(task_id="t1", preface=PREFACE_NORMAL)

    # guidance 只在发送的 prompt（装配期已拼入），不入 memory
    sent = llm.last_request.messages[-1].content
    assert "final reply to the user" in sent and "finish_task" in sent
    recs = await mem.recall_recent(state.scope, [MemoryEventType.LLM_RESPONSE], 10, ctx.provider_ctx)
    assert recs and all("final reply to the user" not in (r.content or "") for r in recs)


async def test_auto_plain_text_completes() -> None:
    llm = MockLLMAdapter(responses=[MockResponse(text="Hi! Anything else?")])
    state, ctx, task, hitl, _mem = _act_state_ctx("auto", llm)

    outcome = await ActStep().execute(state, ctx)

    assert outcome.next_step == "observe"
    assert task.status != "SUSPENDED"
    assert task.outputs == "Hi! Anything else?"        # auto: 文本即产出（旧行为）
    assert hitl.list_pending("s1") == []


async def test_finish_task_finishes_task_end_to_end() -> None:
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    # 反转契约：答复写在消息正文，finish_task 无参收尾标记 → outputs = 正文
    llm = MockLLMAdapter(responses=[
        MockResponse(text="computed: 42", tool_calls=[ToolCall(
            id="tc1", name="control__finish_task", arguments={},
        )]),
    ])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    _handle, state = await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="compute")

    assert state.task.status == "FINISHED"
    assert state.task.outputs == "computed: 42"


async def test_guidance_injected_into_prompt_not_memory() -> None:
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = MockLLMAdapter(responses=[
        MockResponse(text="ok", tool_calls=[ToolCall(
            id="tc1", name="control__finish_task", arguments={},
        )]),
    ])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    mem = InMemoryMemoryProvider()
    runtime.providers.register_memory(mem)

    _handle, state = await runtime.run_single_task(template_id="agent:tpl_echo", user_prompt="hello")

    # 发送的 prompt 含 guidance
    sent = llm.last_request.messages[-1].content
    assert "finish_task" in sent
    # memory 的 USER_PROMPT 不含 guidance（仅原始用户输入）
    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await mem.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert user_recs and all("finish_task" not in (r.content or "") for r in user_recs)
