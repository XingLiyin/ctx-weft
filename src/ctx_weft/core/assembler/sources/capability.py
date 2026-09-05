"""CapabilitySource：按 cap.kind 分三路产出 capabilities blocks。

  kind="tool"   → 携 LLMTool 的 block（进 AssembledPrompt.tools，LLM 可直接调用）
  kind="skill"  → "### Available Skills" 条目
  kind="agent"  → "### Available Sub-Agents" 条目

三路最终都由 composer 渲染进 "## Capabilities" 段、拼到**末条 user message 尾部**
（所有 purpose 一致；不进 system——见 composer.py 槽位总览）。
purpose 门控：三类能力都按 cap.purposes 过滤；skill/agent 默认 purposes=["act"]，
故 observe / compact / recognize_intent 看不到 Skills / Sub-Agents 段。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils.ids import generate_id
from ctx_weft.protocols.capability import (
    AgentCapability,
    Capability,
    SkillCapability,
    ToolCapability,
    qualify,
)
from ctx_weft.protocols.llm import LLMTool

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


def build_llm_tools(caps: list[Capability], purpose: str) -> list[LLMTool]:
    """capability 列表 → LLM 线上工具数组（按 purpose 与 kind=="tool" 过滤）。

    **模块级纯函数，不是 CapabilityCache 的方法**：LLMTool 是 LLM 线上格式、属渲染层，
    cache 只是存储；把它挂上去会让存储层认识线上协议。散文段渲染（下面的 fetch）与
    活工具面（assembler 装在 AssembledPrompt.tools_fn 上的闭包）共用这一份，两条路
    因此不可能算出不同的工具集。

    **但两条路可以不等长，这是刻意的**：散文段是 ContextBlock，会被 budget 按预算裁剪；
    工具数组读 cache，不受裁剪。于是预算吃紧时，prompt 里介绍到的工具可能少于 API 实际
    提供的。取舍方向明确——工具数组是权威声明、散文段是补充说明；宁可模型能调到一个没被
    介绍的工具，也不要它被告知了却调不到。
    """
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

        # metadata["llm_tool"] 仍随块携带（qualified 名的既有断言点在读它），但它不再是
        # AssembledPrompt.tools 的来源——工具面已改由 cache 现算（见 build_llm_tools 文档）。
        llm_tool_by_name = {t.name: t for t in build_llm_tools(tools, request.purpose)}

        for cap in tools:
            qname = qualify(cap.id)
            llm_tool = llm_tool_by_name[qname]
            provider_name, provider_description = _provider_meta(cap.id)
            yield ContextBlock(
                id=generate_id("blk"),
                source="capability",
                kind="capabilities",
                target="system",
                content=cap.description,
                priority=slot_priority("capabilities"),
                token_estimate=request.token_counter(cap.description),
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
                token_estimate=request.token_counter(cap.description),
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
                token_estimate=request.token_counter(cap.description),
                metadata={
                    "capability_id": cap.id,
                    "capability_name": qualify(cap.id),
                    "capability_kind": "agent",
                    "provider_name": provider_name,
                    "provider_description": provider_description,
                },
            )
