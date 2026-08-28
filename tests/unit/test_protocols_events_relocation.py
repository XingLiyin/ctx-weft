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
    """接线换了来源，`CtxWeftRuntime` 的默认 event_store 仍要能用。

    ⚠️ 修订记录：本测试原先按 brief 的 fallback 直接构造
    `InMemoryEventStore(event_bus=InProcessEventBus())`，绕开了
    `CtxWeftRuntime()` 无参构造会因「未注册 AgentCapabilityProvider」抛
    ValueError 这一既有硬校验（core/runtime.py:498-503，与本次搬迁无关）。
    但那样一来测试从未真正碰到 `core/runtime.py` 里本任务实际改动的两处接线
    （模块级 `InProcessEventBus` 改从 providers 取、函数内 `InMemoryEventStore`
    改从 providers 取），且与同文件的
    `test_implementations_still_satisfy_the_relocated_protocols` 重复。code
    review 后改为：照抄 `tests/unit/test_media_get_image.py` 里
    `_StubAgents` 的最小构造方式，真正实例化 `CtxWeftRuntime`；换源本身则用
    AST 读 runtime.py 的 import 语句直接钉住（对象身份比较会被 re-export
    链路掩盖，验证过，见下方注释与 task-3-report.md）。
    """
    from datetime import datetime, timezone

    from ctx_weft.core.runtime import CtxWeftRuntime, ProviderRegistry
    from ctx_weft.protocols.capability import (
        AgentCapability,
        AgentCapabilityProvider,
        CapabilityProviderInfo,
    )
    from ctx_weft.protocols.events import Event
    from ctx_weft.providers.events import InMemoryEventStore

    class _StubAgents(AgentCapabilityProvider):
        """`CtxWeftRuntime` 构造期硬校验要求至少一个 AgentCapabilityProvider——
        照抄 `tests/unit/test_media_get_image.py::_StubAgents`，只为满足这个
        构造期前置条件，与 event 体系无关。
        """

        name = "stub_agents"

        async def list(self, ctx): return [AgentCapability(id="stub_agents:a", name="a", kind="agent")]
        async def get_template(self, template_id, version, ctx): return None
        async def describe(self, ctx): return CapabilityProviderInfo(name=self.name)

    registry = ProviderRegistry()
    registry.register_capability(_StubAgents())
    rt = CtxWeftRuntime(providers=registry)

    # 行为面的最低限：默认 event_store 得是能用的 providers 版实现。
    assert isinstance(rt.event_store, InMemoryEventStore)

    # 「来源」的真正钉子：core/runtime.py 现在两处都被 re-export 链路环绕
    # （core.events.bus → providers.events.bus、core.state.event_store →
    # providers.events.store 都转发同一个类对象），所以 `is` 身份比较钉不住
    # 「从哪条路径 import」——不管 runtime.py 写 `from ctx_weft.core.events import
    # InProcessEventBus` 还是 `from ctx_weft.providers.events import
    # InProcessEventBus`，运行时拿到的都是同一个类对象，`is` 恒真（已用注入
    # 验证过，见 task-3-report.md 的「注入验证」小节）。故改用 AST 直接读
    # runtime.py 里两条 import 语句的 `module` 字段，钉的是源码里写的路径本身，
    # 不是运行时对象——这不是 Task 1 否决的"扫源码字符串"，是结构化解析。
    import ast
    import inspect

    import ctx_weft.core.runtime as runtime_module

    tree = ast.parse(inspect.getsource(runtime_module))
    import_sources: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name in ("InProcessEventBus", "InMemoryEventStore"):
                    import_sources[alias.name] = node.module

    assert import_sources.get("InProcessEventBus") == "ctx_weft.providers.events", (
        "core/runtime.py 模块级 InProcessEventBus 必须从 ctx_weft.providers.events 导入"
    )
    assert import_sources.get("InMemoryEventStore") == "ctx_weft.providers.events", (
        "core/runtime.py 函数内 InMemoryEventStore 必须从 ctx_weft.providers.events 导入"
    )

    # 订阅链路仍要能用：emit 之后 event_store 收得到（自动订阅 bus 后异步投递）。
    await rt._event_bus.emit(Event(
        id="evt_0001", run_id="run_1", sequence=1, session_id="ses_1",
        type="SessionCreated", timestamp=datetime(2026, 8, 28, tzinfo=timezone.utc),
    ))
    # 订阅是异步投递的，给它一次调度机会
    import asyncio
    await asyncio.sleep(0.05)
    assert await rt.event_store.read_by_session("ses_1")


def test_blob_ref_prefix_lives_in_context() -> None:
    """前缀属于内容形态（ImagePart.data 的前缀），不属于 memory，也不属于 events。

    移动的动机是让两个 blob 协议共用它而不互相 import——见 spec §3。
    """
    from ctx_weft.protocols import BLOB_REF_PREFIX as PkgPrefix
    from ctx_weft.protocols.context import BLOB_REF_PREFIX
    from ctx_weft.protocols.memory import BLOB_REF_PREFIX as MemPrefix

    assert BLOB_REF_PREFIX == "blob:"
    assert MemPrefix is BLOB_REF_PREFIX, "memory 侧仍要能拿到（re-export），且是同一对象"
    assert PkgPrefix is BLOB_REF_PREFIX
