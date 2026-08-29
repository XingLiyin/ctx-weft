"""event_bus 必须可由 host 注入（与 event_store / hitl_manager / llm 一致）。"""

from __future__ import annotations

import pytest

from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry
from ctx_weft.providers.agent_template_local import LocalAgentTemplateProvider
from ctx_weft.providers.events import InProcessEventBus


@pytest.fixture
def runtime_registry(tmp_path):
    reg = ProviderRegistry()
    reg.register_capability(LocalAgentTemplateProvider(str(tmp_path)))
    return reg


def test_event_bus_is_injectable(runtime_registry) -> None:
    bus = InProcessEventBus()
    rt = CtxWeftRuntime(providers=runtime_registry, event_bus=bus)
    assert rt.event_bus is bus


def test_event_bus_defaults_when_absent(runtime_registry) -> None:
    rt = CtxWeftRuntime(providers=runtime_registry)
    assert isinstance(rt.event_bus, InProcessEventBus)


def test_runtime_module_has_no_toplevel_provider_import() -> None:
    """兜底实现只在需要时才 import——不得在模块级把 providers 拖进来。"""
    import pathlib

    import ctx_weft.core.runtime as m

    lines = pathlib.Path(m.__file__).read_text(encoding="utf-8").splitlines()
    toplevel = [
        ln for ln in lines
        if ln.startswith("from ctx_weft.providers") or ln.startswith("import ctx_weft.providers")
    ]
    assert toplevel == [], f"模块级 providers import 残留: {toplevel}"
