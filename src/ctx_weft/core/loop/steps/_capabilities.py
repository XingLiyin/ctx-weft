"""共享 capability 解析+绑定，供 PrepareStep 与 ReconcileStep 复用。

ReconcileStep 必须在重跑 dangling 工具**之前**绑定 capability：它作为 resume 的 initial_step
跑在 PrepareStep 之前，而 per-agent cache 原本只在 PrepareStep 绑定（spec/07 §6 端到端缺陷修复）。
"""

from __future__ import annotations

import logging

from ctx_weft.core.orchestrator.capability_resolver import CapabilityResolver
from ctx_weft.core.orchestrator.control_capability import ControlCapabilityProvider
from ctx_weft.core.orchestrator.skill_executor_capability import SkillExecutorCapabilityProvider
from ctx_weft.core.state.models import NormalTaskSettings

logger = logging.getLogger(__name__)


async def resolve_capabilities(state, ctx) -> list:
    """据 template + task + providers 解析该 agent 绑定的 capability 列表。"""
    template = state.extra.get("template")
    if template is None or not ctx.capability_providers:
        return []

    settings = state.task.settings
    skill_name = settings.skill_name if isinstance(settings, NormalTaskSettings) else ""
    forbidden_ids = {
        ref.capability_id for ref in template.capability_refs if ref.mode == "forbidden"
    }

    builtin_caps: list = []
    other_providers: list = []
    for p in ctx.capability_providers:
        if isinstance(p, ControlCapabilityProvider):
            builtin_caps.extend(c for c in await p.list(ctx.provider_ctx) if c.id not in forbidden_ids)
        elif isinstance(p, SkillExecutorCapabilityProvider):
            if skill_name:
                builtin_caps.extend(c for c in await p.list(ctx.provider_ctx) if c.id not in forbidden_ids)
        else:
            other_providers.append(p)

    resolved = await CapabilityResolver().resolve(
        template=template, task=state.task, providers=other_providers, ctx=ctx.provider_ctx,
    )
    return builtin_caps + resolved


async def resolve_and_bind(state, ctx) -> list:
    """解析 capability 并写入 cache，返回绑定列表。

    控制工具是 session 全局的（每个 agent 都拿全部、生命周期为 session），注册到 cache 全局区
    （register_global，不随 per-run evict 逐出）；其余 per-run 解析的能力（skill/mcp/agent）按
    per-agent put。返回的 bound 仍含全部（供本 run 的 assembly）。
    """
    from ctx_weft.core.orchestrator.control_capability import PROVIDER_NAME as _CONTROL
    bound = await resolve_capabilities(state, ctx)
    if ctx.capability_cache is not None:
        _control_prefix = f"{_CONTROL}:"
        control = [c for c in bound if c.id.startswith(_control_prefix)]
        other = [c for c in bound if not c.id.startswith(_control_prefix)]
        ctx.capability_cache.register_global(control)
        ctx.capability_cache.put(state.agent.id, other)
    return bound
