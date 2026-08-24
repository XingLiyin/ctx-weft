"""端到端：多模态 user_prompt 走完至少一个 actor 回合（评审 I3，spec 2026-08-23）。

C1 的复现条件不是理论上的：``start_session`` 派生的 root task 用 ``title=""``
（``session_manager._make_root_task_manager``），使 ``act_guidance._task_label``
必然从 ``title`` 分支落到 ``user_prompt`` 分支。任何多模态 ``user_prompt``（``list[ContentPart]``）
若不经 ``content_to_text`` 拍扁就直接 ``.strip()``，第一个 act 回合就会 ``AttributeError``。

用 ``run_single_task`` 复现不了这条——它显式给 root task 设了 ``title="User Request"``，
title 分支短路，永远走不到 user_prompt 分支。必须走 ``start_session``。
"""

from __future__ import annotations

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.protocols import ImagePart, MemoryEventType, ProviderContext, TextPart, ToolCall
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
    make_runtime,
)

_MULTIMODAL_PROMPT = [
    TextPart(text="describe this image"),
    ImagePart(data="ZmFrZWJhc2U2NGRhdGE=", media_type="image/png"),
]


class _RouterLLM(MockLLMAdapter):
    """按 request.tools 路由：recognize_intent → 空；act → finish_task 收尾。

    root task 无 parent → ObserveStep 走规则降级，不需要路由 report_task_outcome。
    """

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:  # recognize_intent
            return self._stream(MockResponse(text=""), request)
        # act：finish_task 收尾（interaction_mode=interactive 的 root task 只能靠工具调用收尾，
        # 纯文本只会 park 等用户 —— 这里要让它真正跑完一个回合并终结，故显式 finish）。
        return self._stream(
            MockResponse(
                text="This looks like a fake image.",
                tool_calls=[ToolCall(
                    id=self._id(), name="control__finish_task",
                    arguments={"deliverables_summary": "described"},
                )],
            ),
            request,
        )


@pytest.mark.asyncio
async def test_multimodal_prompt_completes_one_actor_round_without_crashing() -> None:
    """start_session(user_prompt=[TextPart, ImagePart]) 走完 act 回合，不抛异常，
    memory 里的 USER_PROMPT 仍是原样的 part 列表（无损）。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _RouterLLM()
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=_MULTIMODAL_PROMPT,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)

    assert state is not None
    assert state.task.status == "FINISHED", f"expected FINISHED, got {state.task.status}"
    assert state.task.user_prompt == _MULTIMODAL_PROMPT

    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await memory.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert len(user_recs) == 1
    assert user_recs[0].content == _MULTIMODAL_PROMPT


@pytest.mark.asyncio
async def test_task_label_does_not_crash_on_multimodal_user_prompt() -> None:
    """退而求其次的直接单元覆盖（防端到端 fixture 未来漂移时这条根因仍被盯住）：
    _task_label 对 title="" 的多模态-prompt task 不抛 AttributeError。"""
    from ctx_weft.core.loop.steps.act_guidance import _task_label
    from ctx_weft.core.state.models import Task

    task = Task(
        id="t1", session_id="s1", status="ACTIVE",
        title="", description="",
        user_prompt=_MULTIMODAL_PROMPT,
    )
    label = _task_label(task)
    assert label  # 不抛；取到 user_prompt 首个 TextPart 的文本
    assert "describe this image" in label
