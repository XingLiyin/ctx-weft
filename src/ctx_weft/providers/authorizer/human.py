"""HITL 审批授权：每次工具调用前请求人工确认。

**无状态**：不持有任何 core 对象、不查任何缓存、不做 I/O。它只做两件纯判断——
第一次进来说「我需要一个人」，被重入时解释人给的决定。等待、登记、幂等、决定缓存
全部归 gateway 与 `core/hitl`（spec §2 / §9.2）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer
from ctx_weft.protocols.hitl import (
    HITL_FORM_APPROVAL,
    HITL_OUTCOME_ACCEPTED,
    HitlAsk,
    HitlDecision,
    ToolResultDelivery,
)

logger = logging.getLogger(__name__)


@dataclass
class HumanConfirmationAuthorizer(Authorizer):
    """每次工具调用前暂停，等待人工确认后再放行。

    host 装配只需 `set_authorizer(pattern, HumanConfirmationAuthorizer())`——**再也拿不到
    HITL 的把手**，这是解耦成为结构性事实而非纪律约定的直接体现。
    """

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="",
                        ) -> AuthorizationDecision:
        """总是让出。决定缓存的短路由 gateway 完成——它先查 registry，命中就直接
        走 `on_decision`，根本不会调到这里。"""
        return AuthorizationDecision(allowed=False, needs_human=HitlAsk(
            form=HITL_FORM_APPROVAL,
            delivery=ToolResultDelivery(tool_call_id=tool_call_id),
            prompt=f"Allow tool '{capability.name}'?",
            detail=capability.description,
            proposal=dict(arguments or {}),
            subject_id=capability.id,
        ))

    async def on_decision(self, capability, ctx, arguments, tool_call_id,
                          decision: HitlDecision) -> AuthorizationDecision:
        """解释人给的决定。**未知 outcome 落 else 分支 = 不放行**——放行是安全决定，
        未知值必须落到拒绝侧，而这个默认由本实现显式写出。"""
        if decision.outcome == HITL_OUTCOME_ACCEPTED:
            return AuthorizationDecision(
                allowed=True,
                message=decision.message,
                modified_arguments=decision.modified_arguments,
            )
        logger.info("HITL blocked '%s' (outcome=%s)", capability.id, decision.outcome)
        return AuthorizationDecision(allowed=False, message=decision.message)
