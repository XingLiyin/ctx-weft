"""Collect raw context and compile it through a purpose Jinja template."""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, runtime_checkable

from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols import (
    AgentTemplate,
    LLMMessage,
    LLMTool,
    MemoryAddress,
    MemoryProvider,
    ProviderContext,
    Purpose,
)
from ctx_weft.protocols.knowledge import KnowledgeProvider

if TYPE_CHECKING:
    from ctx_weft.core.assembler.context import PromptContext
    from ctx_weft.core.capabilities.cache import CapabilityCache
    from ctx_weft.core.models.agent import Agent
    from ctx_weft.core.models.session import Session
    from ctx_weft.core.models.task import Task
    from ctx_weft.core.orchestrator.task.manager import TaskManager


# ── Purpose parameters ───────────────────────────────────────────────────────

CompactScope: TypeAlias = Literal["task", "agent"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActIntent:
    """Execute the current task."""


@dataclass(frozen=True)
class ReviewIntent:
    """Observe the latest segment; judgement is optional."""

    judge_task: bool = True
    terminal_only: bool = False

    def __post_init__(self) -> None:
        if self.terminal_only and not self.judge_task:
            raise ValueError("terminal_only requires judge_task")


@dataclass(frozen=True)
class CompactIntent:
    """Compact task or agent memory."""

    scope: CompactScope = "task"

    def __post_init__(self) -> None:
        if self.scope not in ("task", "agent"):
            raise ValueError(f"unsupported compact scope: {self.scope!r}")


@dataclass(frozen=True)
class MetadataIntent:
    """Recognize and fill task metadata."""


PromptIntent: TypeAlias = ActIntent | ReviewIntent | CompactIntent | MetadataIntent


# ── ContextRequest ────────────────────────────────────────────────────────────


@dataclass
class ContextRequest:
    """装配请求。

    purpose selects both the prompt and the capability surface. ReviewIntent
    controls optional task judgement; CompactIntent controls compression scope.
    Foreground/background execution does not change the purpose.
    """

    purpose: Purpose
    scope: MemoryAddress
    task: Task
    agent: Agent
    session: Session
    template: AgentTemplate | None
    bound_capabilities: list[Any]
    token_counter: Callable[[str], int] = estimate_tokens
    extra: dict[str, Any] = field(default_factory=dict)
    # Optional compilation options; their purpose must match the request.
    intent: PromptIntent | None = None

    def __post_init__(self) -> None:
        expected = {
            "act": ActIntent,
            "observe": ReviewIntent,
            "compact": CompactIntent,
            "recognize_intent": MetadataIntent,
        }
        intent_type = expected.get(self.purpose)
        if intent_type is None:
            raise ValueError(f"unsupported prompt purpose: {self.purpose!r}")
        if self.intent is not None and not isinstance(self.intent, intent_type):
            raise ValueError(
                f"prompt intent {type(self.intent).__name__} does not match purpose "
                f"{self.purpose!r}"
            )


# ── ContextBlock ──────────────────────────────────────────────────────────────


BlockKind = Literal[
    "identity",
    "background",
    "capabilities",
    "directive",
    "guidance",
    "history",
    "blackboard",
    "summary",
    "reference",
    "task_spec",
]


@dataclass
class ContextBlock:
    """Source 的统一输出信封。

    Source 只声明来源、种类和原始值。落位、文字装饰、required 与裁剪 priority
    全部属于 purpose Jinja；Source 不生成 prompt 投影。
    """

    id: str
    source: str  # 来源标识（"capability:identity" / "memory:blackboard:bg" 等）
    kind: BlockKind
    value: Any
    metadata: dict[str, Any] = field(default_factory=dict)


# ── AssembledPrompt ───────────────────────────────────────────────────────────


class AssembledPrompt:
    """Compiled prompt whose tool surface may follow the live capability cache.

    Text and the fallback tool list are the exact snapshot compiled by Jinja and
    PromptBuilder.  Act reuses this object for several LLM turns, though, so a
    resolver is needed for capabilities pinned after assembly.
    """

    def __init__(
        self,
        system: str,
        messages: list[LLMMessage],
        tools: list[LLMTool],
        token_count: int,
        metadata: dict[str, Any] | None = None,
        tools_fn: Callable[[], list[LLMTool]] | None = None,
    ) -> None:
        self.system = system
        self.messages = messages
        self._tools = tools
        self.token_count = token_count
        self.metadata = metadata if metadata is not None else {}
        self.tools_fn = tools_fn
        self.input_snapshot: dict[str, Any] | None = None

    @property
    def tools(self) -> list[LLMTool]:
        if self.tools_fn is None:
            return self._tools
        try:
            return self.tools_fn()
        except Exception:
            logger.warning(
                "live tool resolution failed; using the assembled snapshot",
                exc_info=True,
            )
            return self._tools

    @tools.setter
    def tools(self, value: list[LLMTool]) -> None:
        self._tools = value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AssembledPrompt):
            return NotImplemented
        return (
            self.system == other.system
            and self.messages == other.messages
            and self.tools == other.tools
            and self.token_count == other.token_count
            and self.metadata == other.metadata
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return (
            f"AssembledPrompt(system={self.system!r}, messages={self.messages!r}, "
            f"tools={self.tools!r}, token_count={self.token_count!r}, "
            f"metadata={self.metadata!r})"
        )


# ── Source protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class ContextSource(Protocol):
    """Fetch provider data and wrap each raw object in a ContextBlock."""

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
    """Dependencies available while Sources collect raw context."""

    memory: MemoryProvider
    knowledge_providers: list[KnowledgeProvider]
    provider_ctx: ProviderContext
    capability_cache: "CapabilityCache | None" = None
    task_manager: "TaskManager | None" = None
    # provider_name → CapabilityProvider（持有 live 对象，fetch 时读取 .description；
    # MCP 在 connect 后才有 description，故须 live 读取而非装配时快照）
    capability_provider_index: dict[str, Any] = field(default_factory=dict)


# ── ContextAssembler ───────────────────────────────────────────────────────────


@dataclass
class ContextAssembler:
    """装配流水线主控。"""

    sources: list[ContextSource]
    deps: AssemblerDeps

    async def assemble(self, request: ContextRequest) -> AssembledPrompt:
        """Collect raw sources, then compile once through purpose Jinja."""
        from ctx_weft.core.assembler.builder import PromptBuilder

        context = await self.collect(request)
        from ctx_weft.core.assembler.snapshot import capture_prompt, recording_counter

        token_counts: dict[str, int] = {}
        prompt = PromptBuilder(recording_counter(request.token_counter, token_counts)).compile(
            context, tools=self._tools_snapshot(context))
        prompt.input_snapshot = capture_prompt(context)
        prompt.input_snapshot["token_counts"] = token_counts
        self._install_live_tools(prompt, request)
        return prompt

    async def collect(self, request: ContextRequest) -> PromptContext:
        """Collect raw source blocks without applying prompt policy or budget."""
        from ctx_weft.core.assembler.context import PromptContext

        all_blocks: list[ContextBlock] = []
        async with asyncio.TaskGroup() as tg:
            futs = [tg.create_task(self._collect(s, request)) for s in self.sources]
        for f in futs:
            all_blocks.extend(f.result())
        return PromptContext(request=request, blocks=tuple(all_blocks))

    @staticmethod
    def _tools_snapshot(context: PromptContext) -> list[LLMTool]:
        """Build tools from the same raw capability blocks seen by Jinja."""
        if context.request.purpose == "compact":
            return []
        from ctx_weft.core.assembler.sources.capability import build_llm_tools

        capabilities = [block.value for block in context.get("capabilities")]
        return build_llm_tools(capabilities, context.request.purpose,
                               terminal_only=bool(getattr(context.request.intent, "terminal_only", False)))

    def _install_live_tools(
        self,
        prompt: AssembledPrompt,
        request: ContextRequest,
    ) -> None:
        """Keep runtime pins visible during a multi-turn act loop."""
        cache = self.deps.capability_cache
        if cache is None or request.purpose == "compact":
            return

        from ctx_weft.core.assembler.sources.capability import build_llm_tools

        agent_id = getattr(request.agent, "id", "") or request.scope.agent_id
        task_id = getattr(request.task, "id", "") or request.scope.task_id
        purpose = request.purpose
        prompt.tools_fn = lambda: build_llm_tools(
            cache.available(agent_id, task_id), purpose,
            terminal_only=bool(getattr(request.intent, "terminal_only", False)))

    async def _collect(self, source: ContextSource, request: ContextRequest) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        async for blk in source.fetch(request, self.deps):
            blocks.append(blk)
        return blocks
