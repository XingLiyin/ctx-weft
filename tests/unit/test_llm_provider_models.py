"""LLMProvider.fetch_models / fetch_models_for_account / ping / verify_model —— 复用 _build_adapter，不改 protocol。"""
from __future__ import annotations

import httpx
import pytest

from ctx_weft.protocols import LLMCallError, LLMChunk
from ctx_weft.providers.llm.provider import LLMAccount, LLMProvider, ModelConfig


class _FakeStore:
    def save(self, account) -> None: ...
    def delete(self, name) -> bool: return True
    def list_all(self) -> list: return []


class _FakeAdapter:
    """最小 LLMClient：记录收到的 request，complete 产出 chunk 或抛 LLMCallError。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list = []

    def complete(self, request, stream: bool = True):
        self.calls.append(request)
        return self._gen()

    async def _gen(self):
        if self._fail:
            raise LLMCallError("API error 404: model not found", status_code=404, retriable=False)
        yield LLMChunk(kind="token", text="hi")
        yield LLMChunk(kind="done", finish_reason="stop")


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


async def test_verify_model_returns_latency_and_uses_configured_model(monkeypatch):
    p = _provider()
    fake = _FakeAdapter()
    monkeypatch.setattr(p, "_build_adapter", lambda acc: fake)
    ms = await p.verify_model(style="anthropic", api_key="k", base_url="", model="claude-opus-4-8")
    assert isinstance(ms, float) and ms >= 0
    assert len(fake.calls) == 1
    req = fake.calls[0]
    assert req.model == "claude-opus-4-8"
    assert req.max_tokens > 1  # 显式不用 1：避开个别推理模型对极小 max_tokens 的拒绝


async def test_verify_model_bad_model_raises(monkeypatch):
    p = _provider()
    monkeypatch.setattr(p, "_build_adapter", lambda acc: _FakeAdapter(fail=True))
    with pytest.raises(LLMCallError):
        await p.verify_model(style="anthropic", api_key="k", base_url="", model="nope")


async def test_verify_model_for_account_reuses_live_adapter():
    p = _provider()
    p.register_account(
        LLMAccount(name="acc", style="openai", api_key="sk", base_url="https://api.openai.com",
                   models=[ModelConfig("gpt-4o", 128_000)], default_model="gpt-4o"),
        persist=False,
    )
    fake = _FakeAdapter()
    p._adapters["acc"] = fake
    ms = await p.verify_model_for_account("acc", "gpt-4o")
    assert isinstance(ms, float) and ms >= 0
    assert fake.calls[0].model == "gpt-4o"


async def test_verify_model_for_account_unknown_raises():
    p = _provider()
    with pytest.raises(KeyError):
        await p.verify_model_for_account("nope", "m")
