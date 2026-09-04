"""Context Assembler：装配流水线核心。

ContextRequest 是装配入口；ContextBlock 是中间表示；AssembledPrompt 是最终输出。
ContextSource 是数据获取接口。

═══ 流水线 ═══

  sources（并发 fetch，每个产 0..N 个 block）
    IdentitySource        identity + directive
    CapabilitySource      capabilities（tools/skills/agents 三路）
    TaskSpecSource        task_spec（metadata 载体）
    AgentRecallSource     history（task body + agent 层回合）
    BlackboardSource      blackboard + background
    SemanticRecallSource  summary
    KnowledgeRetrieval…   reference
    GuidanceSource        guidance（act 态势，extra["act_guidance"]）
          │
          ▼  ContextBlock[]（kind / target / priority / token_estimate）
    BudgetStrategy.apply  超限时按保护阶梯裁剪（阶梯见 priority.py，
          │               动态覆盖与配对丢弃见 budget.py）
          ▼  kept blocks
    Composer.compose      按 request.purpose 渲染（槽位布局见 composer.py）
          │
          ▼
    AssembledPrompt（system / messages / tools / token_count）

详见设计文档 §5.2。
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from ctx_weft.protocols import (
    AgentTemplate,
    LLMMessage,
    LLMTool,
    LoopConfig,
    MemoryProvider,
    MemoryAddress,
    ProviderContext,
    Purpose,
)
from ctx_weft.core.utils import effective_limit, estimate_tokens
from ctx_weft.protocols.knowledge import KnowledgeProvider

if TYPE_CHECKING:
    from ctx_weft.core.domain.models import Agent, Session, Task


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
    scope: MemoryAddress
    task: "Task"
    agent: "Agent"
    session: "Session"
    template: AgentTemplate | None  # 实例化时绑定的 template（pin 了 version）；None 时 IdentitySource 跳过
    bound_capabilities: list[Any]  # list[Capability]，但避免循环导入
    # token 计数回调：生产由 step 构造时传 ctx.llm.tokenizer.count（已校准、随当次模型）；
    # 默认回退未校准启发式（测试/无 llm 场景）。sources/composer 统一经它计数。
    token_counter: Callable[[str], int] = estimate_tokens
    extra: dict[str, Any] = field(default_factory=dict)


# ── ContextBlock ──────────────────────────────────────────────────────────────


BlockKind = Literal[
    # ── 渲染进 system ──
    "identity",  # SOUL / facet 正文（IdentitySource）
    "background",  # ## Project Background（BlackboardSource long_term_background topic）
    # ── 注入段：composer 拼进 user 回合（不进 system，也不是独立消息）──
    "capabilities",  # ## Capabilities → 当前 task 的 user 回合尾部，末条留一行指针（CapabilitySource）
    "directive",  # ## Instructions for the current task → 当前 task 的 user 回合（skill_instructions）
    "guidance",  # act 运行时态势 guidance → 末条 user 最尾部（GuidanceSource）
    # ── 渲染进 messages ──
    "history",  # 多轮对话无损重建（AgentRecallSource）
    "blackboard",  # 相关任务 topic 通信 / 长期 project_log（BlackboardSource）
    "summary",  # 语义召回结果（SemanticRecallSource）
    "reference",  # 知识检索结果（KnowledgeRetrievalSource）
    "task_spec",  # 当前 task spec 的 metadata 载体（TaskSpecSource）——composer 读其
    #              metadata 去装饰当前 user 回合（## Current Task/Message 框），不独立渲染
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
        token_limit = effective_limit(
            request.session.context_limit, request.session.reserved_output_tokens
        )
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
