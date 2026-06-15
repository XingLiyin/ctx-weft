"""KnowledgeProvider 协议。

定位：动态检索的参考资料。Agent 主动询问、按需取回的外部文献——
RAG 结果、Wiki 片段、API 文档、行业规则查询。

关键约束：Knowledge 是 dynamic & retrieval-based，作为 user 角色引用块进入
messages（"这是我刚查到的资料"），不进 system prompt。

详见设计文档 §4.2。
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loomex_core.protocols.context import Citation, ProviderContext


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class KnowledgeQuery:
    """检索请求。"""

    text: str
    intent: str | None = None  # "policy" | "reference" | "api_spec" | ...
    top_k: int = 5
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeDoc:
    """检索返回的单条文档。"""

    id: str
    content: str  # markdown 正文
    score: float  # 相关度
    source: str  # provider name 或子源标识
    metadata: dict[str, Any] = field(default_factory=dict)
    citation: Citation | None = None


@dataclass
class KnowledgeProviderInfo:
    """Provider 元信息。"""

    name: str
    collections: list[str] = field(default_factory=list)  # 可查询的 collection 列表
    supports_filters: bool = False
    recommended_top_k_range: tuple[int, int] = (1, 20)


# ── Protocol ──────────────────────────────────────────────────────────────────


@runtime_checkable
class KnowledgeProvider(Protocol):
    """外部知识库接入面。"""

    name: str

    @abstractmethod
    def retrieve(
        self,
        query: KnowledgeQuery,
        ctx: ProviderContext,
    ) -> AsyncIterator[KnowledgeDoc]:
        """流式返回相关知识片段。"""
        ...

    @abstractmethod
    async def describe(self, ctx: ProviderContext) -> KnowledgeProviderInfo:
        """返回 provider 元信息。"""
        ...
