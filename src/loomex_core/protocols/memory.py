"""MemoryProvider 协议（统一）。

定位：LoomeX-00 中唯一的 memory 抽象。承载所有"短期 / 长期 / Blackboard"职能——
它们在 LoomeX-00 中是**同一概念**。

核心思想：Provider 是一个**事件摄取 + 多模召回**的黑盒：
- core 把所有重要事件通过 ingest() 喂给 provider
- Provider 自由决定如何内化
- core 通过三种 recall 接口取回：recall_recent / recall_topic / recall_semantic

详见设计文档 §4.3。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from loomex_core.protocols.context import ContentPart, ProviderContext


# ── Event types ───────────────────────────────────────────────────────────────


class MemoryEventType(StrEnum):
    """Memory 事件类型——core 通过 ingest() 喂给 provider 的事件分类。

    继承 StrEnum 以保持 JSON / 字符串语义。

    分层见 spec/06 §3：每个类型由 EVENT_LAYER 唯一映射到一层（task / agent / session）。
    """

    # ── task 层（一次 task 的私有执行对话）──
    USER_PROMPT = "user_prompt"            # 用户任务输入；retry 时也追加于此
    LLM_RESPONSE = "llm_response"          # actor LLM 一轮的完整输出（metadata.tool_calls 供无损重建）
    TOOL_INVOCATION = "tool_invocation"    # actor 一次「真实能力」工具调用（仅审计）
    TOOL_RESULT = "tool_result"            # 「真实能力」工具返回值
    TASK_COMPACT_SUMMARY = "task_compact_summary"    # task compact（observe active）产出的 [Context so far]

    # ── agent 层（该 agent 的派发日志，跨 task 持久）──
    TASK_DISPATCH = "task_dispatch"               # delegate_task/delegate_plan 调用（result 暂挂）
    TASK_DISPATCH_RESULT = "task_dispatch_result"  # child 回填的 output+process_report
    AGENT_COMPACT_SUMMARY = "agent_compact_summary"  # agent compact 产出的 [既往派发摘要]

    # ── session / topic ──
    BLACKBOARD_PUBLISH = "blackboard_publish"  # 显式 topic 发布（见 spec/04）

    # ── 过渡期保留（spec/06 落地后移除；EVENT_LAYER 仍映射，旧调用点未迁移前可用）──
    OBSERVER_SUMMARY = "observer_summary"  # 旧 verdict.summary 通道 → 被 TASK_DISPATCH_RESULT 取代
    COMPACT_SUMMARY = "compact_summary"    # 旧 compact 通道 → 拆为 TASK/AGENT_COMPACT_SUMMARY


class MemoryLayer(StrEnum):
    """Memory 分层（spec/06 §2）。scope key 与 seq 计数按层分区。"""

    TASK = "task"        # tenant|session|task|<task_id>
    AGENT = "agent"      # tenant|session|agent|<agent_id>
    SESSION = "session"  # tenant|session


# 事件类型 → 层（spec/06 §3）。唯一映射，ingest/recall 据此选 scope key。
EVENT_LAYER: dict[MemoryEventType, MemoryLayer] = {
    MemoryEventType.USER_PROMPT: MemoryLayer.TASK,
    MemoryEventType.LLM_RESPONSE: MemoryLayer.TASK,
    MemoryEventType.TOOL_INVOCATION: MemoryLayer.TASK,
    MemoryEventType.TOOL_RESULT: MemoryLayer.TASK,
    MemoryEventType.TASK_COMPACT_SUMMARY: MemoryLayer.TASK,
    MemoryEventType.TASK_DISPATCH: MemoryLayer.AGENT,
    MemoryEventType.TASK_DISPATCH_RESULT: MemoryLayer.AGENT,
    MemoryEventType.AGENT_COMPACT_SUMMARY: MemoryLayer.AGENT,
    MemoryEventType.BLACKBOARD_PUBLISH: MemoryLayer.SESSION,
    # 过渡期旧类型
    MemoryEventType.OBSERVER_SUMMARY: MemoryLayer.AGENT,
    MemoryEventType.COMPACT_SUMMARY: MemoryLayer.AGENT,
}


def layer_for_types(types: list[MemoryEventType]) -> MemoryLayer:
    """从一次召回请求的类型集合推导层；要求同层，混层抛错（spec/06 §8）。"""
    layers = {EVENT_LAYER[t] for t in types}
    if len(layers) != 1:
        raise ValueError(f"recall types span multiple memory layers: {layers} for {types}")
    return layers.pop()


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class MemoryScope:
    """记忆范围限定。"""

    session_id: str
    task_id: str | None = None
    agent_id: str | None = None


@dataclass
class MemoryEvent:
    """ingest 的输入：一条要写入的 memory 事件。"""

    type: MemoryEventType
    scope: MemoryScope
    content: str | list[ContentPart]
    timestamp: datetime
    # 以下字段有默认值
    role: Literal["user", "assistant", "system", "tool"] | None = None
    topic: str | None = None  # 用于 topic-style 事件（含父子 task 通信）
    causation_id: str | None = None  # 关联上游 event
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryRecord:
    """recall 返回的统一表示。"""

    id: str
    type: MemoryEventType
    content: str | list[ContentPart]
    timestamp: datetime
    role: Literal["user", "assistant", "system", "tool"] | None = None
    topic: str | None = None
    score: float | None = None  # 仅 recall_semantic 时填
    metadata: dict = field(default_factory=dict)


@dataclass
class Subscription:
    """task 对 topic 的订阅（持久化）。

    task_id = 订阅方任务（读取该 topic 的 task）；"" 表示 session 级订阅（如 long_term_*）。
    """

    session_id: str
    topic: str
    cursor: int  # 已读到的 seq_no
    # subtask=自己派生的子任务结果（可 review/reopen）；predecessor=同 plan 前序结果（只读）；
    # long_term_*=跨 session 长期上下文。
    intent: Literal["subtask", "predecessor", "long_term_background", "long_term_project_log"]
    task_id: str = ""
    priority: int = 5


@dataclass
class CompactResult:
    """apply_compact 返回。"""

    events_before: int  # apply_compact 之前未 superseded 的事件数
    events_after: int  # 之后未 superseded 的事件数（含新写入的 COMPACT_SUMMARY）
    summary_event_id: str  # 新写入的 COMPACT_SUMMARY 事件 id


@dataclass
class MemoryProviderInfo:
    """Provider 能力声明。"""

    name: str
    supports_semantic: bool = False  # 是否支持 recall_semantic
    supports_topic: bool = True  # 是否支持 topic / subscription
    supports_compact_archival: bool = True  # apply_compact 是否真的归档
    max_event_size_bytes: int | None = None


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class MemoryProvider(Protocol):
    """统一 memory：摄取所有事件 + 多模召回。单实例，必需。"""

    name: str

    # ── 摄取（write）──

    @abstractmethod
    async def ingest(
        self,
        event: MemoryEvent,
        ctx: ProviderContext,
    ) -> str:
        """写入一个 memory event；返回 event id。

        core 调用方必须为每个重要事件调用 ingest；provider 自由决定是否持久化、
        如何索引。
        """
        ...

    # ── 召回（read，三种模式）──

    @abstractmethod
    async def recall_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        limit: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """按时间倒序返回最近 N 条指定类型事件。必需实现。

        PrepareStep 装配 messages 段的主路径。
        """
        ...

    @abstractmethod
    async def recall_topic(
        self,
        topic: str,
        since: int,
        ctx: ProviderContext,
    ) -> tuple[list[MemoryRecord], int]:
        """按 topic 拉取（自 since seq_no 之后），返回 (events, new_cursor)。

        必需实现。父子 task 通信 + 跨 session 订阅式上下文用。
        """
        ...

    @abstractmethod
    async def recall_semantic(
        self,
        query: str,
        scope: MemoryScope,
        top_k: int,
        ctx: ProviderContext,
    ) -> list[MemoryRecord]:
        """语义相似度召回。可选——core 默认实现返空；外部实现核心能力。

        Provider 通过 describe() 声明是否支持。
        """
        ...

    # ── 订阅（cross-session 长期上下文）──

    @abstractmethod
    async def subscribe_topic(
        self,
        session_id: str,
        topic: str,
        intent: Literal["subtask", "predecessor", "long_term_background", "long_term_project_log"],
        ctx: ProviderContext,
        task_id: str = "",
    ) -> str:
        """task 订阅 topic；返回 subscription id。幂等：同 (session, task, topic) 重复订阅保留游标。"""
        ...

    @abstractmethod
    async def list_subscriptions(
        self,
        session_id: str,
        ctx: ProviderContext,
        task_id: str | None = None,
    ) -> list[Subscription]:
        """列出订阅。task_id 给定时只返回该 task 的订阅 + session 级订阅（task_id=""）；None 返回全部。"""
        ...

    # ── 压缩（compact 触发时使用）──

    @abstractmethod
    async def apply_compact(
        self,
        scope: MemoryScope,
        summary: str,
        keep_last: int,
        ctx: ProviderContext,
        layer: MemoryLayer = MemoryLayer.AGENT,
    ) -> CompactResult:
        """折叠指定 layer 的 scope（spec/06 §7）。

        - layer=TASK：task compact，写 TASK_COMPACT_SUMMARY，折叠 task 层执行转录。
        - layer=AGENT：agent compact，写 AGENT_COMPACT_SUMMARY，按「完整派发对」折叠 agent 层。

        把该 layer scope 内、超出 keep_last 范围的事件标记 superseded
        （core 默认实现物理 archive；外部实现可能仅更新索引）。
        """
        ...

    # ── 工具 ──

    @abstractmethod
    async def count_recent(
        self,
        scope: MemoryScope,
        types: list[MemoryEventType],
        ctx: ProviderContext,
    ) -> int:
        """计数（供 PrepareStep 估算消息数）。"""
        ...

    @abstractmethod
    async def describe(self, ctx: ProviderContext) -> MemoryProviderInfo:
        """返回 provider 能力声明。"""
        ...
