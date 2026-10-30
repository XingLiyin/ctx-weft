"""Multi-account provider with per-model protocol, capabilities and limits."""

from __future__ import annotations

import dataclasses
import logging
import time

from ctx_weft.core.utils.estimate import default_output_reserve
from ctx_weft.protocols import ImagePart, LLMClient, LLMMessage, LLMRequest, TextPart
from ctx_weft.providers.llm.config import (
    LLMAccount,
    LLMModelNotFoundError,
    ModelCapabilities,
    ModelConfig,
    ModelLimits,
)
from ctx_weft.providers.llm.store import LLMAccountStoreProtocol

logger = logging.getLogger(__name__)
_PROBE_MAX_TOKENS = 16
# Valid 1x1 RGB PNG; a minimal image-input probe with no external URL dependency.
_PROBE_IMAGE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mNoaGgAAAMEAYF1LgG8AAAAAElFTkSuQmCC"
)


class _FixedModelClient:
    """Thin LLMClient wrapper that injects a fixed model into every request."""

    def __init__(
        self,
        adapter: LLMClient,
        model: str,
        context_limit: int,
        output_reserve: int,
        output_ceiling: int | None = None,
        account: str = "",
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._context_limit = context_limit
        self._output_reserve = output_reserve
        self._output_ceiling = output_ceiling
        self._account = account

    @property
    def model(self) -> str:
        """实际解析出的模型名（account/model 缺省经 default 解析后的真值）。
        duck-typed 非协议必需：runtime 执行前经 getattr 读取回填 session.llm_model，
        事件层（resolve_llm_identity）才有真值可报，不落 "mock" 兜底。"""
        return self._model

    @property
    def account(self) -> str:
        """实际解析出的账号名（同上，回填 session.llm_provider）。"""
        return self._account

    @property
    def context_limit(self) -> int:
        return self._context_limit

    @property
    def output_reserve(self) -> int:
        return self._output_reserve

    @property
    def output_ceiling(self) -> int | None:
        return self._output_ceiling

    @property
    def supports_tool_calling(self) -> bool:
        return self._adapter.supports_tool_calling

    def complete(self, request: LLMRequest, stream: bool = True):
        return self._adapter.complete(dataclasses.replace(request, model=self._model), stream)

    @property
    def tokenizer(self):
        return self._adapter.tokenizer_for(self._model)


# ── LLMProvider ───────────────────────────────────────────────────────────────


class LLMProvider:
    """Resolve configured models; reuse adapters for each account/protocol/image combination."""

    def __init__(self, store: LLMAccountStoreProtocol) -> None:
        self._accounts: dict[str, LLMAccount] = {}
        self._adapters: dict[str, dict[tuple[str, bool], LLMClient]] = {}
        self._adapter_settings: dict[str, tuple[str, str, int]] = {}
        self._store = store

    def register_account(self, account: LLMAccount, *, persist: bool = True) -> None:
        if account.name in self._accounts:
            raise KeyError(f"LLM account already registered: '{account.name}'")
        account.__post_init__()
        for model in account.models:
            model.__post_init__()
        if len({m.name for m in account.models}) != len(account.models):
            raise ValueError(f"Duplicate model names in account '{account.name}'")
        if not account.default_model and account.models:
            account.default_model = account.models[0].name
        if account.default_model:
            self._model_config(account, account.default_model)
        if persist:
            self._store.save(account)
        self._accounts[account.name] = account
        logger.info("LLMProvider: registered account '%s'", account.name)

    def delete_account(self, name: str) -> None:
        self.get_account(name)
        self._store.delete(name)
        del self._accounts[name]
        self._adapters.pop(name, None)
        self._adapter_settings.pop(name, None)

    def get_account(self, name: str) -> LLMAccount:
        if name not in self._accounts:
            raise KeyError(f"LLM account not found: '{name}'")
        return self._accounts[name]

    def list_accounts(self) -> list[LLMAccount]:
        return list(self._accounts.values())

    def is_registered(self, name: str) -> bool:
        return name in self._accounts

    def add_model(self, name: str, model: ModelConfig) -> LLMAccount:
        account = self.get_account(name)
        model.__post_init__()
        if any(m.name == model.name for m in account.models):
            raise ValueError(f"Model '{model.name}' already exists in account '{name}'")
        updated = dataclasses.replace(
            account,
            models=[*account.models, model],
            default_model=account.default_model or model.name,
        )
        self._store.save(updated)
        self._accounts[name] = updated
        return updated

    def update_model(self, name: str, model: ModelConfig) -> LLMAccount:
        account = self.get_account(name)
        self._model_config(account, model.name)
        model.__post_init__()
        updated = dataclasses.replace(
            account, models=[model if m.name == model.name else m for m in account.models]
        )
        self._store.save(updated)
        self._accounts[name] = updated
        return updated

    def remove_model(self, name: str, model: str) -> LLMAccount:
        account = self.get_account(name)
        models = [m for m in account.models if m.name != model]
        default = account.default_model
        if default == model:
            default = models[0].name if models else ""
        updated = dataclasses.replace(account, models=models, default_model=default)
        self._store.save(updated)
        self._accounts[name] = updated
        return updated

    def set_default_model(self, name: str, model: str) -> LLMAccount:
        account = self.get_account(name)
        self._model_config(account, model)
        updated = dataclasses.replace(account, default_model=model)
        self._store.save(updated)
        self._accounts[name] = updated
        return updated

    def _resolve_account(
        self, account: str | None, model: str | None
    ) -> tuple[LLMAccount, str | None]:
        if not self._accounts:
            raise RuntimeError(
                "No LLM accounts registered. Register one before resolving a client."
            )
        name = account or next(iter(self._accounts))
        if name not in self._accounts:
            logger.warning("LLM account '%s' not found; falling back to default account.", name)
            name, model = next(iter(self._accounts)), None
        return self.get_account(name), model

    @staticmethod
    def _model_config(account: LLMAccount, model: str | None) -> ModelConfig:
        selected = model or account.default_model
        config = next((m for m in account.models if m.name == selected), None)
        if config is None:
            raise LLMModelNotFoundError(f"Model '{selected}' not in account '{account.name}'")
        return config

    def get_model_config(self, account: str | None = None, model: str | None = None) -> ModelConfig:
        """Pure configuration lookup; shares default resolution with get_client."""
        acc, selected = self._resolve_account(account, model)
        return self._model_config(acc, selected)

    def get_client(self, account: str | None = None, model: str | None = None) -> LLMClient:
        acc, selected = self._resolve_account(account, model)
        cfg = self._model_config(acc, selected)
        cfg.__post_init__()
        reserve = cfg.output_reserve
        if reserve is None:
            reserve = default_output_reserve(cfg.limit.context)
        return _FixedModelClient(
            self._get_adapter(acc, cfg),
            cfg.name,
            cfg.limit.context,
            reserve,
            cfg.limit.output,
            account=acc.name,
        )

    def _get_adapter(self, account: LLMAccount, model: ModelConfig) -> LLMClient:
        model.__post_init__()
        settings = (account.base_url, account.api_key, account.timeout_sec)
        if self._adapter_settings.get(account.name) != settings:
            self._adapters[account.name] = {}
            self._adapter_settings[account.name] = settings
        cache = self._adapters[account.name]
        key = (model.protocol, "image" in model.capabilities.input)
        if key not in cache:
            cache[key] = self._build_adapter(account, model)
        return cache[key]

    def _build_adapter(self, account: LLMAccount, model: ModelConfig) -> LLMClient:
        model.__post_init__()
        image = "image" in model.capabilities.input
        if model.protocol == "openai-official":
            from ctx_weft.providers.llm.adapters.openai import OpenAIAdapter

            return OpenAIAdapter(
                api_key=account.api_key,
                base_url=account.base_url,
                timeout_sec=account.timeout_sec,
                image_input=image,
            )
        if model.protocol == "anthropic":
            from ctx_weft.providers.llm.adapters.anthropic import (
                AnthropicAdapter,
                AnthropicMultimodalAdapter,
            )

            cls = AnthropicMultimodalAdapter if image else AnthropicAdapter
        else:
            from ctx_weft.providers.llm.adapters.openai_compatible import (
                OpenAICompatibleAdapter,
                OpenAICompatibleMultimodalAdapter,
            )

            cls = OpenAICompatibleMultimodalAdapter if image else OpenAICompatibleAdapter
        return cls(
            api_key=account.api_key,
            base_url=account.base_url,
            timeout_sec=account.timeout_sec,
        )

    async def fetch_models(self, protocol: str, api_key: str, base_url: str) -> list[str]:
        account = LLMAccount(name="_probe", api_key=api_key, base_url=base_url)
        cfg = ModelConfig("_probe", protocol, ModelLimits(128_000))
        adapter = self._build_adapter(account, cfg)
        try:
            return await adapter.list_models()
        finally:
            await _close_adapter(adapter)

    async def fetch_models_for_account(self, name: str, *, protocol: str) -> list[str]:
        account = self.get_account(name)
        cfg = ModelConfig("_probe", protocol, ModelLimits(128_000))
        return await self._get_adapter(account, cfg).list_models()

    async def ping(self, protocol: str, api_key: str, base_url: str) -> float:
        started = time.perf_counter()
        await self.fetch_models(protocol, api_key, base_url)
        return (time.perf_counter() - started) * 1000.0

    async def ping_account(self, name: str, *, protocol: str) -> float:
        started = time.perf_counter()
        await self.fetch_models_for_account(name, protocol=protocol)
        return (time.perf_counter() - started) * 1000.0

    async def _probe_model(
        self, adapter: LLMClient, cfg: ModelConfig, *, verify_modalities: bool = True
    ) -> None:
        """Use image input for capability verification, plain text for routine latency checks."""
        content: str | list[TextPart | ImagePart] = "ping"
        if verify_modalities and "image" in cfg.capabilities.input:
            content = []
            if "text" in cfg.capabilities.input:
                content.append(TextPart(text="Reply OK."))
            content.append(ImagePart(data=_PROBE_IMAGE_PNG, media_type="image/png"))
        request = LLMRequest(
            model=cfg.name,
            system="",
            messages=[LLMMessage(role="user", content=content)],
            max_tokens=_PROBE_MAX_TOKENS,
        )
        async for _ in adapter.complete(request, stream=True):
            pass

    async def verify_model(
        self,
        protocol: str,
        api_key: str,
        base_url: str,
        *,
        model: str,
        capabilities: ModelCapabilities | None = None,
    ) -> float:
        account = LLMAccount(name="_probe", api_key=api_key, base_url=base_url)
        cfg = ModelConfig(
            model, protocol, ModelLimits(128_000), capabilities or ModelCapabilities()
        )
        adapter = self._build_adapter(account, cfg)
        started = time.perf_counter()
        try:
            await self._probe_model(adapter, cfg)
        finally:
            await _close_adapter(adapter)
        return (time.perf_counter() - started) * 1000.0

    async def verify_model_for_account(self, name: str, model: str | ModelConfig) -> float:
        """Accept a configured name, or a candidate config before it is saved."""
        account = self.get_account(name)
        cfg = model if isinstance(model, ModelConfig) else self._model_config(account, model)
        started = time.perf_counter()
        await self._probe_model(self._get_adapter(account, cfg), cfg)
        return (time.perf_counter() - started) * 1000.0

    async def ping_model_for_account(self, name: str, model: str) -> float:
        """Measure a configured model with text only; does not verify image support."""
        account = self.get_account(name)
        cfg = self._model_config(account, model)
        started = time.perf_counter()
        await self._probe_model(self._get_adapter(account, cfg), cfg, verify_modalities=False)
        return (time.perf_counter() - started) * 1000.0

    def load_from_store(self) -> int:
        count = 0
        for account in self._store.list_all():
            if account.name in self._accounts:
                continue
            try:
                self.register_account(account, persist=False)
                count += 1
            except Exception:
                logger.exception("LLMProvider: failed to load account '%s'", account.name)
        return count


async def _close_adapter(adapter: LLMClient) -> None:
    close = getattr(adapter, "aclose", None)
    if close is not None:
        await close()
    else:
        client = getattr(adapter, "_client", None)
        if client is not None:
            await client.aclose()
