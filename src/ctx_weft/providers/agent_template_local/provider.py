"""LocalAgentTemplateProvider：单根目录扫描版 agent template provider（core 内置参考实现）。

根目录下每个含 SOUL.md 的子目录即一个模板；list()/get_template() 每次重新扫盘，
热更新友好（与 capability_skill_local 同族）。

**不做「缺 facet 从 default 模板借」**（2026-09-28 移出）：那是部署约定，不是模板加载的
一部分——「有一个叫 default 的母版」是宿主的产品概念。缺 facet 时的兜底全在 core 自己手里：
compact / recognize_intent 回退 act facet，observe 家族不回退、由装配层的通用 observer 文案
接手（见 `core/assembler/sources/identity.py::_OBSERVE_PURPOSES`）。宿主要「借母版」就在它自己
的 AgentCapabilityProvider 里做。
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
from ctx_weft.providers.agent_template_local._loader import TemplateLoader

logger = logging.getLogger(__name__)

# ⚠ 数据契约：存量事件/快照（host m010 迁移）与 host canonical 边界均以 "agent:" 为
# 前缀——此常量不可改名（spec 2026-07-22 方案 B「不变量」）。
PROVIDER_NAME = "agent"


class LocalAgentTemplateProvider(AgentCapabilityProvider):
    name = PROVIDER_NAME

    def __init__(
        self,
        templates_root: Path,
        loader: TemplateLoader | None = None,
    ) -> None:
        self._root = Path(templates_root)
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
        for _, t in self._loader.scan(self._root):
            if t.id == template_id:
                return t
        return None

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(self._loader.scan(self._root)),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
