from loomex_core.core.config import RuntimeConfig
from loomex_core.core.runtime import LoomeXRuntime


class _StubResolver:
    async def get(self, *a, **k): ...


def test_runtime_injects_hitl_knobs():
    cfg = RuntimeConfig(hitl_timeout_sec=42, hitl_max_resolved=7)
    rt = LoomeXRuntime(template_resolver=_StubResolver(), config=cfg)
    assert rt.hitl_manager._timeout_sec == 42
    assert rt.hitl_manager._max_resolved == 7


def test_runtime_default_config():
    rt = LoomeXRuntime(template_resolver=_StubResolver())
    assert rt._config.task_max_concurrent == 4
