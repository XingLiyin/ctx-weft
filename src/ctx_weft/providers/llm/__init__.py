"""LLM provider 层：多账号管理器 + Anthropic/OpenAI adapter + Mock。

host 直接从本模块引入即可：

    from ctx_weft.providers.llm import (
        LLMProvider, LLMAccount, ModelConfig,      # 多账号管理（实现 LLMClientResolver）
        AnthropicAdapter, OpenAIAdapter,           # 纯文本 adapter（需 httpx，extras: [llm]）
        AnthropicMultimodalAdapter, OpenAIMultimodalAdapter,  # 多模态 adapter
        MockLLMAdapter, MockResponse,              # 测试用 adapter（无额外依赖）
    )

`AnthropicAdapter` / `OpenAIAdapter` / `AnthropicMultimodalAdapter` / `OpenAIMultimodalAdapter`
依赖 httpx（`pip install "ctx-weft[llm]"`），因此采用惰性加载（PEP 562）：未安装 httpx 时，
只要不访问这些名字，本包依然可正常导入。
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
    from ctx_weft.providers.llm.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    from ctx_weft.providers.llm.openai import OpenAIAdapter, OpenAIMultimodalAdapter

__all__ = [
    "LLMProvider",
    "LLMAccount",
    "ModelConfig",
    "SUPPORTED_STYLES",
    "LLMAccountStoreProtocol",
    "MockLLMAdapter",
    "MockResponse",
    "AnthropicAdapter",
    "AnthropicMultimodalAdapter",
    "OpenAIAdapter",
    "OpenAIMultimodalAdapter",
]


def __getattr__(name: str):
    """惰性导出 httpx-dependent adapter（仅在被访问时才 import）。"""
    if name in ("AnthropicAdapter", "AnthropicMultimodalAdapter"):
        from ctx_weft.providers.llm import anthropic as _m
        return getattr(_m, name)
    if name in ("OpenAIAdapter", "OpenAIMultimodalAdapter"):
        from ctx_weft.providers.llm import openai as _m
        return getattr(_m, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
