"""Phase 1 集成测试：最小可跑 Loop 端到端。

跑一个：用户输入 "say hello" → mock LLM 回复 "Hello!" → task FINISHED → memory 写入。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ctx_weft.core import CtxWeftRuntime, ProviderRegistry
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.protocols import (
    AgentCapability,
    AgentCapabilityProvider,
    AgentTemplate,
    CapabilityProviderInfo,
    CapabilityRef,
    IdentityFacet,
    LoopConfig,
    MemoryConfig,
    MemoryEventType,
    MemoryAddress,
    ProviderContext,
)
from ctx_weft.providers.memory_blackboard import InMemoryMemoryProvider


class InlineAgentTemplateProvider(AgentCapabilityProvider):
    """测试私有桩：内存 dict 装 template。

    core src 不 ship in-memory provider（spec 方案 B 决策 4）——32 个测试文件的
    fixture 是内联 AgentTemplate（含 LoopConfig 全字段），SOUL.md 表达不了，故测试
    侧保留此桩；真实目录版实现见 ctx_weft.providers.agent_template_local。
    """

    name = "agent"

    def __init__(self) -> None:
        self._templates: dict[str, AgentTemplate] = {}

    def register(self, template: AgentTemplate) -> None:
        self._templates[template.id] = template

    async def list(self, ctx: ProviderContext) -> list:
        return [
            AgentCapability(
                id=f"{self.name}:{t.id}", name=t.id, template_name=t.id,
                description=t.metadata.get("description", ""), version=t.version,
            )
            for t in self._templates.values()
        ]

    async def get_template(self, template_id, version, ctx) -> AgentTemplate | None:
        return self._templates.get(template_id)

    async def describe(self, ctx) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name, capability_count=len(self._templates),
            supports_streaming=False, supports_cancel=False,
        )


def make_runtime(**kwargs) -> CtxWeftRuntime:
    """测试构造入口：把 agent_provider 注册进 registry 后构造 runtime（方案 B：无适配器）。"""
    provider = kwargs.pop("agent_provider")
    providers = kwargs.pop("providers", None) or ProviderRegistry()
    providers.register_capability(provider)
    return CtxWeftRuntime(providers=providers, **kwargs)


def make_echo_template() -> AgentTemplate:
    """构造一个最简的 echo agent template。"""
    return AgentTemplate(
        id="tpl_echo",
        name="echo_agent",
        version="0.1.0",
        identity={
            "act": IdentityFacet(
                text="You are a helpful echo agent. Repeat the user's message politely.",
            ),
            "observe": IdentityFacet(
                text="You evaluate whether the agent has responded appropriately.",
            ),
        },
        description="Phase 1 smoke test agent",
        capability_refs=[],  # Phase 1 不绑定 capability
        memory_config=MemoryConfig(),
        loop_config=LoopConfig(),
    )


@pytest.mark.asyncio
async def test_minimal_echo_loop() -> None:
    """完整跑一个 reason → act → observe → finalize 流程。"""
    # ── Setup ────────────────────────────────────────────────────────────────
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = MockLLMAdapter(
        responses=[
            MockResponse(text="Hello! You said: say hello"),
        ],
    )

    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    # ── Run ──────────────────────────────────────────────────────────────────
    handle, state = await runtime.run_single_task(
        template_id="agent:tpl_echo",
        user_prompt="say hello",
    )

    # ── Assertions ───────────────────────────────────────────────────────────
    assert state.task.status == "FINISHED", f"expected FINISHED, got {state.task.status}"
    assert state.task.user_prompt == "say hello"
    assert state.verdict is not None
    assert state.verdict.task_outcome == "success"
    # root agent 正常结束走规则降级 observer：act_recap 是机械总结，不回显文本
    assert "conversation round" in state.verdict.act_recap

    # transcript 应有 1 个 turn；assistant 文本回显在 transcript（而非 verdict.act_recap）
    assert len(state.transcript) == 1
    assert "Hello" in state.transcript[0].assistant_text

    # task 层应含 USER_PROMPT + LLM_RESPONSE；root 任务无 parent → 不写 agent 层经验
    # （spec/06 §4.2：agent 经验=它派发的子任务，自己执行的任务不写 self 经验）
    memory = runtime.providers.get_memory()
    ctx = ProviderContext(session_id=state.session.id, agent_id=state.agent.id)
    count_user = await memory.count_recent(
        scope=state.scope,
        types=[MemoryEventType.USER_PROMPT],
        ctx=ctx,
    )
    count_dispatch_result = await memory.count_recent(
        scope=state.scope,
        types=[MemoryEventType.TASK_DISPATCH_RESULT],
        ctx=ctx,
    )
    count_llm = await memory.count_recent(
        scope=state.scope,
        types=[MemoryEventType.LLM_RESPONSE],
        ctx=ctx,
    )
    assert count_user == 1, f"expected 1 USER_PROMPT, got {count_user}"
    assert count_dispatch_result == 0, f"root task writes no agent-layer result, got {count_dispatch_result}"
    assert count_llm == 1, f"expected 1 LLM_RESPONSE, got {count_llm}"


@pytest.mark.asyncio
async def test_prompt_structure_matches_miniagents() -> None:
    """验证 Composer 输出结构对齐 miniAgents：
    - Actor system prompt 含 identity（SOUL）
    - Actor messages 只有 1 条 user message
    - 文本包含 '## Current Message' 段（miniAgents 风格）
    """
    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())

    llm = MockLLMAdapter(responses=[MockResponse(text="ack")])
    runtime = make_runtime(llm=llm, agent_provider=resolver)
    runtime.providers.register_memory(InMemoryMemoryProvider())

    _handle, _state = await runtime.run_single_task(
        template_id="agent:tpl_echo",
        user_prompt="hello there",
    )

    # 验证 mock 接到的 request 形态符合 miniAgents 风格
    req = llm.last_request
    assert req is not None
    # System prompt 含 SOUL 文本
    assert "You are a helpful echo agent" in req.system

    # Messages 只有一条 user message（miniAgents 风格：history 序列化进单条文本）
    assert len(req.messages) == 1
    msg = req.messages[0]
    assert msg.role == "user"
    msg_text = msg.content if isinstance(msg.content, str) else ""
    # 含当前消息段
    assert "## Current Message" in msg_text
    assert "hello there" in msg_text
