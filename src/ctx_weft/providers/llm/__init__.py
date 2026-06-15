"""LLM provider 层：多账号管理器 + Anthropic/OpenAI adapter + Mock。

host 直接从本模块引入即可：

    from ctx_weft.providers.llm import (
        LLMProvider, LLMAccount, ModelConfig,      # 多账号管理（实现 LLMClientResolver）
        AnthropicAdapter, OpenAIAdapter,           # 真实 adapter（需 httpx，extras: [llm]）
        MockLLMAdapter, MockResponse,              # 测试用 adapter（无额外依赖）
    )

`AnthropicAdapter` / `OpenAIAdapter` 依赖 httpx（`pip install "ctx-weft[llm]"`），因此采用
惰性加载（PEP 562）：未安装 httpx 时，只要不访问这两个名字，本包依然可正常导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.llm.provider import (
    SUPPORTED_STYLES,
    LLMAccount,
    LLMProvider,
    ModelConfig,
)
from ctx_weft.providers.llm.store import LLMAccountStoreProtocol

if TYPE_CHECKING:  # 让类型检查器/IDE 看得到，但运行时不触发 httpx 导入
    from ctx_weft.providers.llm.anthropic import AnthropicAdapter
    from ctx_weft.providers.llm.openai import OpenAIAdapter

__all__ = [
    "LLMProvider",
    "LLMAccount",
    "ModelConfig",
    "SUPPORTED_STYLES",
    "LLMAccountStoreProtocol",
    "MockLLMAdapter",
    "MockResponse",
    "AnthropicAdapter",
    "OpenAIAdapter",
]


def __getattr__(name: str):
    """惰性导出 httpx-dependent adapter（仅在被访问时才 import）。"""
    if name == "AnthropicAdapter":
        from ctx_weft.providers.llm.anthropic import AnthropicAdapter
        return AnthropicAdapter
    if name == "OpenAIAdapter":
        from ctx_weft.providers.llm.openai import OpenAIAdapter
        return OpenAIAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
