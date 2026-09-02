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


def test_hitl_timeout_reaches_the_waiter_that_actually_waits():
    """旋钮必须走完**最后一跳**：到达真正在等的那个对象。

    `rt._hitl_timeout_sec` 只是构造期的一次属性赋值——断言到它为止，等于没测接线：
    有人从 `_build_loop_ctx` 里的 `HitlWaiter(..., timeout_sec=...)` 删掉那个实参，
    每一次热等待都会**静默**退回默认（永不超时），而全量测试照绿。仓里其它
    `HitlWaiter(` 全是测试自己构造的，替代不了这条。
    """
    from ctx_weft.protocols import ProviderContext

    cfg = RuntimeConfig(hitl_timeout_sec=42)
    rt = make_runtime(agent_provider=_StubResolver(), config=cfg)
    loop_ctx = rt._build_loop_ctx(
        assembler=None, llm=None, memory=None,
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default"),
        gateway=None, skill_index={}, cancel_token=None, task_manager=None,
    )
    assert loop_ctx.waiter is not None, "loop context 必须带 waiter，否则热等待根本不存在"
    assert loop_ctx.waiter._timeout_sec == 42, (
        "配置的驱逐窗口没有到达 HitlWaiter —— 热等待会静默按默认值走"
    )
    # 同一个 registry：waiter 若挂在另一份账上，投递与终局就不在同一处发生。
    assert loop_ctx.waiter._registry is rt.hitl_registry
    assert loop_ctx.hitl is rt.hitl
