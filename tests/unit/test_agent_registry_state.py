"""AgentRegistry 持有 agent 的身份与配置——注册表，不是状态机。

镜像 SessionManager 的 _SessionState / _states / register_session 形状
（docs/events-v2.md §2.1.1 的那次晋升）。区别：它零订阅。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.lifecycle_manager import LifecycleManager
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio


class _Bus:
    """本文件只测 registry 的状态语义，不关心事件——显式哨兵替身，不靠默认值。"""

    async def emit(self, ev) -> None:
        return None


def _lm() -> LifecycleManager:
    from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
    from ctx_weft.core.runtime import ProviderRegistry
    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    providers = ProviderRegistry()
    providers.register_capability(provider)
    return LifecycleManager(template_lookup=TemplateLookup(providers=providers), event_bus=_Bus())


async def test_instantiate_registers_a_record():
    lm = _lm()
    agent, _tmpl = await lm.instantiate(
        template_id="agent:tpl_echo", session_id="s1", tenant_id="default",
    )
    assert lm.has(agent.id)
    assert lm.template_id_of(agent.id) == agent.template_id


async def test_register_session_is_reentrant():
    lm = _lm()
    lm.register_session("s1", tenant_id="t1", fallback_template_id="agent:tpl_echo")
    lm.register_session("s1", tenant_id="OTHER", fallback_template_id="OTHER")
    # 已存在则保留原状态——与 SessionManager.register_session 同口径
    assert lm._sessions["s1"].tenant_id == "t1"


async def test_release_session_drops_only_that_sessions_agents():
    lm = _lm()
    a, _ = await lm.instantiate(
        template_id="agent:tpl_echo", session_id="s1", tenant_id="default")
    b, _ = await lm.instantiate(
        template_id="agent:tpl_echo", session_id="s2", tenant_id="default")
    lm.release_session("s1")
    assert not lm.has(a.id)
    assert lm.has(b.id)


async def test_instantiate_parent_fallback_uses_this_calls_session_not_most_recent():
    """多 session 并发恢复：unregistered 父 agent 的回落语境该取*本次 instantiate
    调用自己*的 session/tenant，不是「最近一次 register_session 的会话」——否则
    在 s2 之后才给 s1 spawn 子 agent 会把 s1 的孩子错记成 s2 的 tenant。
    """
    lm = _lm()
    lm.register_session("s1", tenant_id="tenant_1", fallback_template_id="agent:tpl_echo")
    lm.register_session("s2", tenant_id="tenant_2", fallback_template_id="agent:tpl_echo")
    child, _ = await lm.instantiate(
        template_id="agent:tpl_echo", session_id="s1", tenant_id="tenant_1",
        parent_agent_id="agt_ghost_parent_never_registered",
    )
    assert lm.has("agt_ghost_parent_never_registered")
    assert lm._agents["agt_ghost_parent_never_registered"].session_id == "s1"
    assert lm._agents["agt_ghost_parent_never_registered"].tenant_id == "tenant_1"
    assert lm._agents[child.id].tenant_id == "tenant_1"
