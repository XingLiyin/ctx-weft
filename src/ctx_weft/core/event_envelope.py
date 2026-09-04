"""事件封套的唯一构造点。**叶子模块**：只引 `core.utils` + `protocols.events`。

改造前，「无 run 语境」的封套（`run_id=None, sequence=0`）在全仓有 9 处逐字重复，
跨 3 个包：`core.orchestrator`（7）、`core.hitl`（1）、`core.runtime`（1）；
`EVENT_TYPES` 白名单校验另有 3 份（`loop.driver.make_event` + 两个 `_emit`）。

**为什么在 `core/` 顶层而不是某个子包里**：`core.hitl` 今天只依赖 `protocols` +
`core.utils` + 自己的兄弟模块。把 helper 塞进 `core.orchestrator` 会新增一条
`hitl → orchestrator` 边，而 `loop` 和 `runtime` 都在 hitl 之上，方向是反的。
本模块的依赖面与 `core/utils.py` 同级，谁都能引，无环。

**为什么不叫 `core/events.py`**：那个模块名是被明令禁止的——
`tests/unit/test_protocols_events_relocation.py` 断言 `ctx_weft.core.events`
及其子模块不得存在。旧的 `core/events/` 是 2026-08-27 三层划界时刻意留下的转发
shim，过渡期结束后于 08-29 删除，那条用例防的正是「有人为了少改一行又把转发层加
回来」。本模块与那件事无关（它不转发任何东西），但沿用那个名字会踩中守卫，也会
让读到的人误以为 shim 复活了。

run 级事件仍走 `core.loop.driver.make_event`——它做 LoopState 字段抽取 +
`sequence_counter` 自增，那是 run 域的真实职责；它只是不再自己拼 `Event`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols.events import EVENT_TYPES, Event

if TYPE_CHECKING:
    from datetime import datetime

    from ctx_weft.protocols.events import EventBus

__all__ = ["emit_event", "new_event"]


def new_event(
    event_type: str,
    *,
    session_id: str,
    tenant_id: str,
    origin: str,
    run_id: str | None = None,
    sequence: int = 0,
    task_id: str | None = None,
    agent_id: str | None = None,
    payload: dict | None = None,
    metadata: dict | None = None,
    timestamp: "datetime | None" = None,
    causation_id: str | None = None,
) -> Event:
    """构造一个 Event，自动分配 id 与时间戳。

    `run_id` / `sequence` 的默认值就是「无 run 语境」的形态——那是 session / agent /
    task 级事实的**定义特征**，不是随手挑的缺省。run 级调用方（`make_event`）显式传。

    未知事件类型 → `ValueError`：V1 严格白名单（设计文档 §14.3）。这是**唯一**一份
    校验；直接构造 `Event` 的路径此前会绕过它，故两个 `_emit` 各补了一份。
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}; not in EVENT_TYPES")
    return Event(
        id=generate_id("evt"),
        run_id=run_id,
        sequence=sequence,
        session_id=session_id,
        type=event_type,
        timestamp=timestamp or now_utc(),
        tenant_id=tenant_id,
        task_id=task_id,
        agent_id=agent_id,
        origin=origin,
        payload=payload or {},
        metadata=metadata or {},
        causation_id=causation_id,
    )


async def emit_event(bus: "EventBus | None", event_type: str, **kwargs) -> None:
    """构造并发出。``bus`` 为 None → no-op。

    None-tolerant 是 `TaskManager._emit` 的既有语义（TM 允许在没有总线的情况下被
    构造，大量单测依赖这一点），在这里统一承担。校验先于发射：坏类型不会先落一半。
    """
    ev = new_event(event_type, **kwargs)
    if bus is None:
        return
    await bus.emit(ev)
