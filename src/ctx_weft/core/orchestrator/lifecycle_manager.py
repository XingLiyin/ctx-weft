"""LifecycleManager：Agent 实例化 + spawn 深度检查。

Capability 解析已移至 PrepareStep（CapabilityResolver），
此处只负责从 template 创建 Agent 对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ctx_weft.core.errors import CtxWeftError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.state.models import Agent
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate

logger = logging.getLogger(__name__)


class UnknownCapabilityError(CtxWeftError):
    pass


class SpawnDepthExceeded(CtxWeftError):
    pass


@dataclass
class LifecycleManager:
    """Agent 实例化。不再处理 capability 解析——由 PrepareStep.CapabilityResolver 负责。"""

    template_lookup: "TemplateLookup"

    async def instantiate_agent(
        self,
        template_id: str,
        session_id: str,
        tenant_id: str,
        parent_agent: Agent | None = None,
        ctx: ProviderContext | None = None,
        existing_agent_id: str | None = None,
    ) -> tuple[Agent, AgentTemplate]:
        """解析 template，创建 Agent 对象。

        template_id 须为规范形式 provider:name；裸 id 由 TemplateLookup 抛 TemplateNotFoundError。
        """
        resolve_ctx = ctx or ProviderContext(session_id=session_id, tenant_id=tenant_id)
        template: AgentTemplate = await self.template_lookup.get_template(
            template_id, None, ctx=resolve_ctx,
        )

        spawn_depth = 0
        if parent_agent is not None:
            spawn_depth = parent_agent.spawn_depth + 1
            if spawn_depth > template.loop_config.max_spawn_depth:
                raise SpawnDepthExceeded(
                    f"Max spawn depth {template.loop_config.max_spawn_depth} exceeded "
                    f"(current: {spawn_depth})"
                )

        agent = Agent(
            id=existing_agent_id or generate_id("agt"),
            session_id=session_id,
            template_id=template.id,
            tenant_id=tenant_id,
            parent_agent_id=parent_agent.id if parent_agent else None,
            spawn_depth=spawn_depth,
            memory_config=template.memory_config,
            loop_config=template.loop_config,
            created_at=now_utc(),
        )

        logger.info(
            "LifecycleManager: instantiated agent %s (template=%s, depth=%d)",
            agent.id, template_id, spawn_depth,
        )
        return agent, template
