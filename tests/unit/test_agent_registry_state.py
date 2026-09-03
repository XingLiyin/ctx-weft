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


def _lm() -> LifecycleManager:
    from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
    from ctx_weft.core.runtime import ProviderRegistry
    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    providers = ProviderRegistry()
    providers.register_capability(provider)
    return LifecycleManager(template_lookup=TemplateLookup(providers=providers))


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
