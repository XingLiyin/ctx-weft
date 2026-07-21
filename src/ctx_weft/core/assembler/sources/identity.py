"""IdentitySource：读 template.identity[purpose]，渲染为 identity 段。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class IdentitySource:
    """从 template.identity 取对应 purpose 的 facet，渲染为 identity ContextBlock。

    缺失某 purpose 时 fallback 到 facets["act"]（详见设计文档 §4.6.6）。
    """

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
        # background_observe 复用 observe 的 ROLE facet；缺 observe 再回退 act。
        facet = (
            template.identity.get(request.purpose)
            or (template.identity.get("observe") if request.purpose == "background_observe" else None)
            or template.identity.get("act")
        )
        if facet is None:
            return

        text = facet.text
        yield ContextBlock(
            id=generate_id("blk"),
            source="identity",
            kind="identity",
            target="system",
            content=text,
            priority=slot_priority("identity"),  # 最高，几乎不可裁
            token_estimate=request.token_counter(text),
            metadata={
                "template_id": template.id,
                "template_version": template.version,
                "facet_purpose": request.purpose,
                "style": facet.style,
            },
        )

        # Skill instructions（Level 2）由 PrepareStep 加载后传入 request.extra
        skill_instructions: str = request.extra.get("skill_instructions", "")
        if skill_instructions:
            yield ContextBlock(
                id=generate_id("blk"),
                source="identity:skill",
                kind="directive",
                target="system",
                content=skill_instructions,
                priority=slot_priority("directive"),
                token_estimate=request.token_counter(skill_instructions),
                metadata={
                    "kind": "skill_instructions",
                    "skill_name": request.extra.get("skill_name", ""),
                },
            )
