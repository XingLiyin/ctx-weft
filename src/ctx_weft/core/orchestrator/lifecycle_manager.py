"""LifecycleManager：Agent 实例化 + spawn 深度检查。

Capability 解析已移至 PrepareStep（CapabilityResolver），
此处只负责从 template 创建 Agent 对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ctx_weft.core.errors import CtxWeftError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.state.models import Agent
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import LoopConfig, MemoryConfig
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate

logger = logging.getLogger(__name__)


class UnknownCapabilityError(CtxWeftError):
    pass


class SpawnDepthExceeded(CtxWeftError):
    pass


@dataclass
class _SessionDefaults:
    tenant_id: str
    fallback_template_id: str


@dataclass
class _AgentRecord:
    session_id: str
    tenant_id: str
    template_id: str
    parent_agent_id: str | None
    spawn_depth: int
    memory_config: MemoryConfig
    loop_config: LoopConfig


@dataclass
class LifecycleManager:
    """Agent 实例化 + 注册表。

    从前是「runtime.py 里 new 五次、用完即弃的无状态 dataclass」，现在是
    runtime 级长生命周期组件，`_agents` 是 agent 身份与配置的唯一住所——
    与 SessionManager 在 2026-09-02 做过的那次晋升同形（docs/events-v2.md §2.1.1）。
    Capability 解析已移至 PrepareStep（CapabilityResolver），此处只负责从
    template 创建 Agent 对象并登记。
    """

    template_lookup: "TemplateLookup"
    _agents: dict[str, _AgentRecord] = field(default_factory=dict)
    _sessions: dict[str, _SessionDefaults] = field(default_factory=dict)

    def register_session(
        self, session_id: str, *, tenant_id: str, fallback_template_id: str,
    ) -> None:
        """纳入管理。已存在则保留原状态（重入安全），与 SessionManager 同口径。"""
        self._sessions.setdefault(
            session_id, _SessionDefaults(tenant_id=tenant_id, fallback_template_id=fallback_template_id),
        )

    def release_session(self, session_id: str) -> None:
        for aid in [k for k, r in self._agents.items() if r.session_id == session_id]:
            self._agents.pop(aid, None)
        self._sessions.pop(session_id, None)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def template_id_of(self, agent_id: str) -> str:
        return self._agents[agent_id].template_id

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

        self._agents[agent.id] = _AgentRecord(
            session_id=session_id,
            tenant_id=tenant_id,
            template_id=agent.template_id,
            parent_agent_id=agent.parent_agent_id,
            spawn_depth=agent.spawn_depth,
            memory_config=template.memory_config,
            loop_config=template.loop_config,
        )

        logger.info(
            "LifecycleManager: instantiated agent %s (template=%s, depth=%d)",
            agent.id, template_id, spawn_depth,
        )
        return agent, template
