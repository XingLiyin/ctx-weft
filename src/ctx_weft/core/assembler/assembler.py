"""Context Assembler：装配流水线核心。

ContextRequest 是装配入口；ContextBlock 是中间表示；AssembledPrompt 是最终输出。
ContextSource 是数据获取接口。

详见设计文档 §5.2。
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from ctx_weft.protocols import (
    AgentTemplate,
    LLMMessage,
    LLMTool,
    LoopConfig,
    MemoryProvider,
    MemoryScope,
    ProviderContext,
    Purpose,
)
from ctx_weft.protocols.knowledge import KnowledgeProvider

if TYPE_CHECKING:
    from ctx_weft.core.state.models import Agent, Session, Task


# ── ContextRequest ────────────────────────────────────────────────────────────


@dataclass
class ContextRequest:
    """装配请求。

    purpose 标识装配的 prompt 给哪个 LLM 调用阶段消费：
      - "act"     → PrepareStep 装配给下一个 ActStep 的 LLM 用
      - "observe" → ObserveStep 装配给自己的 LLM 用
      - "compact" → CompactStep 装配给摘要 LLM 用
    """

    purpose: Purpose
    scope: MemoryScope
    task: "Task"
    agent: "Agent"
    session: "Session"
    template: AgentTemplate | None  # 实例化时绑定的 template（pin 了 version）；None 时 IdentitySource 跳过
    bound_capabilities: list[Any]  # list[Capability]，但避免循环导入
    extra: dict[str, Any] = field(default_factory=dict)
    # ObserveStep 装配时需要本轮 Actor 的执行 transcript
    actor_transcript: list[Any] | None = None  # list[ConversationTurn]


# ── ContextBlock ──────────────────────────────────────────────────────────────


BlockKind = Literal[
    # → system prompt
    "identity",
    "background",  # 项目背景（订阅 long_term_background topic 的内容）
    "capabilities",
    "directive",  # skill_instructions
    # → messages
    "history",  # short-term recent messages
    "blackboard",  # subtask / predecessor topic / 长期 project_log
    "summary",  # semantic recall 结果
    "reference",  # knowledge retrieval 结果
    "transcript",  # observe 用：actor transcript
    "task_spec",  # 当前 task 描述
]

BlockTarget = Literal["system", "messages"]


@dataclass
class ContextBlock:
    """装配阶段的中间块。每个 Source 产出 0..N 个 block。"""

    id: str
    source: str  # 来源标识（"capability:identity" / "memory:blackboard:bg" 等）
    kind: BlockKind
    target: BlockTarget
    content: str | list[Any]  # 文本或 ContentPart 列表
    priority: int  # 0 最高，budget 不够时高 priority 优先保留
    token_estimate: int
    metadata: dict[str, Any] = field(default_factory=dict)


# ── AssembledPrompt ───────────────────────────────────────────────────────────


@dataclass
class AssembledPrompt:
    """Composer 输出：装配好的最终 prompt。"""

    system: str
    messages: list[LLMMessage]
    tools: list[LLMTool]
    token_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Source protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class ContextSource(Protocol):
    """装配源——把 provider 数据转换为 ContextBlock。"""

    name: str

    @abstractmethod
    def fetch(
        self,
        request: ContextRequest,
        deps: "AssemblerDeps",
    ) -> AsyncIterator[ContextBlock]: ...


# ── AssemblerDeps ─────────────────────────────────────────────────────────────


@dataclass
class AssemblerDeps:
    """注入给 Source / Composer / Budget 的依赖集合。"""

    memory: MemoryProvider
    knowledge_providers: list[KnowledgeProvider]
    provider_ctx: ProviderContext
    skill_provider_index: dict[str, Any] = field(default_factory=dict)  # provider_name → SkillCapabilityProvider
    # provider_name → CapabilityProvider（持有 live 对象，fetch 时读取 .description；
    # MCP 在 connect 后才有 description，故须 live 读取而非装配时快照）
    capability_provider_index: dict[str, Any] = field(default_factory=dict)


# ── ContextAssembler ───────────────────────────────────────────────────────────


@dataclass
class ContextAssembler:
    """装配流水线主控。"""

    sources: list[ContextSource]
    budget: "BudgetStrategy"
    composer: "Composer"
    deps: AssemblerDeps

    async def assemble(self, request: ContextRequest) -> AssembledPrompt:
        """1) 并发触发所有 sources；2) budget；3) compose。"""
        all_blocks: list[ContextBlock] = []
        async with asyncio.TaskGroup() as tg:
            futs = [tg.create_task(self._collect(s, request)) for s in self.sources]
        for f in futs:
            all_blocks.extend(f.result())

        # budget
        token_limit = request.session.context_limit
        kept = await self.budget.apply(all_blocks, token_limit, request)

        # compose
        prompt = await self.composer.compose(kept, request)
        return prompt

    async def _collect(self, source: ContextSource, request: ContextRequest) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        async for blk in source.fetch(request, self.deps):
            blocks.append(blk)
        return blocks




# Forward references to avoid circular imports at module load
from ctx_weft.core.assembler.budget import BudgetStrategy  # noqa: E402
from ctx_weft.core.assembler.composer import Composer  # noqa: E402
