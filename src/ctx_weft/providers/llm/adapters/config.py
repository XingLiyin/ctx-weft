"""Dependency-free built-in adapter settings and persistence codecs."""

from dataclasses import dataclass

from ctx_weft.protocols.llm import ModelCallConfig, register_model_call_config


@dataclass
class AnthropicCallConfig(ModelCallConfig):
    """An explicit thinking budget overrides the adapter's named budget mapping."""

    thinking_budget_tokens: int | None = None

    def validate(self) -> None:
        super().validate()
        budget = self.thinking_budget_tokens
        if budget is not None and (type(budget) is not int or budget < 1024):
            raise ValueError("thinking_budget_tokens must be at least 1024 or None")
        if budget is not None and self.reasoning_effort in ("none", "off"):
            raise ValueError("A thinking budget cannot be combined with disabled thinking")


register_model_call_config("anthropic", AnthropicCallConfig)
