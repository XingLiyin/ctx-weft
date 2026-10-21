"""Public imports and the optional HTTP dependency survive package reorganization."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_mock_and_provider_import_without_httpx():
    source = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": source + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(
        [sys.executable, "-c", """
import importlib.abc
import sys

class NoHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'httpx' or fullname.startswith('httpx.'):
            raise ModuleNotFoundError('HTTP dependency is unavailable')

sys.meta_path.insert(0, NoHTTP())
from ctx_weft.providers.llm import LLMAccount, LLMProvider, MockLLMAdapter, MockResponse
from ctx_weft.providers.llm.adapters import MockLLMAdapter as Adapter, ReasoningCandidate
from ctx_weft.providers.llm.store import LLMAccountStoreProtocol
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.testing import MockLLMAdapter as TestingAdapter
assert MockLLMAdapter is Adapter is TestingAdapter
assert 'httpx' not in sys.modules
"""],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_public_http_adapters_resolve_to_their_implementations():
    from ctx_weft.providers import llm
    from ctx_weft.providers.llm import adapters
    from ctx_weft.providers.llm.adapters.anthropic import (
        AnthropicAdapter,
        AnthropicMultimodalAdapter,
    )
    from ctx_weft.providers.llm.adapters.openai import OpenAIAdapter, OpenAIMultimodalAdapter

    for adapter in (
        AnthropicAdapter, AnthropicMultimodalAdapter,
        OpenAIAdapter, OpenAIMultimodalAdapter,
    ):
        assert getattr(llm, adapter.__name__) is adapter
        assert getattr(adapters, adapter.__name__) is adapter
