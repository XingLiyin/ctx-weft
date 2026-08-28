"""event 契约搬进 protocols 之后，两条 import 路径必须指向同一批对象。

spec: docs/superpowers/specs/2026-08-27-protocols-layer-event-contracts-design.md

本文件钉的是「搬迁不改行为」——re-export 必须是同一个对象（`is`），不是同名副本。
同名副本会让 `isinstance` 与 `EventType.X is EventType.X` 在跨路径比较时静默失败。
"""

from __future__ import annotations


def test_event_types_are_the_same_objects() -> None:
    """两条路径拿到的必须是同一个类对象，不是各自定义的同名类。"""
    from ctx_weft.core.events.types import Event as CoreEvent
    from ctx_weft.core.events.types import EventFilter as CoreFilter
    from ctx_weft.core.events.types import EventType as CoreType
    from ctx_weft.protocols.events import Event, EventFilter, EventType

    assert CoreEvent is Event
    assert CoreFilter is EventFilter
    assert CoreType is EventType


def test_event_constant_sets_are_the_same_objects() -> None:
    from ctx_weft.core.events.types import EVENT_TYPES as CORE_TYPES
    from ctx_weft.core.events.types import TRANSIENT_EVENT_TYPES as CORE_TRANSIENT
    from ctx_weft.protocols.events import EVENT_TYPES, TRANSIENT_EVENT_TYPES

    assert CORE_TYPES is EVENT_TYPES
    assert CORE_TRANSIENT is TRANSIENT_EVENT_TYPES


def test_package_level_reexport_still_works() -> None:
    """`from ctx_weft.core.events import X` 是 99 个测试文件在用的路径，不能断。"""
    from ctx_weft.core.events import Event, EventFilter, EventType
    from ctx_weft.protocols.events import Event as PEvent

    assert Event is PEvent
    assert EventFilter is not None and EventType is not None


def test_task_status_map_stays_in_core() -> None:
    """TASK_STATUS_BY_EVENT 是 core 的投影逻辑（依赖 core 的 TaskStatus），不进 protocols。"""
    import ctx_weft.protocols.events as pe
    from ctx_weft.core.events.types import TASK_STATUS_BY_EVENT

    assert TASK_STATUS_BY_EVENT
    assert not hasattr(pe, "TASK_STATUS_BY_EVENT")


def test_protocols_events_does_not_import_core() -> None:
    """层序守卫：protocols 不得依赖 core。

    这条不变量一旦破掉，`protocols/context.py` 那个刻意的惰性绑定就白做了，
    且会在某些 import 顺序下变成真实的循环导入。

    用 `ast` 解析实际的 import 语句，而不是对源码整体做子串扫描：字符串扫描
    误报（docstring/注释里提到字面路径 `ctx_weft.core` 也会被判违规）也漏报
    （`from ctx_weft import core`、`importlib.import_module(...)`、拼接/折行
    的字符串都扫不出来）。别为了"简化"把这条改回字符串匹配。
    """
    import ast
    import inspect

    import ctx_weft.protocols.events as pe

    source = inspect.getsource(pe)
    tree = ast.parse(source)

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "ctx_weft.core" or module.startswith("ctx_weft.core."):
                violations.append(f"line {node.lineno}: from {module} import ...")
            elif module == "ctx_weft":
                for alias in node.names:
                    if alias.name == "core":
                        violations.append(f"line {node.lineno}: from ctx_weft import core")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "ctx_weft.core" or name.startswith("ctx_weft.core."):
                    violations.append(f"line {node.lineno}: import {name}")

    assert not violations, (
        "protocols/events.py 不得 import core，违规语句：" + "; ".join(violations)
    )
