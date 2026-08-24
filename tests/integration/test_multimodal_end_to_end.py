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
from ctx_weft.core.events import EventType
from ctx_weft.core.runtime import SessionStartParams
from ctx_weft.core.utils import _IMAGE_PART_TOKENS
from ctx_weft.protocols import (
    ImagePart, LLMChunk, LLMUsage, MemoryEventType, ProviderContext, TextPart, ToolCall,
)
from ctx_weft.providers.llm.anthropic import AnthropicAdapter
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


# ── Task 7：兑现 Phase 0 的两条遗留义务 + 端到端出网验证（spec §6.5）───────────

_TEXT_ONLY_PROMPT = [TextPart(text="describe this image")]


class _FinishRouterLLM(MockLLMAdapter):
    """同 _RouterLLM，抽出复用：recognize_intent → 空；act → finish_task 收尾。"""

    def __init__(self, **kw) -> None:
        super().__init__(responses=[], **kw)
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        self.last_request = request
        names = {getattr(t, "name", "") for t in (getattr(request, "tools", None) or [])}
        if "control__update_task_metadata" in names:
            return self._stream(MockResponse(text=""), request)
        return self._stream(
            MockResponse(
                text="ok",
                tool_calls=[ToolCall(
                    id=self._id(), name="control__finish_task",
                    arguments={"deliverables_summary": "d"},
                )],
            ),
            request,
        )


async def _run_and_collect_context_assembled_tokens(user_prompt) -> int:
    """驱动一次完整 start_session 回合，返回 PrepareStep 真实装配（PriorityBudgetStrategy +
    DefaultComposer 全链路）产出的 CONTEXT_ASSEMBLED.token_count（生产事件，非测试自己算的）。"""
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    llm = _FinishRouterLLM()
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    memory = InMemoryMemoryProvider()
    runtime.providers.register_memory(memory)

    handle = await runtime.start_session(
        SessionStartParams.create(
            template_id="agent:tpl_echo",
            user_prompt=user_prompt,
            context_limit=100_000,
        )
    )
    state = await handle.wait_for_finish(timeout=5.0)
    assert state.task.status == "FINISHED"

    events = await runtime.event_store.read_by_session(state.session.id)
    counts = [e.payload["token_count"] for e in events if e.type == EventType.CONTEXT_ASSEMBLED]
    assert counts, "expected at least one CONTEXT_ASSEMBLED event"
    return counts[0]


@pytest.mark.asyncio
async def test_assembled_token_count_higher_with_image_than_text_only() -> None:
    """Phase 0 遗留义务 1（spec §6.5）：composer.py:400 的 image_tokens(m.content) 曾因
    composer 一律拍扁 m.content 恒为 str 而恒返回 0。这里驱动完整装配链
    （start_session → PrepareStep → ContextAssembler.assemble → DefaultComposer.compose），
    从生产事件 CONTEXT_ASSEMBLED 里取真实 token_count，断言含图会话 > 同等文本会话
    （二者共享同一段文本 "describe this image"，唯一变量是多出的 ImagePart）。"""
    text_tokens = await _run_and_collect_context_assembled_tokens(_TEXT_ONLY_PROMPT)
    mm_tokens = await _run_and_collect_context_assembled_tokens(_MULTIMODAL_PROMPT)
    assert mm_tokens == text_tokens + _IMAGE_PART_TOKENS, (
        f"含图会话 token_count({mm_tokens}) 应恰好比同等文本会话({text_tokens}) 多"
        f" _IMAGE_PART_TOKENS({_IMAGE_PART_TOKENS})——否则 image_tokens(m.content) 只是"
        "贡献了非零但错误的常数（此前的 `>` 断言对此不敏感）"
    )


class _WireCapturingAnthropicAdapter(AnthropicAdapter):
    """驱动 Task 5 的真实 wire 转换代码（AnthropicAdapter._build_payload → _serialize_messages）
    捕获实际会发给 Anthropic Messages API 的 payload，但不做真实网络调用——complete() 直接
    从 payload 合成一段 chunk 流，不经 httpx。captured_payloads 是本 Phase 的验收证据：
    图片是否真的进了 wire content blocks，而不是仅仅停留在 AssembledPrompt.messages 里。"""

    def __init__(self, **kw) -> None:
        super().__init__(api_key="test-key", **kw)
        self.captured_payloads: list[dict] = []
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"tc{self._n}"

    def complete(self, request, stream=True):
        payload = self._build_payload(request)
        self.captured_payloads.append(payload)
        names = {t.get("name", "") for t in (payload.get("tools") or [])}
        if "control__update_task_metadata" in names:
            return self._fake_stream(text="", tool_calls=[])
        return self._fake_stream(
            text="ok",
            tool_calls=[ToolCall(
                id=self._id(), name="control__finish_task",
                arguments={"deliverables_summary": "d"},
            )],
        )

    async def _fake_stream(self, *, text: str, tool_calls: list[ToolCall]):
        if text:
            yield LLMChunk(kind="token", text=text)
        for tc in tool_calls:
            yield LLMChunk(kind="tool_call", tool_call=tc)
        yield LLMChunk(kind="usage", usage=LLMUsage(
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
        ))
        yield LLMChunk(kind="done", finish_reason="tool_use" if tool_calls else "stop")


def _has_image_block(payload: dict) -> bool:
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image":
                    return True
    return False


@pytest.mark.asyncio
async def test_multimodal_prompt_reaches_wire_payload_as_image_block() -> None:
    """本 Phase 的真正验收（spec §6.5）：start_session(user_prompt=[TextPart, ImagePart])
    走完一个 actor 回合后，
      1) 不抛异常；
      2) memory 里的 USER_PROMPT 记录仍是 part 列表；
      3) 送到 LLM adapter 的 wire payload（真实 AnthropicAdapter._build_payload 产出）里
         含 image block ——图片真的出网了，不是止步于 AssembledPrompt.messages。
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = _WireCapturingAnthropicAdapter()
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
    state = await handle.wait_for_finish(timeout=5.0)  # 1) 不抛异常

    assert state is not None
    assert state.task.status == "FINISHED"

    # 2) memory 里的 USER_PROMPT 记录仍是 part 列表
    ctxp = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    user_recs = await memory.recall_recent(
        state.scope, [MemoryEventType.USER_PROMPT], 10, ctxp,
    )
    assert len(user_recs) == 1
    assert user_recs[0].content == _MULTIMODAL_PROMPT

    # 3) 送到 LLM adapter 的 wire payload 里含 image block
    assert llm.captured_payloads, "expected at least one captured wire payload"
    assert any(_has_image_block(p) for p in llm.captured_payloads), (
        "图片没有出现在任何一次 wire payload 的 image block 里——"
        "AssembledPrompt.messages 里可能有 ImagePart，但没有真的出网"
    )
