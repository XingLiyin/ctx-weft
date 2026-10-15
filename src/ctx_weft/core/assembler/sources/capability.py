"""CapabilitySource：按 cap.kind 分三路产出 capabilities blocks。

  kind="tool"   → raw ToolCapability
  kind="skill"  → raw SkillCapability
  kind="agent"  → raw AgentCapability

Source 从 cache 的当前可用面返回原始能力；无 cache 的独立使用场景回退到
request.bound_capabilities。purpose 模板负责用途过滤、分组、排版和落位。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.capability import (
    AgentCapability,
    Capability,
    SkillCapability,
    SkillCapabilityProvider,
    ToolCapability,
    qualify,
)
from ctx_weft.protocols.llm import LLMTool

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


logger = logging.getLogger(__name__)


def build_llm_tools(caps: list[Capability], purpose: str) -> list[LLMTool]:
    """Build the frozen LLM tool snapshot for one purpose."""
    return [
        LLMTool(
            name=qualify(cap.id),
            description=cap.description,
            input_schema=cap.input_schema,
        )
        for cap in caps
        if isinstance(cap, ToolCapability) and purpose in cap.purposes
    ]


class CapabilitySource:
    name = "capability"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        cache = getattr(deps, "capability_cache", None)
        if cache is None:
            capabilities = request.bound_capabilities
        else:
            agent_id = getattr(request.agent, "id", "") or request.scope.agent_id
            task_id = getattr(request.task, "id", "") or request.scope.task_id
            capabilities = cache.available(agent_id, task_id)

        tools: list[ToolCapability] = []
        skills: list[SkillCapability] = []
        agents: list[AgentCapability] = []
        for cap in capabilities:
            if isinstance(cap, ToolCapability):
                tools.append(cap)
            elif isinstance(cap, SkillCapability):
                skills.append(cap)
            elif isinstance(cap, AgentCapability):
                agents.append(cap)

        # ── kind="tool" → LLMTool ────────────────────────────────────────────
        # provider_name → description（live：MCP connect 后才有；deps 持有 provider 对象）
        provider_index = getattr(deps, "capability_provider_index", None) or {}

        def _provider_meta(cap_id: str) -> tuple[str, str]:
            # provider 名 = cap.id 去掉末段（mcp:github:create_issue → mcp:github，
            # local_skill:pdf → local_skill），与 provider.name 对齐；描述 live 读取。
            pname = cap_id.rsplit(":", 1)[0]
            provider = provider_index.get(pname)
            return pname, (getattr(provider, "description", "") if provider else "")

        for cap in tools:
            qname = qualify(cap.id)
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qname,  # qualified: rendered into "Available Tools" prose
                    "capability_kind": "tool",
                    "input_schema": cap.input_schema,
                    "side_effects": cap.side_effects,
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
                value=cap,
            )

        # ── kind="skill" → 每个 skill 独立 block，由 composer 聚合成列表 ──────
        for cap in skills:
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qualify(cap.id),
                    "capability_kind": "skill",
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
                value=cap,
            )

        selected_skill = getattr(
            getattr(getattr(request, "task", None), "settings", None),
            "skill_name",
            "",
        )
        if request.purpose == "act" and selected_skill:
            selected = next(
                (cap for cap in skills if qualify(cap.id) == selected_skill),
                None,
            )
            if selected is not None:
                provider_name = selected.id.rsplit(":", 1)[0]
                provider = provider_index.get(provider_name)
                if isinstance(provider, SkillCapabilityProvider):
                    try:
                        definition = await provider.load_definition(
                            selected.name,
                            deps.provider_ctx,
                        )
                    except Exception:
                        logger.exception(
                            "CapabilitySource: failed to load skill definition for '%s'",
                            selected_skill,
                        )
                    else:
                        if definition is not None and definition.instructions:
                            yield ContextBlock(
                                id=generate_id("blk"),
                                source="capability:skill-definition",
                                kind="directive",
                                metadata={"kind": "skill_instructions"},
                                value=definition,
                            )

        # ── kind="agent" → 每个 agent 独立 block，由 composer 聚合成列表 ────────
        for cap in agents:
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qualify(cap.id),
                    "capability_kind": "agent",
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
                value=cap,
            )
