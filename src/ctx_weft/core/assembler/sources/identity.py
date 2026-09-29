"""IdentitySource：读 template.identity[purpose]，渲染为 identity 段。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


#: observe 家族：前台 observe 与它的两档后台。这三个 purpose **不回退 act**。
#:
#: 其余 purpose（compact / recognize_intent）缺 facet 时仍回退 act——它们是 actor 自己的
#: 内部工序（压自己的对话、认自己的意图），拿 SOUL 当人格是对的。observe 不是：它的全部
#: 意义在于**不是 actor**，回退 act 等于让 actor 顶着自己的人格判自己，而且 system 提示
#: 本来就是同一段 SOUL（`composer._build_act_system`），等于同一段人格出现两遍。
#:
#: 这三个 purpose 缺 facet 时**不产 identity block**，由 `composer._OBSERVER_ROLE_FALLBACK`
#: 兜一份框架自带的通用 observer 准则。
_OBSERVE_PURPOSES = ("observe", "background_observe", "background_recap")


class IdentitySource:
    """从 template.identity 取对应 purpose 的 facet，渲染为 identity ContextBlock。

    缺失某 purpose 时 fallback 到 facets["act"]（详见设计文档 §4.6.6）——
    **observe 家族除外**，见 `_OBSERVE_PURPOSES`。
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
        # 两档后台 observe（判定 `background_observe` / 只摘要 `background_recap`）都复用
        # observe 的 ROLE facet——观察者的判断准则与「这一次要不要判」无关。
        facet = template.identity.get(request.purpose)
        if facet is None and request.purpose in ("background_observe", "background_recap"):
            facet = template.identity.get("observe")
        if facet is None and request.purpose not in _OBSERVE_PURPOSES:
            facet = template.identity.get("act")
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
