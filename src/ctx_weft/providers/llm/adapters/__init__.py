"""Built-in LLM adapters and the shared reasoning extension for host subclasses.

HTTP adapters are loaded only when accessed, so mock-only users do not need httpx.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.providers.llm.adapters._stream_parsing import ReasoningCandidate
from ctx_weft.providers.llm.adapters.mock import MockLLMAdapter, MockResponse

if TYPE_CHECKING:
    from ctx_weft.providers.llm.adapters.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    from ctx_weft.providers.llm.adapters.openai import OpenAIAdapter, OpenAIMultimodalAdapter

__all__ = [
    "AnthropicAdapter",
    "AnthropicMultimodalAdapter",
    "OpenAIAdapter",
    "OpenAIMultimodalAdapter",
    "MockLLMAdapter",
    "MockResponse",
    "ReasoningCandidate",
]


def __getattr__(name: str):
    if name in ("AnthropicAdapter", "AnthropicMultimodalAdapter"):
        from ctx_weft.providers.llm.adapters import anthropic

        return getattr(anthropic, name)
    if name in ("OpenAIAdapter", "OpenAIMultimodalAdapter"):
        from ctx_weft.providers.llm.adapters import openai

        return getattr(openai, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
