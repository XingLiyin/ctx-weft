"""TemplateLookup：qualified 名反查 + cap.id 前缀精确路由的模板加载（内部组件，非协议）。

发现与加载同源（spec 2026-07-22）：模板进入 core 的唯一通道是 AgentCapabilityProvider。
- resolve_qualified：agent__planner → 完整 cap.id（'agent:planner'），保留 provider 归属；
- get_template：按 cap.id 前缀（rsplit(':', 1)，与装配层 _provider_meta 同口径）路由到
  唯一 provider；裸 id（无可路由前缀）直接 TemplateNotFoundError——边界强制规范 id，
  core 不做注册序扫描回落。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ctx_weft.core.errors import TemplateNotFoundError
from ctx_weft.protocols.capability import (
    AgentCapability, AgentCapabilityProvider, qualify,
)

if TYPE_CHECKING:
    from ctx_weft.core.runtime import ProviderRegistry
    from ctx_weft.protocols.context import ProviderContext
    from ctx_weft.protocols.template import AgentTemplate

logger = logging.getLogger(__name__)


class TemplateLookup:
    def __init__(self, providers: "ProviderRegistry") -> None:
        self._providers = providers

    def _agent_providers(self) -> list[AgentCapabilityProvider]:
        return [p for p in self._providers.get_capability_providers()
                if isinstance(p, AgentCapabilityProvider)]

    async def resolve_qualified(self, qualified: str, ctx: "ProviderContext") -> str:
        """qualified 工具名（agent__planner）→ 规范 cap.id（agent:planner）。

        未命中原样返回：字面值交给 get_template 判定（裸 id 在那里报错）。
        单 provider list() 失败 → log + 跳过（与装配路径吞异常口径一致）。"""
        for p in self._agent_providers():
            try:
                caps = await p.list(ctx)
            except Exception:
                logger.exception("TemplateLookup: provider %r list() failed", p.name)
                continue
            for cap in caps:
                if isinstance(cap, AgentCapability) and qualify(cap.id) == qualified:
                    return cap.id
        return qualified

    async def get_template(
        self, ref: str, version: str | None, ctx: "ProviderContext",
    ) -> "AgentTemplate":
        """规范 id（provider:name）前缀精确路由加载。

        裸 id / 未知前缀 / 路由到的 provider 返回 None → TemplateNotFoundError
        （路由已确定，不问其他 provider）。provider 真实故障原样传播。"""
        providers = self._agent_providers()
        names = [p.name for p in providers]
        if ":" in ref:
            provider_name, local_name = ref.rsplit(":", 1)
            for p in providers:
                if p.name == provider_name:
                    template = await p.get_template(local_name, version, ctx)
                    if template is None:
                        raise TemplateNotFoundError(ref, providers=names)
                    return template
        raise TemplateNotFoundError(ref, providers=names)
