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
    import ast
    import importlib.util
    import pathlib

    import ctx_weft.core.runtime as m

    def _is_provider(name: str) -> bool:
        return name == "ctx_weft.providers" or name.startswith("ctx_weft.providers.")

    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=m.__file__)

    offenders: list[str] = []
    # 只看模块顶层的直接子节点——惰性 import 全部缩进在函数/方法体内，
    # 不是 ast.Module.body 的直接元素，天然被排除。
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_provider(alias.name):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                dotted = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(dotted, m.__package__)
            if _is_provider(base):
                offenders.append(base)
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                if _is_provider(target):
                    offenders.append(target)

    assert offenders == [], f"模块级 providers import 残留: {offenders}"
