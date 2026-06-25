"""LLMProvider — multi-account LLM provider.

Manages multiple LLMAccount configs and resolves LLMClient instances.
Implements the LLMClientResolver protocol so it can be registered
into ProviderRegistry via providers.register_llm_provider().
"""

from __future__ import annotations

import dataclasses
import logging
import time
from dataclasses import dataclass, field

from ctx_weft.protocols import LLMClient, LLMMessage, LLMRequest
from ctx_weft.providers.llm.store import LLMAccountStoreProtocol

logger = logging.getLogger(__name__)

SUPPORTED_STYLES = {"anthropic", "openai"}

# 模型可用性探测的补全预算。不用 1：个别推理模型对极小 max_tokens 会直接拒绝，
# 取一个小而安全的值——只需请求被接受、流能跑完，不关心实际产出多少 token。
_PROBE_MAX_TOKENS = 16


@dataclass
class ModelConfig:
    name: str
    context_limit: int
    max_output_tokens: int = 8192


@dataclass
class LLMAccount:
    """One API endpoint + credentials + list of available models."""

    name: str
    style: str          # "anthropic" | "openai"
    api_key: str
    base_url: str = ""
    models: list[ModelConfig] = field(default_factory=list)
    default_model: str = ""
    timeout_sec: int = 120


# ── Internal wrapper ──────────────────────────────────────────────────────────


class _FixedModelClient:
    """Thin LLMClient wrapper that injects a fixed model into every request."""

    def __init__(
        self,
        adapter: LLMClient,
        model: str,
        context_limit: int,
        max_output_tokens: int,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._context_limit = context_limit
        self._max_output_tokens = max_output_tokens

    @property
    def context_limit(self) -> int:
        return self._context_limit

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

    @property
    def supports_tool_calling(self) -> bool:
        return self._adapter.supports_tool_calling

    def complete(self, request: LLMRequest, stream: bool = True):
        return self._adapter.complete(
            dataclasses.replace(request, model=self._model), stream
        )

    async def count_tokens(self, text: str) -> int:
        return await self._adapter.count_tokens(text)


# ── LLMProvider ───────────────────────────────────────────────────────────────


class LLMProvider:
    """Multi-account LLM provider. Registered into ProviderRegistry.

    Usage:
        store = LLMAccountStore()
        provider = LLMProvider(store)
        provider.load_from_store()
        registry.register_llm_provider(provider)

        # Later, in session execution:
        llm = provider.get_client(account="claude", model="claude-opus-4-7")
    """

    def __init__(self, store: LLMAccountStoreProtocol, *, max_http_retries: int = 3) -> None:
        self._accounts: dict[str, LLMAccount] = {}
        self._adapters: dict[str, LLMClient] = {}
        self._store = store
        self._max_http_retries = max_http_retries

    # ── Account CRUD ──────────────────────────────────────────────────────────

    def register_account(self, account: LLMAccount, *, persist: bool = True) -> None:
        if account.name in self._accounts:
            raise KeyError(f"LLM account already registered: '{account.name}'")
        if account.style not in SUPPORTED_STYLES:
            raise ValueError(f"Unsupported style '{account.style}'. Must be one of: {SUPPORTED_STYLES}")
        if not account.default_model and account.models:
            account.default_model = account.models[0].name

        self._adapters[account.name] = self._build_adapter(account)
        self._accounts[account.name] = account
        if persist:
            self._store.save(account)
        logger.info("LLMProvider: registered account '%s' (%s)", account.name, account.style)

    def delete_account(self, name: str) -> None:
        if name not in self._accounts:
            raise KeyError(f"LLM account not found: '{name}'")
        del self._accounts[name]
        del self._adapters[name]
        self._store.delete(name)

    def get_account(self, name: str) -> LLMAccount:
        if name not in self._accounts:
            raise KeyError(f"LLM account not found: '{name}'")
        return self._accounts[name]

    def list_accounts(self) -> list[LLMAccount]:
        return list(self._accounts.values())

    def is_registered(self, name: str) -> bool:
        return name in self._accounts

    # ── Model management ─────────────────────────────────────────────────────

    def add_model(
        self,
        name: str,
        model: str,
        context_limit: int | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMAccount:
        account = self.get_account(name)
        if not any(m.name == model for m in account.models):
            account.models.append(ModelConfig(
                name=model,
                context_limit=context_limit or 128_000,
                max_output_tokens=max_output_tokens or 8192,
            ))
        if not account.default_model:
            account.default_model = model
        self._store.save(account)
        return account

    def remove_model(self, name: str, model: str) -> LLMAccount:
        account = self.get_account(name)
        account.models = [m for m in account.models if m.name != model]
        if account.default_model == model:
            account.default_model = account.models[0].name if account.models else ""
        self._store.save(account)
        return account

    def set_default_model(self, name: str, model: str) -> LLMAccount:
        account = self.get_account(name)
        if not any(m.name == model for m in account.models):
            raise ValueError(f"Model '{model}' not in account '{name}'")
        account.default_model = model
        self._store.save(account)
        return account

    # ── Client resolution ─────────────────────────────────────────────────────

    def get_client(
        self,
        account: str | None = None,
        model: str | None = None,
    ) -> LLMClient:
        """Resolve a LLMClient for the given account + model.

        account=None → first registered account.
        model=None   → account's default_model.
        """
        if not self._accounts:
            raise RuntimeError("No LLM accounts registered. Register one via LLMProvider.register_account() (or have the host register it) before resolving a client.")

        name = account or next(iter(self._accounts))
        if name not in self._accounts:
            # 账号可能已被删除（会话/任务仍钉着旧账号名）→ 回退到默认账号 + 默认模型，
            # 而不是直接 KeyError 让任务失败。沿用旧模型名会落到默认账号上不匹配，故置空。
            logger.warning("LLM account '%s' not found; falling back to default account.", name)
            name = next(iter(self._accounts))
            model = None
        acc = self.get_account(name)
        adapter = self._adapters[name]

        resolved_model = model or acc.default_model
        if not resolved_model:
            raise ValueError(
                f"No model specified and account '{name}' has no default model. "
                "Add one via LLMProvider.add_model() or set a default via LLMProvider.set_default_model()."
            )

        model_cfg = next((m for m in acc.models if m.name == resolved_model), None)
        ctx_limit = model_cfg.context_limit if model_cfg else 128_000
        max_out = model_cfg.max_output_tokens if model_cfg else 8192

        return _FixedModelClient(adapter, resolved_model, ctx_limit, max_out)

    # ── Model discovery / connectivity (host-facing; not on the protocol) ──────

    async def fetch_models(self, style: str, api_key: str, base_url: str = "") -> list[str]:
        """List models for ad-hoc (unregistered) credentials. Builds a transient adapter."""
        probe = LLMAccount(name="_probe", style=style, api_key=api_key, base_url=base_url)
        adapter = self._build_adapter(probe)  # raises ValueError on unsupported style
        return await adapter.list_models()

    async def fetch_models_for_account(self, name: str) -> list[str]:
        """List models for a registered account (reuses its live adapter)."""
        self.get_account(name)  # raises KeyError if missing
        return await self._adapters[name].list_models()

    async def ping(self, style: str, api_key: str, base_url: str = "") -> float:
        """Probe ad-hoc credentials; returns latency in ms. Raises on failure."""
        t0 = time.perf_counter()
        await self.fetch_models(style, api_key, base_url)
        return (time.perf_counter() - t0) * 1000.0

    async def ping_account(self, name: str) -> float:
        """Probe a registered account; returns latency in ms. Raises on failure."""
        t0 = time.perf_counter()
        await self.fetch_models_for_account(name)
        return (time.perf_counter() - t0) * 1000.0

    async def _probe_model(self, adapter: LLMClient, model: str) -> None:
        """Send one minimal completion with `model` and drain the stream.

        Validates that the model name exists AND the account has permission to
        call it: a bad/unauthorized model makes the adapter raise LLMCallError
        (HTTP 4xx) instead of merely listing models. Unlike fetch_models/ping,
        this actually exercises the configured model.
        """
        request = LLMRequest(
            model=model,
            system="",
            messages=[LLMMessage(role="user", content="ping")],
            max_tokens=_PROBE_MAX_TOKENS,
        )
        async for _ in adapter.complete(request, stream=True):
            pass

    async def verify_model(self, style: str, api_key: str, base_url: str = "", *, model: str) -> float:
        """Verify a model is callable with ad-hoc credentials; returns latency in ms.

        Builds a transient adapter and runs a minimal completion. Raises on failure
        (unsupported style, bad credentials, unknown model, no permission)."""
        probe = LLMAccount(name="_probe", style=style, api_key=api_key, base_url=base_url)
        adapter = self._build_adapter(probe)  # raises ValueError on unsupported style
        t0 = time.perf_counter()
        await self._probe_model(adapter, model)
        return (time.perf_counter() - t0) * 1000.0

    async def verify_model_for_account(self, name: str, model: str) -> float:
        """Verify a model is callable for a registered account (reuses its live adapter);
        returns latency in ms. Raises KeyError if missing, or on call failure."""
        self.get_account(name)  # raises KeyError if missing
        t0 = time.perf_counter()
        await self._probe_model(self._adapters[name], model)
        return (time.perf_counter() - t0) * 1000.0

    # ── Persistence ───────────────────────────────────────────────────────────

    def load_from_store(self) -> int:
        """Load all accounts from store. Returns count loaded."""
        count = 0
        for account in self._store.list_all():
            if account.name in self._accounts:
                continue
            try:
                self.register_account(account, persist=False)
                count += 1
            except Exception:
                logger.exception("LLMProvider: failed to load account '%s'", account.name)
        if count:
            logger.info("LLMProvider: restored %d account(s) from store", count)
        return count

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_adapter(self, account: LLMAccount) -> LLMClient:
        if account.style == "anthropic":
            from ctx_weft.providers.llm.anthropic import AnthropicAdapter
            return AnthropicAdapter(
                api_key=account.api_key,
                base_url=account.base_url or "https://api.anthropic.com",
                timeout_sec=account.timeout_sec,
                max_http_retries=self._max_http_retries,
            )
        if account.style == "openai":
            from ctx_weft.providers.llm.openai import OpenAIAdapter
            return OpenAIAdapter(
                api_key=account.api_key,
                base_url=account.base_url or "https://api.openai.com",
                timeout_sec=account.timeout_sec,
                max_http_retries=self._max_http_retries,
            )
        raise ValueError(f"Unsupported style: {account.style}")
