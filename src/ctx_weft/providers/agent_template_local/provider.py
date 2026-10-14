"""LocalAgentTemplateProvider：单根目录扫描版 agent template provider（core 内置参考实现）。

根目录下每个含 SOUL.md 的子目录即一个模板；list()/get_template() 每次重新扫盘，
热更新友好（与 capability_skill_local 同族）。

**不做「缺 facet 从 default 模板借」**（2026-09-28 移出）：那是部署约定，不是模板加载的
一部分——「有一个叫 default 的母版」是宿主的产品概念。缺 facet 时的兜底全在 core 自己手里：
observe 判决档使用 ROLE + COMPACT、摘要档只使用 COMPACT；observer 与 compact 都不
回退 act，其中缺失的压缩纪律由 composer 的 core fallback 补上。recognize_intent 仍回退
act facet。规则见 `core/assembler/sources/identity.py`；宿主要「借母版」就在它自己的
AgentCapabilityProvider 里做。
"""

from __future__ import annotations

import dataclasses
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
        """入参是局部名（目录名/SOUL 的 name），出口贴归属前缀——见协议的 ⚠。

        只在 return 那一行贴：`scan()` 来的 `t.id` 是 loader 读出来的裸名，内部匹配与
        `list()` 的 `cap.id` 拼装都照旧用它。`replace` 是浅拷贝、与 `t` 共享 identity
        dict，但 loader 每次 scan 都重新 load（热更新友好，见模块 docstring），手上这份
        本来就没有别人在用。
        """
        for _, t in self._loader.scan(self._root):
            if t.id == template_id:
                return dataclasses.replace(t, id=f"{PROVIDER_NAME}:{t.id}")
        return None

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(self._loader.scan(self._root)),
            supports_streaming=False,
            supports_cancel=False,
            description=self.description,
        )
