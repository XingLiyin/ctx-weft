"""LLM 接入协议层。

定义 core 与 host 之间关于 LLM 的硬契约（第 5 个接入协议，与 Memory / Capability /
Knowledge / Template 并列）。包含两个**相互独立**的扩展点：

[Adapter 契约] 实现一个新 LLM adapter 所需的全部类型：
  - LLMClient     — 要实现的协议本身（host 实现，core 只依赖此抽象）
  - LLMRequest    — complete() 入参（读 model / system / messages / tools）
  - LLMMessage    — request.messages 元素
  - LLMTool       — request.tools 元素
  - LLMChunk      — complete() 必须 yield 的流式产物
  - ToolCall      — chunk.kind == "tool_call" 的 payload
  - LLMUsage      — chunk.kind == "usage" 的 payload
  - LLMCallError  — 错误/重试契约（TaskManager 读 .retriable 决定是否重试）
  多模态 adapter 还会用到 protocols.context.ContentPart（LLMMessage.content 的元素）。

[Resolver 契约] 仅"多账号 LLM provider"需要，与写单个 adapter 无关：
  - LLMClientResolver — 注册进 ProviderRegistry.register_llm_provider

零运行时依赖（仅依赖 protocols.context）。详见设计文档 §11。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ctx_weft.protocols.context import ContentPart


# ══════════════════════════════════════════════════════════════════════════════
# Adapter 契约：数据类型（adapter 读 LLMRequest/Message/Tool，产 LLMChunk/ToolCall/Usage）
# ══════════════════════════════════════════════════════════════════════════════


# ── Errors ────────────────────────────────────────────────────────────────────


class LLMCallError(RuntimeError):
    """LLM API 调用错误，携带 HTTP 状态码与分类标志。

    三类故障各走不同路径（retriable × outage 组合）：

    retriable=False              永久错（401 认证 / 400 参数 / 404 模型 / transport 重试耗尽）
                                 → 立即 FAILED，重试无意义。
    retriable=True, outage=True  基础设施类瞬时故障（429 限流 / 5xx / 网络 / transport 断流）
                                 → 由自愈层（stream_llm_resilient）进程内退避等待 LLM 恢复；
                                 预算耗尽则 _run_loop 走 INTERRUPTED（可 /resume）。**不**经
                                 TaskManager 重试、**不**增 failure_counter——LLM 宕机不是任务失败。
    retriable=True, outage=False 内容类可重试错（_finalize 截断 / 半截 tool call / 空响应）：
                                 LLM 在线但本轮输出退化 → 由 TaskManager 有界重试该任务（重跑一轮
                                 通常即可），不当作基础设施中断。outage 标志由 adapter 在抛错点按
                                 来源设置（只有 adapter 能区分"断流"与"内容截断"），核心层只读不猜。
    retry_after_sec              provider 给了 Retry-After 时，自愈退避优先采用。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        retriable: bool = True,
        outage: bool = False,
        retry_after_sec: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.outage = outage
        self.retry_after_sec = retry_after_sec


class LLMOutageError(LLMCallError):
    """瞬时 LLM 基础设施故障，应按可恢复中断（INTERRUPTED）处理，**不是** task 失败。

    自愈层在"预算耗尽"或"已吐 chunk 后中途断流"时抛此异常；_run_loop 用
    ``except LLMOutageError`` 干净分流到 INTERRUPTED 路径。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        retry_after_sec: float | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            retriable=True,
            outage=True,
            retry_after_sec=retry_after_sec,
        )


# ── Message ───────────────────────────────────────────────────────────────────


@dataclass
class LLMMessage:
    """统一 LLM 对话消息。"""

    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[ContentPart]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    reasoning_content: str | None = None  # 部分模型支持的"内部推理"内容


# ── Tool ──────────────────────────────────────────────────────────────────────


@dataclass
class LLMTool:
    """传给 LLM 的工具定义。"""

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema

    def to_prompt_text(self) -> str:
        """渲染为 system prompt 可读文本（miniAgents 风格）。"""
        return f"{self.name}: {self.description}"


# ── Tool call（LLM 返回）───────────────────────────────────────────────────────


# adapter 在「native 工具参数没解析成 JSON」时的兜底哨兵：把原始未解析文本原样塞进此键
# （见 providers/llm 的 _parse_buffers/_parse_tool_blocks），不静默丢、交 gateway 报错。
# finalize 会尝试解包（unwrap_raw_arguments）；gateway 见到残留的它则给直白报错。
# 注意别把这个 key 回灌进模型上下文——否则模型会误当参数名照抄，陷入 doom loop。
RAW_ARGS_KEY = "_raw"


@dataclass
class ToolCall:
    """LLM 返回的一次工具调用。"""

    id: str  # LLM 分配的 call id（OpenAI/Anthropic 都有）
    name: str
    arguments: dict[str, Any]


# ── Usage ─────────────────────────────────────────────────────────────────────


@dataclass
class LLMUsage:
    """token 使用统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ── Request / 流式 chunk ──────────────────────────────────────────────────────


@dataclass
class LLMRequest:
    """统一 LLM 请求。"""

    model: str
    system: str
    messages: list[LLMMessage]
    tools: list[LLMTool] = field(default_factory=list)
    max_tokens: int | None = None
    temperature: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)  # trace_id / run_id / 计费标签


@dataclass
class LLMChunk:
    """流式 chunk。adapter 的 complete() 始终 yield 此类型（stream=False 时也聚合成序列）。"""

    kind: Literal["token", "reasoning", "tool_call", "tool_call_partial", "usage", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: LLMUsage | None = None
    finish_reason: str | None = None


# ══════════════════════════════════════════════════════════════════════════════
# Adapter 契约：协议本身
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class LLMClient(Protocol):
    """LLM 调用统一门面。adapter 实现这个协议；core 只依赖此抽象。"""

    @property
    @abstractmethod
    def context_limit(self) -> int:
        """LLM 的硬上下文上限。"""
        ...

    @property
    @abstractmethod
    def max_output_tokens(self) -> int:
        """LLM 单次输出 token 上限。"""
        ...

    # 可选（duck-typed，非协议必需）：output_ceiling -> int | None
    #   单次输出的收紧上限。网关经 getattr 读取，缺省/None → 回退 context_limit。
    #   实现方（_FixedModelClient）可提供；未提供者网关自动回退。

    @property
    @abstractmethod
    def supports_tool_calling(self) -> bool:
        """是否支持 tool calling。"""
        ...

    @abstractmethod
    def complete(
        self,
        request: LLMRequest,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        """流式 LLM 调用——返回 chunk iterator。

        当 stream=False 时，adapter 内部也应聚合为单个 chunk 序列。
        """
        ...

    @abstractmethod
    async def count_tokens(self, text: str) -> int:
        """估算文本的 token 数。"""
        ...


# ══════════════════════════════════════════════════════════════════════════════
# Resolver 契约：仅多账号 LLM provider 实现（与写单个 adapter 无关）
# ══════════════════════════════════════════════════════════════════════════════


@runtime_checkable
class LLMClientResolver(Protocol):
    """Protocol: resolve an LLMClient by account name + model name.

    Registered into ProviderRegistry as the multi-account LLM provider.
    """

    def get_client(
        self,
        account: str | None = None,
        model: str | None = None,
    ) -> LLMClient:
        ...
