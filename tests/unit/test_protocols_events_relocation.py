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


def test_bus_and_store_protocols_are_the_same_objects() -> None:
    from ctx_weft.core.events.bus import EventBus as CoreBus
    from ctx_weft.core.events.bus import SubscriptionHandle as CoreHandle
    from ctx_weft.core.state.event_store import EventStore as CoreStore
    from ctx_weft.core.state.event_store import RunSnapshot as CoreSnapshot
    from ctx_weft.protocols.events import (
        EventBus, EventStore, RunSnapshot, SubscriptionHandle,
    )

    assert CoreBus is EventBus
    assert CoreHandle is SubscriptionHandle
    assert CoreStore is EventStore
    assert CoreSnapshot is RunSnapshot


def test_implementations_do_not_enter_protocols() -> None:
    """实现不进 protocols——协议与实现分居是本次划界的全部意义。

    本任务只保证「不在 protocols」；Task 3 会把它们从 core 搬进 providers。
    """
    import ctx_weft.protocols.events as pe
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.state.event_store import InMemoryEventStore

    assert InProcessEventBus is not None and InMemoryEventStore is not None
    assert not hasattr(pe, "InProcessEventBus")
    assert not hasattr(pe, "InMemoryEventStore")


def test_implementations_still_satisfy_the_relocated_protocols() -> None:
    """re-export 若产生了同名副本，这条会红——runtime_checkable 认的是具体类对象。"""
    from ctx_weft.core.events.bus import InProcessEventBus
    from ctx_weft.core.state.event_store import InMemoryEventStore
    from ctx_weft.protocols.events import EventBus, EventStore

    assert isinstance(InProcessEventBus(), EventBus)
    assert isinstance(InMemoryEventStore(), EventStore)


def test_event_store_contract_is_exported_from_package_root() -> None:
    """host 必须实现 EventStore，却一直只能从 core 里 import 它（spec §1）。"""
    import ctx_weft
    from ctx_weft.protocols.events import EventStore

    assert ctx_weft.EventStore is EventStore
    assert "EventStore" in ctx_weft.__all__


def test_builtin_implementations_live_in_providers() -> None:
    """实现归 providers（spec §2 三层划界）。core 侧保留 re-export，对象必须同一。"""
    from ctx_weft.core.events.bus import InProcessEventBus as CoreBus
    from ctx_weft.core.state.event_store import InMemoryEventStore as CoreStore
    from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus

    assert CoreBus is InProcessEventBus
    assert CoreStore is InMemoryEventStore


def test_package_root_store_comes_from_providers() -> None:
    """host 的 `from ctx_weft import InMemoryEventStore` 拿到的仍是同一个类。"""
    import ctx_weft
    from ctx_weft.providers.events import InMemoryEventStore

    assert ctx_weft.InMemoryEventStore is InMemoryEventStore


def test_dead_simplified_store_is_gone() -> None:
    """`core/control/replay.py` 的同名简化版是死代码（零使用者），已删。

    留着的危害是「同名不同实现、都对外可见」：
    `from ctx_weft.core.control import InMemoryEventStore` 会拿到一个缺快照方法的对象。
    """
    import ctx_weft.core.control as cc
    import ctx_weft.core.control.replay as replay

    assert not hasattr(replay, "InMemoryEventStore")
    assert not hasattr(cc, "InMemoryEventStore")
    assert "InMemoryEventStore" not in getattr(cc, "__all__", [])


async def test_runtime_still_gets_a_working_default_store() -> None:
    """接线换了来源，默认 event_store 仍要能用（自动订阅 bus 后收得到事件）。

    ⚠️ 适配自 brief：`CtxWeftRuntime()` 无参构造会因「未注册
    AgentCapabilityProvider」抛 ValueError（core/runtime.py 里的硬校验，与本次
    搬迁无关）。改用 brief 建议的等价写法：直接构造
    `InMemoryEventStore(event_bus=InProcessEventBus())`，验证的正是 runtime.py
    里同一行接线（`self.event_store = event_store or InMemoryEventStore(event_bus=self._event_bus)`）
    背后的订阅链路是否仍然工作。
    """
    from datetime import datetime, timezone

    from ctx_weft.protocols.events import Event, EventStore
    from ctx_weft.providers.events import InMemoryEventStore, InProcessEventBus

    bus = InProcessEventBus()
    store = InMemoryEventStore(event_bus=bus)
    assert isinstance(store, EventStore)
    await bus.emit(Event(
        id="evt_0001", run_id="run_1", sequence=1, session_id="ses_1",
        type="SessionCreated", timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
    ))
    # 订阅是异步投递的，给它一次调度机会
    import asyncio
    await asyncio.sleep(0.05)
    assert await store.read_by_session("ses_1")
