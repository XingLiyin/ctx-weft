"""CapabilitySource：按 cap.kind 分三路渲染 bound capabilities。

  kind="tool"   → LLMTool block（当前 LLM 可直接调用）
  kind="skill"  → "Available Skills" 文本块注入 system prompt
  kind="agent"  → "Available Sub-Agents" 文本块注入 system prompt
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils import estimate_tokens, generate_id
from ctx_weft.protocols.capability import AgentCapability, SkillCapability, ToolCapability, qualify

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class CapabilitySource:
    name = "capability"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock
        from ctx_weft.protocols import LLMTool

        tools: list[ToolCapability] = []
        skills: list[SkillCapability] = []
        agents: list[AgentCapability] = []

        for cap in request.bound_capabilities:
            # purpose 门控对三类能力一致：tool / skill / agent 都按 cap.purposes 过滤。
            # skill / agent 默认 purposes=["act"]（委派只发生在 act），故 observe / compact /
            # recognize_intent 不再看到 "Available Skills" / "Available Sub-Agents"。
            if request.purpose not in cap.purposes:
                continue
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
            llm_tool = LLMTool(
                name=qname,
                description=cap.description,
                input_schema=cap.input_schema,
            )
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                target="system",
                content=cap.description,
                priority=slot_priority("capabilities"),
                token_estimate=estimate_tokens(cap.description),
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qname,  # qualified: rendered into "Available Tools" prose
                    "capability_kind": "tool",
                    "input_schema": cap.input_schema,
                    "side_effects": cap.side_effects,
                    "llm_tool": llm_tool,
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
            )

        # ── kind="skill" → 每个 skill 独立 block，由 composer 聚合成列表 ──────
        for cap in skills:
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                target="system",
                content=cap.description,
                priority=slot_priority("capabilities"),
                token_estimate=estimate_tokens(cap.description),
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qualify(cap.id),
                    "capability_kind": "skill",
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
            )

        # ── kind="agent" → 每个 agent 独立 block，由 composer 聚合成列表 ────────
        for cap in agents:
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                target="system",
                content=cap.description,
                priority=slot_priority("capabilities"),
                token_estimate=estimate_tokens(cap.description),
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qualify(cap.id),
                    "capability_kind": "agent",
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
            )
