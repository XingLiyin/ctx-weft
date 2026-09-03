"""instantiate（真新建）与 materialize（水合）是两件事。

拆开之前它们是同一个方法的两种模式，靠 existing_agent_id 是否为 None 区分，
而区分的结果只有调用方知道 —— 那正是 agent 域事件散落在 runtime 里的原因。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.agent_registry import AgentRegistry
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_echo_template,
)

pytestmark = pytest.mark.asyncio

TPL = "agent:tpl_echo"


class _Bus:
    """本文件测的是 instantiate/materialize 的返回值语义，不关心事件。"""

    async def emit(self, ev) -> None:
        return None


class _Client:
    def __init__(self, account="acct_default", model="mdl_default",
                 context_limit=200_000, output_reserve=8192):
        self.account, self.model = account, model
        self.context_limit, self.output_reserve = context_limit, output_reserve


def _lm() -> AgentRegistry:
    from ctx_weft.core.runtime import ProviderRegistry

    provider = InlineAgentTemplateProvider()
    provider.register(make_echo_template())
    providers = ProviderRegistry()
    providers.register_capability(provider)
    lm = AgentRegistry(
        template_lookup=TemplateLookup(providers=providers),
        event_bus=_Bus(),
        model_resolver=lambda a, m: _Client(),
    )
    lm.register_session("s1", tenant_id="default", fallback_template_id=TPL)
    return lm


async def test_instantiate_has_no_existing_agent_id_param():
    import inspect
    sig = inspect.signature(AgentRegistry.instantiate)
    assert "existing_agent_id" not in sig.parameters


async def test_materialize_carries_template_config():
    lm = _lm()
    agent, tmpl = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    got, rm = lm.materialize(agent.id)          # 不再收窗口参数
    assert got.id == agent.id
    assert got.template_id == tmpl.id
    # 从 template 来，不是 dataclass 默认 —— 这修掉了 agents_from_projection 的旧行为
    assert got.memory_config == tmpl.memory_config
    assert got.loop_config == tmpl.loop_config
    # 窗口从 client 派生（批次 B）
    assert got.loop_guard.context_limit == rm.context_limit
    assert got.loop_guard.reserved_output_tokens == rm.reserved_output_tokens


async def test_materialize_is_a_fresh_object_each_call():
    """Agent 带一次 run 的可变量（loop_guard.context_tokens 由 act.py 改写），
    所以每次派发产出新实例是正确的，不是浪费。"""
    lm = _lm()
    agent, _ = await lm.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default")
    a, _ = lm.materialize(agent.id)
    b, _ = lm.materialize(agent.id)
    assert a is not b


async def test_materialize_unknown_id_falls_back_and_never_raises(caplog):
    """恢复期缺口降级，不抛 —— 与 agents_from_projection 的既有口径一致。"""
    lm = _lm()
    got, _rm = lm.materialize("agt_never_seen")
    assert got.id == "agt_never_seen"
    assert got.template_id  # 回落到 session 的 fallback_template_id
    assert lm.has("agt_never_seen")  # 就地补登记，第二次不再警告
