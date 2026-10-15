"""IdentitySource：收集用途角色；judge 额外收集 compact 总结原则。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class IdentitySource:
    """Return the bound AgentTemplate without choosing a purpose facet."""

    name = "identity"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        template = request.template
        if template is None:
            return
        yield ContextBlock(
            id=generate_id("blk"),
            source="identity",
            kind="identity",
            value=template,
        )
