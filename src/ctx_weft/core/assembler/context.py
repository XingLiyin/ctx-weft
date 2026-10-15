"""Collected source context exposed to prompt templates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock, ContextRequest


@dataclass(frozen=True)
class PromptContext:
    """One request plus the raw blocks collected for it.

    This object deliberately contains no placement or trimming policy. Templates select
    blocks and hand the chosen placement/priority to PromptBuilder.
    """

    request: ContextRequest
    blocks: tuple[ContextBlock, ...]

    def get(self, kind: str) -> tuple[ContextBlock, ...]:
        return tuple(block for block in self.blocks if block.kind == kind)

    def first(self, kind: str) -> ContextBlock | None:
        return next((block for block in self.blocks if block.kind == kind), None)
