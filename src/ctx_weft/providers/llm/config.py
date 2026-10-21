"""Account connection settings and per-model API, capabilities and limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

LLMProtocol = Literal["openai", "anthropic"]
SUPPORTED_PROTOCOLS = frozenset({"openai", "anthropic"})


@dataclass
class ModelLimits:
    context: int
    output: int | None = None

    def __post_init__(self) -> None:
        if type(self.context) is not int or self.context <= 0:
            raise ValueError("limit.context must be a positive integer")
        if self.output is not None and (type(self.output) is not int or self.output <= 0):
            raise ValueError("limit.output must be a positive integer or None")


@dataclass
class ModelCapabilities:
    input: list[str] = field(default_factory=lambda: ["text"])
    output: list[str] = field(default_factory=lambda: ["text"])

    def __post_init__(self) -> None:
        if not self.input or any(m not in {"text", "image"} for m in self.input):
            raise ValueError("capabilities.input must contain text and/or image")
        if self.output != ["text"]:
            raise ValueError("Only text output is currently supported")


@dataclass
class ModelConfig:
    name: str
    protocol: LLMProtocol
    limit: ModelLimits
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    output_reserve: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("Model name must not be empty")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise ValueError(f"Unsupported protocol '{self.protocol}'")
        self.limit.__post_init__()
        self.capabilities.__post_init__()
        if self.output_reserve is not None and (
            type(self.output_reserve) is not int or self.output_reserve < 0
        ):
            raise ValueError("output_reserve must be a non-negative integer or None")


@dataclass
class LLMAccount:
    name: str
    api_key: str = field(repr=False)
    base_url: str
    models: list[ModelConfig] = field(default_factory=list)
    default_model: str = ""
    timeout_sec: int = 120

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.base_url.strip():
            raise ValueError("Account name and base_url must not be empty")
        if type(self.timeout_sec) is not int or self.timeout_sec <= 0:
            raise ValueError("timeout_sec must be a positive integer")


class LLMModelNotFoundError(ValueError):
    retriable = False
