"""LocalAgentTemplateProvider：单根目录扫描版 agent template provider（core 内置参考实现）。

根目录下每个含 SOUL.md 的子目录即一个模板；list()/get_template() 每次重新扫盘，
热更新友好（与 capability_skill_local 同族）。非 default 模板缺
compact/recognize_intent/observe facet 时从 default 模板补齐（merge_default_facets）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ctx_weft.protocols.capability import (
    AgentCapability,
    AgentCapabilityProvider,
    Capability,
    CapabilityProviderInfo,
)
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate
from ctx_weft.providers.agent_template_local._loader import (
    DEFAULT_MERGE_PURPOSES,
    TemplateLoader,
    merge_default_facets,
)

logger = logging.getLogger(__name__)

# ⚠ 数据契约：存量事件/快照（host m010 迁移）与 host canonical 边界均以 "agent:" 为
# 前缀——此常量不可改名（spec 2026-07-22 方案 B「不变量」）。
PROVIDER_NAME = "agent"


class LocalAgentTemplateProvider(AgentCapabilityProvider):
    name = PROVIDER_NAME

    def __init__(
        self,
        templates_root: Path,
        default_template_id: str = "default",
        loader: TemplateLoader | None = None,
    ) -> None:
        self._root = Path(templates_root)
        self._default_template_id = default_template_id
        self._loader = loader or TemplateLoader()

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return [
            AgentCapability(
                id=f"{PROVIDER_NAME}:{t.id}",
                name=t.id,
                template_name=t.id,
                description=t.description,
                version=t.version,
            )
            for _, t in self._loader.scan(self._root)
        ]

    async def get_template(
        self, template_id: str, version: str | None, ctx: ProviderContext,
    ) -> AgentTemplate | None:
        found: AgentTemplate | None = None
        default: AgentTemplate | None = None
        for _, t in self._loader.scan(self._root):
            if t.id == template_id:
                found = t
            if t.id == self._default_template_id:
                default = t
        if found is None:
            return None
        if found.id != self._default_template_id and default is not None:
            merge_default_facets(found, default, DEFAULT_MERGE_PURPOSES)
        return found

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(self._loader.scan(self._root)),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
