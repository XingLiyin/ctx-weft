"""无状态授权策略：全放行 / 按模板白黑名单。契约见 ``protocols/capability.py``。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer


@dataclass
class AllowAllAuthorizer(Authorizer):
    """默认：放行全部。"""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True)


@dataclass
class AllowListAuthorizer(Authorizer):
    """按 agent 模板白/黑名单放行 capability id。

    allow_map: {template_id: set[capability_id]} —— 空集 = 全拦；模板不在表中 = 不限制。
    deny_map:  {template_id: set[capability_id]} —— deny 优先。
    deny_message: 被拦截时回灌给 LLM 的统一说明（可空）。
    模板维度取自 ``ctx.agent_template_id``。
    """

    allow_map: dict[str, set[str]] = field(default_factory=dict)
    deny_map: dict[str, set[str]] = field(default_factory=dict)
    deny_message: str = ""

    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        tmpl = ctx.agent_template_id
        allowed = self.allow_map.get(tmpl)
        denied = self.deny_map.get(tmpl, set())
        if capability.id in denied:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        if allowed is not None and capability.id not in allowed:
            return AuthorizationDecision(allowed=False, message=self.deny_message)
        return AuthorizationDecision(allowed=True)
