"""Render the Act guidance snapshot into a budget-visible guidance block.

Legacy extra["act_guidance"] text remains accepted when no snapshot is supplied.
The composer places this block at the original trailing-user position.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class GuidanceSource:
    """Render a snapshot, or accept legacy text; omit empty guidance."""

    name = "guidance"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock
        from ctx_weft.core.assembler.prompts import load_prompt_template

        extra = request.extra or {}
        snapshot = extra.get("act_guidance_context")
        # Render before budgeting so the existing priority and estimate remain valid.
        text = (load_prompt_template("act").render("guidance", snapshot)
                if snapshot is not None else extra.get("act_guidance", ""))
        if not text:
            return
        yield ContextBlock(
            id=generate_id("blk"),
            source="guidance",
            kind="guidance",
            target="messages",
            content=text,
            priority=slot_priority("guidance"),
            token_estimate=request.token_counter(text),
            metadata={},
        )
