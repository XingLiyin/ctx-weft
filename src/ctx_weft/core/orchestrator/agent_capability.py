"""TemplateAgentCapabilityProvider：把可用 agent template 暴露为 AgentCapability。

与 ControlCapabilityProvider / SkillExecutorCapabilityProvider 同级，runtime 初始化时
自动注册。AgentCapabilityProvider 是 list 型 provider，可注册多个（本地模板 + 远端
注册中心等）；本 provider 仅覆盖「TemplateResolver 可见的模板」这一来源。
"""

from __future__ import annotations

import logging

from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    Capability,
    CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import TemplateResolver

logger = logging.getLogger(__name__)

PROVIDER_NAME = "agent"


class TemplateAgentCapabilityProvider(AgentCapabilityProvider):
    name = PROVIDER_NAME

    def __init__(self, template_resolver: TemplateResolver) -> None:
        self._resolver = template_resolver

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        try:
            summaries = await self._resolver.list_summaries(ctx)
        except Exception:
            logger.exception("TemplateAgentCapabilityProvider: list_summaries failed")
            return []
        return [
            AgentCapability(
                id=f"{PROVIDER_NAME}:{s.id}",
                name=s.id,
                template_name=s.id,
                description=s.description,
            )
            for s in summaries
        ]

    async def retrieve(self, ctx: ProviderContext) -> list[Capability]:
        """Sub-agents are bound only via the template's declared `subagents` (required
        refs), never auto-retrieved. Returning [] makes `subagents` a true allowlist —
        an agent never sees the whole template catalog as delegatable.
        """
        return []

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        try:
            count = len(await self._resolver.list_summaries(ctx))
        except Exception:
            count = 0
        return CapabilityProviderInfo(
            name=self.name, capability_count=count,
            supports_streaming=False, supports_cancel=False,
            description=self.description,
        )
