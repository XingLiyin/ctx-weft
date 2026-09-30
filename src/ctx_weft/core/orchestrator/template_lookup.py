"""TemplateLookup：qualified 名反查 + cap.id 前缀精确路由的模板加载（内部组件，非协议）。

发现与加载同源（spec 2026-07-22）：模板进入 core 的唯一通道是 AgentCapabilityProvider。
- resolve_qualified：agent__planner → 完整 cap.id（'agent:planner'），保留 provider 归属；
- get_template：按 cap.id 前缀（rsplit(':', 1)，与装配层 _provider_meta 同口径）路由到
  唯一 provider；裸 id（无可路由前缀）直接 TemplateNotFoundError——边界强制规范 id，
  core 不做注册序扫描回落。

**回落开关**（`RuntimeConfig.fallback_template_ref`，2026-09-29）：host 可以交出一个
「解析不出来时改用这个」的 ref，于是 get_template 变成四段（精确路由 → 裸 id 补前缀 →
回落 → 抛）。core 并不因此知道「母版」是什么，它只拿到一个不透明字符串——概念留在 host，
策略值进配置。**不配就是一个字节不变**：下面两段新逻辑全部挂在这个开关下，没配时 core
仍然严格执行 spec 2026-07-22 的「边界强制规范 id」。
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
    def __init__(self, providers: "ProviderRegistry", fallback_ref: str = "") -> None:
        self._providers = providers
        # 空 = 不回落（历史行为）。非空 = host 声明「解析不出来时改用这个 ref」，
        # 见 get_template 的四段说明。core 不解释这个值的含义，只把它当 ref 再解析一次。
        self._fallback_ref = fallback_ref

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
        """规范 id（provider:name）前缀精确路由加载；配了回落则依次再试两段。

        ① **精确路由**：`provider:name` → 同名 provider（路由已确定，不问其他 provider，
           即使别家有同名模板）。provider 真实故障原样传播——那是故障，不是 miss。
        ② **裸 id 补前缀**（仅在配了回落时）：对每个已注册 agent provider 试 `name:id`。
        ③ **回落**（仅在配了回落时）：改解析 `fallback_ref`。
        ④ 仍然没有 → `TemplateNotFoundError`。

        **没配回落时 ②③ 不存在**，行为与改动前逐字节相同：裸 id / 未知前缀 / 路由到的
        provider 返回 None，一律直接抛（spec 2026-07-22「边界强制规范 id，core 不做注册序
        扫描回落」）。

        **为什么 ② 必须先于 ③。** 裸 id 不是坏 id，是**旧 id**——`LifecycleManager` 写进
        `Agent.template_id` 的一直是 `template.id`，也就是 provider 内部的 local name。
        少了 ② 这一段，那些裸 id 会直接掉进 ③ 拿到母版，而它们本来补个前缀就能找回真模板；
        更糟的是这个错误是**自洽**的：下次解析同样回落，agent 从此永远跑母版，没有任何一处
        会再发现它本该是别的。命中多个 provider 时取第一个并留 warning——裸 id 本就没保留
        是哪一家，猜一次总比整个 agent 换身份强，但要让人看得见。

        ② 是**推测性**的一轮，所以单个 provider 抛错只 log + 跳过（与 `resolve_qualified`
        同口径），不像 ① 那样上抛：一个坏 provider 不该把补前缀这条兜底路整条掐断。
        """
        providers = self._agent_providers()
        names = [p.name for p in providers]
        template = await self._route(ref, version, ctx, providers)
        if template is not None:
            return template
        if self._fallback_ref:
            if ":" not in ref:
                template = await self._complete_bare_id(ref, version, ctx, providers)
                if template is not None:
                    return template
            if self._fallback_ref != ref:
                template = await self._route(
                    self._fallback_ref, version, ctx, providers)
                if template is not None:
                    logger.warning(
                        "TemplateLookup: 模板 %r 解析不出来，已回落到 %r"
                        "（registered agent providers: %s）。这个 agent 跑的不是它声称的那份"
                        "模板——请检查模板是否装全、id 是否为规范形式 'provider:name'。",
                        ref, self._fallback_ref, ", ".join(names) or "(none)",
                    )
                    return template
        raise TemplateNotFoundError(ref, providers=names)

    async def _route(
        self, ref: str, version: str | None, ctx: "ProviderContext",
        providers: list[AgentCapabilityProvider],
    ) -> "AgentTemplate | None":
        """① 前缀精确路由。无前缀 / 没有同名 provider / provider 说 miss → None。

        provider 抛出的真实故障（IO/网络/解析错误）**不吞**：协议规定 miss 用 None 表示，
        抛出来的就是故障，掩盖它只会让「模板明明在、却一直回落母版」变成无从查起的怪事。
        """
        if ":" not in ref:
            return None
        provider_name, local_name = ref.rsplit(":", 1)
        for p in providers:
            if p.name == provider_name:
                return await p.get_template(local_name, version, ctx)
        return None

    async def _complete_bare_id(
        self, ref: str, version: str | None, ctx: "ProviderContext",
        providers: list[AgentCapabilityProvider],
    ) -> "AgentTemplate | None":
        """② 裸 id 补前缀：逐个 agent provider 试 `name:ref`，命中多个取第一个 + warning。"""
        found: list[tuple[str, AgentTemplate]] = []
        for p in providers:
            try:
                template = await p.get_template(ref, version, ctx)
            except Exception:
                logger.exception(
                    "TemplateLookup: provider %r get_template(%r) failed during "
                    "bare-id completion", p.name, ref)
                continue
            if template is not None:
                found.append((p.name, template))
        if not found:
            return None
        if len(found) > 1:
            logger.warning(
                "TemplateLookup: 裸 id %r 在 %d 个 provider 上都有（%s），取第一个——"
                "裸 id 没保留是哪一家。请把引用改成规范形式 'provider:name'。",
                ref, len(found), ", ".join(n for n, _ in found),
            )
        else:
            logger.warning(
                "TemplateLookup: 裸 id %r 已补前缀为 %r。请把引用改成规范形式 "
                "'provider:name'——这条兜底只在配了回落时存在。",
                ref, f"{found[0][0]}:{ref}",
            )
        return found[0][1]
