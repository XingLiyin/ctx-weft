from ctx_weft.core.config import RuntimeConfig
from ctx_weft.protocols import AgentCapabilityProvider, CapabilityProviderInfo
from tests.integration.test_minimal_loop import make_runtime


class _StubResolver(AgentCapabilityProvider):
    name = "agent"

    async def list(self, ctx): return []

    async def get_template(self, *a, **k): ...

    async def describe(self, ctx): return CapabilityProviderInfo(name=self.name)


def test_runtime_injects_hitl_knobs():
    cfg = RuntimeConfig(hitl_timeout_sec=42, hitl_max_resolved=7)
    rt = make_runtime(agent_provider=_StubResolver(), config=cfg)
    # 旋钮的落点随重设计换了对象：超时归管栈的 HitlWaiter（装配 LoopContext 时现造，
    # runtime 只持有值），保留上限归管账的 HitlRegistry。
    assert rt._hitl_timeout_sec == 42
    assert rt.hitl_registry._max_resolved == 7


def test_runtime_default_config():
    rt = make_runtime(agent_provider=_StubResolver())
    assert rt._config.task_max_concurrent == 4
