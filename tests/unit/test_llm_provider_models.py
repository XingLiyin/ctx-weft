"""LLMProvider.fetch_models / fetch_models_for_account / ping —— 复用 _build_adapter，不改 protocol。"""
from __future__ import annotations

import httpx
import pytest

from loomex_core.providers.llm.provider import LLMAccount, LLMProvider, ModelConfig


class _FakeStore:
    def save(self, account) -> None: ...
    def delete(self, name) -> bool: return True
    def list_all(self) -> list: return []


def _provider() -> LLMProvider:
    return LLMProvider(_FakeStore())


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_models_for_account():
    p = _provider()
    p.register_account(
        LLMAccount(name="acc", style="openai", api_key="sk", base_url="https://api.openai.com",
                   models=[ModelConfig("gpt-4o", 128_000)], default_model="gpt-4o"),
        persist=False,
    )
    p._adapters["acc"]._client = _mock(
        lambda req: httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "o3"}]})
    )
    assert await p.fetch_models_for_account("acc") == ["gpt-4o", "o3"]


async def test_fetch_models_dynamic(monkeypatch):
    p = _provider()
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(200, json={"data": [{"id": "claude-opus-4-8"}]})

    real_build = p._build_adapter

    def fake_build(acc):
        a = real_build(acc)
        a._client = _mock(handler)
        return a

    monkeypatch.setattr(p, "_build_adapter", fake_build)
    models = await p.fetch_models(style="anthropic", api_key="k", base_url="https://api.anthropic.com")
    assert models == ["claude-opus-4-8"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"


async def test_ping_returns_latency(monkeypatch):
    p = _provider()

    async def fake_fetch(style, api_key, base_url=""):
        return ["m"]

    monkeypatch.setattr(p, "fetch_models", fake_fetch)
    ms = await p.ping(style="openai", api_key="k")
    assert isinstance(ms, float) and ms >= 0


async def test_fetch_models_unknown_account_raises():
    p = _provider()
    with pytest.raises(KeyError):
        await p.fetch_models_for_account("nope")
