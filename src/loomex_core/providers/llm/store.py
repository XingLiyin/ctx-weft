"""LLMAccountStore protocol — persistence interface for LLM account configs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from loomex_core.providers.llm.provider import LLMAccount


@runtime_checkable
class LLMAccountStoreProtocol(Protocol):
    def save(self, account: LLMAccount) -> None: ...
    def delete(self, name: str) -> bool: ...
    def list_all(self) -> list[LLMAccount]: ...
