"""HITL 审批授权：每次工具调用前暂停，等人工确认后再放行。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctx_weft.core.utils import content_to_text
from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer

if TYPE_CHECKING:
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager

logger = logging.getLogger(__name__)


@dataclass
class HumanConfirmationAuthorizer(Authorizer):
    """每次工具调用前暂停，等待人工确认后再放行。

    shell 侧持有同一个 HitlManager 实例，通过 approve() / reject() 响应；可在 approve 时
    携带 modified_arguments（改写参数），或在 reject 时携带 message（指导反馈）——二者经
    AuthorizationDecision 流出，由 CapabilityGateway 应用 / 回灌。
    热窗口被驱逐（超时）时返回 ``defer=True`` 的决定，由 gateway 挂起本次调用。
    """

    hitl_manager: "HitlManager"

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        # 决定缓存命中直接用（cold reconcile，spec/07 §6）——内存优先,未命中回落事件日志
        # （否则跨重启再入会重新求批一遍）;内存 pending 由 request() 幂等复用。
        approval = await self.hitl_manager.find_resolved_for_tool_call(
            ctx.session_id, tool_call_id)
        if approval is None:
            hitl_id = await self.hitl_manager.request(
                form="approval",
                session_id=ctx.session_id,
                task_id=ctx.task_id or "",
                agent_id=ctx.agent_id or "",
                capability_id=capability.id,
                arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?",
                context=capability.description,
                tool_call_id=tool_call_id,
            )
            approval = await self.hitl_manager.wait_for_decision(hitl_id)
            if approval is None:
                # 热→冷驱逐：不放行也不拒绝。gateway 见 defer 即「绝不调 provider.invoke
                # + 挂起」（capability_gateway.py 的 defer 分支）。走协议的挂起语义而不是
                # 让 core 的 HitlPark 穿过 authorize()——Authorizer 是 host 扩展点，
                # 内置实现该做契约的范例。
                return AuthorizationDecision(allowed=False, defer=True)
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True,
                message=content_to_text(approval.message),
                modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (outcome=%s)", capability.id, approval.outcome)
        return AuthorizationDecision(allowed=False, message=content_to_text(approval.message))
