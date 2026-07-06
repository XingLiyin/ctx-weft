"""Authorizer：capability 授权协议 + 内置实现。

核心方法 `authorize(capability, ...) -> AuthorizationDecision`：对**一次工具调用**作放行/拦截
决定，并可携带回灌给 LLM 的 `message`（反馈/拒绝指导）与 allow 时的 `modified_arguments`（改写参数）。
`filter` 是基于 `authorize` 的批量便捷默认（可见性过滤），保留给装配期/外部用。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ctx_weft.core.state.models import Agent, Task
from ctx_weft.protocols.capability import Capability
from ctx_weft.protocols.context import ProviderContext

if TYPE_CHECKING:
    from ctx_weft.core.orchestrator.hitl_manager import HitlManager

logger = logging.getLogger(__name__)


@dataclass
class AuthorizationDecision:
    """一次授权的结构化结果。"""

    allowed: bool
    message: str = ""                              # 反馈 / 拒绝指导，回灌给 LLM（allow / deny 都可带）
    modified_arguments: dict[str, Any] | None = None  # allow 时的有效参数（None = 用原参）
    defer: bool = False                            # spec/07 §7：挂起本次调用（不放行也不拒绝；gateway 绝不 invoke）


class Authorizer(ABC):
    """对一次 capability 调用作授权决定。"""

    @abstractmethod
    async def authorize(
        self,
        capability: Capability,
        agent: Agent,
        task: Task | None,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
        *,
        tool_call_id: str = "",
    ) -> AuthorizationDecision: ...

    async def filter(
        self,
        capabilities: list[Capability],
        agent: Agent,
        task: Task | None,
        ctx: ProviderContext,
        arguments: dict[str, Any] | None = None,
    ) -> list[Capability]:
        """批量可见性过滤（基于 authorize 的默认实现）。"""
        result = []
        for cap in capabilities:
            if (await self.authorize(cap, agent, task, ctx, arguments)).allowed:
                result.append(cap)
        return result


@dataclass
class AllowAllAuthorizer(Authorizer):
    """默认：放行全部。"""

    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True)


@dataclass
class AllowListAuthorizer(Authorizer):
    """按 agent 模板白/黑名单放行 capability id。

    allow_map: {template_id: set[capability_id]} —— 空集 = 全拦；模板不在表中 = 不限制。
    deny_map:  {template_id: set[capability_id]} —— deny 优先。
    deny_message: 被拦截时回灌给 LLM 的统一说明（可空）。
    """

    allow_map: dict[str, set[str]] = field(default_factory=dict)
    deny_map: dict[str, set[str]] = field(default_factory=dict)
    deny_message: str = ""

    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        tmpl = agent.template_id
        allowed = self.allow_map.get(tmpl)
        denied = self.deny_map.get(tmpl, set())
        if capability.id in denied:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        if allowed is not None and capability.id not in allowed:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        return AuthorizationDecision(allowed=True)


@dataclass
class HumanConfirmationAuthorizer(Authorizer):
    """每次工具调用前暂停，等待人工确认后再放行。

    shell 侧持有同一个 HitlManager 实例，通过 approve() / reject() 响应；可在 approve 时
    携带 modified_arguments（改写参数），或在 reject 时携带 message（指导反馈）——二者经
    AuthorizationDecision 流出，由 CapabilityGateway 应用 / 回灌。
    """

    hitl_manager: "HitlManager"

    async def authorize(self, capability, agent, task, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        # 决定缓存命中直接用（cold reconcile，spec/07 §6）——内存优先,未命中回落事件日志
        # （否则跨重启再入会重新求批一遍）;内存 pending 由 request() 幂等复用。
        approval = await self.hitl_manager.find_resolved_for_tool_call(
            agent.session_id, tool_call_id)
        if approval is None:
            hitl_id = await self.hitl_manager.request(
                form="approval",
                session_id=agent.session_id,
                task_id=task.id if task else "",
                agent_id=agent.id,
                capability_id=capability.id,
                arguments=arguments or {},
                question=f"Allow tool '{capability.name}'?",
                context=capability.description,
                tool_call_id=tool_call_id,
            )
            approval = await self.hitl_manager.wait(hitl_id)   # may raise HitlPark on eviction
        if approval.accepted:
            return AuthorizationDecision(
                allowed=True,
                message=approval.message,
                modified_arguments=approval.modified_arguments,
            )
        logger.info("HITL blocked '%s' (status=%s)", capability.id, approval.status)
        return AuthorizationDecision(allowed=False, message=approval.message)
