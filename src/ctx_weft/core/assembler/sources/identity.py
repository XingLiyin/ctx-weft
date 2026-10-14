"""IdentitySource：收集用途角色；judge 额外收集 compact 总结原则。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class IdentitySource:
    """从 template.identity 取用途所需的 facet，分别生成 identity ContextBlock。

    Facet 的选择与 fallback 是编译策略，唯一实现位于
    ``request_policy.select_identity_facet``；本 Source 只把结果适配成可参与预算的 block。
    judge 同时评价任务和生成摘要，compact 作为独立 block 保留来源与 token 预算。
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
        from ctx_weft.core.assembler.request_policy import (
            resolve_prompt_policy,
            select_identity_facet,
        )

        definition = resolve_prompt_policy(request)
        facet = select_identity_facet(template, definition.facet)
        facets = [(definition.facet, facet)]
        if definition.template == "observe" and definition.judge_task:
            facets.append(("compact", select_identity_facet(template, "compact")))
        for facet_name, selected in facets:
            if selected is None:
                continue
            text = selected.text
            yield ContextBlock(
                id=generate_id("blk"),
                source="identity",
                kind="identity",
                target="system",
                content=text,
                priority=slot_priority("identity"),  # 身份与总结原则均不可裁
                token_estimate=request.token_counter(text),
                metadata={
                    "template_id": template.id,
                    "template_version": template.version,
                    "facet_purpose": facet_name,
                    "style": selected.style,
                },
            )

        if facet is None:
            return

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
