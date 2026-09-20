"""超长会话不得出现「读整条事件流」的查询。

`read_by_session` 一次性把整条流变成 `list[Event]`——实测一条 3 万事件的会话约
130MB 常驻、读一次 3.5 秒（SQLite 本地；Postgres 走网更慢）。所以它只有一个正当用途：
快照不可用时重放整条流。凡是只关心几种事件类型的折叠，都必须按类型收窄；而那唯一正当的
重放也要分批，不把整条流驻留内存。

这里钉两件事：
1. 只关心少数类型的折叠**不得**触发全量读（段 recap 折叠曾经就是全量，每次 `/resume` 付一次）；
2. 无快照的全量重放走分批，且**分批与整批逐字段等价**（重放是左折叠，可结合）。

第 2 条的分批**由 store 产出**（`EventStore.replay`）。core 只
`async for batch in store.replay(sid)`，不问「你支不支持分页」、不替谁选降级路：那种能力
探测曾经写在 core 里，一个坏设计生出两个分支和两种失败形态。本分支的必需读法里没有游标，
所以协议的默认实现就是一次性；能分批的 store 自己覆盖 `replay`。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ctx_weft.core.control.reducers import (
    TASK_RECAP_EVENT_TYPES,
    rebuild_view,
    reduce_events,
)
from ctx_weft.core.events.types import Event, EventType
from ctx_weft.core.state.event_store import EventStore, InMemoryEventStore

#: 分批大小现在是 store 的事（`EventStore.REPLAY_BATCH`），core 不持有它。
_REPLAY_BATCH = EventStore.REPLAY_BATCH

pytestmark = pytest.mark.asyncio

_T0 = datetime(2026, 9, 20, tzinfo=UTC)
_SID = "s_long"


def _ev(n: int, type_: str, **payload) -> Event:
    return Event(
        id=f"evt_{n:08d}", run_id="r1", sequence=n, session_id=_SID, type=type_,
        timestamp=_T0, tenant_id="acme", payload=payload,
    )


class _CountingStore(InMemoryEventStore):
    """记下每条读路径被调了几次——断言的是**读法**，不只是结果。"""

    def __init__(self) -> None:
        super().__init__()
        self.full_reads = 0
        self.typed_reads: list[tuple[str, ...]] = []
        self.batches: list[int] = []

    async def read_by_session(self, session_id: str):
        self.full_reads += 1
        return await super().read_by_session(session_id)

    async def read_session_events_of_types(self, session_id: str, types):
        self.typed_reads.append(tuple(str(t) for t in types))
        return await super().read_session_events_of_types(session_id, types)

    async def replay(self, session_id: str):
        async for batch in super().replay(session_id):
            self.batches.append(len(batch))
            yield batch


async def _seed(store: InMemoryEventStore, n_noise: int) -> None:
    """一条「长会话」：大量与段 recap 无关的噪音事件 + 两对 recap 事件。"""
    await store.append(_ev(0, EventType.SESSION_CREATED, user_prompt="go",
                           template_id="agent:tpl", root_agent_id="agt_root"))
    for i in range(1, n_noise + 1):
        await store.append(_ev(i, EventType.RUN_FINISHED, outcome="completed"))
    # 一对完成的（started + done）+ 一对被崩溃打断的（只有 started）
    await store.append(_ev(n_noise + 1, EventType.TASK_RECAP_STARTED,
                           task_id="t_done", boundary="finish", agent_id="agt_root"))
    await store.append(_ev(n_noise + 2, EventType.TASK_RECAP_DONE, task_id="t_done"))
    await store.append(_ev(n_noise + 3, EventType.TASK_RECAP_STARTED,
                           task_id="t_stuck", boundary="finish", agent_id="agt_root"))


# ── 1. 段 recap 折叠：按类型收窄，不碰全量 ──────────────────────────────────


async def test_recap_fold_reads_only_the_two_recap_types() -> None:
    """收窄 helper 本身：给它那个类型集，它只发一次类型查询、不碰全量。

    ⚠️ 这条**只测 helper**，护不住「调用点用错工具」——那条在
    `tests/integration/test_task_recap_recovery.py::test_resume_does_not_read_the_whole_event_stream`，
    它走真实的 `/resume` → `recover_session` 路径。两条都要有：这条钉工具本身的行为，
    那条钉真实调用点确实用了它。
    """
    from tests.integration.test_minimal_loop import (
        InlineAgentTemplateProvider, make_echo_template, make_runtime,
    )

    resolver = InlineAgentTemplateProvider()
    resolver.register(make_echo_template())
    store = _CountingStore()
    rt = make_runtime(agent_provider=resolver, event_store=store)
    await _seed(store, n_noise=500)

    pending = await rt._read_session_events_of_types(_SID, TASK_RECAP_EVENT_TYPES)

    assert store.full_reads == 0, "不该为段 recap 折叠读整条事件流"
    assert store.typed_reads == [tuple(str(t) for t in TASK_RECAP_EVENT_TYPES)]
    # 503 条噪音里只取回 3 条
    assert len(pending) == 3, f"只该取回那两类，实际 {len(pending)} 条"


async def test_recap_type_set_matches_what_the_fold_actually_reads() -> None:
    """类型集与折叠实现必须同步——漏一个类型就会静默少折出一个待重跑的段。"""
    import inspect

    from ctx_weft.core.control.reducers import fold_pending_task_recap

    src = inspect.getsource(fold_pending_task_recap)
    for t in TASK_RECAP_EVENT_TYPES:
        assert t.name in src, f"{t.name} 在类型集里但折叠实现没读它"
    # 反向：折叠读到的 TASK_* 类型都必须在集合里
    declared = {t.name for t in TASK_RECAP_EVENT_TYPES}
    for line in src.splitlines():
        if "EventType.TASK_" in line:
            name = line.split("EventType.")[1].split(":")[0].split()[0].strip(" :")
            assert name in declared, f"折叠读了 {name}，但它不在 TASK_RECAP_EVENT_TYPES 里"


# ── 2. 无快照的全量重放：分批，且与整批等价 ────────────────────────────────


async def test_full_replay_is_batched_not_one_shot() -> None:
    """无快照时由 store 分多批产出，**一次全量读都不发**。

    连带钉住每批都不超过 `REPLAY_BATCH`：批大小失控就等于没分批（内存峰值又回到 O(n)）。
    """
    store = _CountingStore()
    await _seed(store, n_noise=_REPLAY_BATCH * 2 + 17)   # 跨 3 批，末批不满

    view = await rebuild_view(store, _SID)

    assert store.full_reads == 0, "分批路径不该再触发 read_by_session"
    assert len(store.batches) >= 3, f"应分多批，实际 {len(store.batches)} 批"
    assert max(store.batches) <= _REPLAY_BATCH, f"批大小失控：{store.batches}"
    assert sum(store.batches) == _REPLAY_BATCH * 2 + 21, "不重不漏：每条事件恰好折一次"
    assert view.session_id == _SID


async def test_batched_replay_equals_one_shot_replay() -> None:
    """分批与整批**逐字段等价**——重放是左折叠、可结合，这条是那个论证的可执行版本。

    不等价的话，恢复出来的状态会依赖「这次走了哪条路」，那种 bug 极难归因。
    """
    store = _CountingStore()
    await _seed(store, n_noise=_REPLAY_BATCH + 5)

    batched = await rebuild_view(store, _SID)
    one_shot = reduce_events(await store.read_by_session(_SID), run_id=_SID)

    assert batched.session_id == one_shot.session_id
    assert batched.events_total == one_shot.events_total
    assert sorted(batched.tasks) == sorted(one_shot.tasks)
    assert sorted(batched.agents) == sorted(one_shot.agents)
    assert batched.session_status == one_shot.session_status
    assert batched.task_status == one_shot.task_status


async def test_protocol_default_replay_is_one_shot_and_that_is_the_whole_story() -> None:
    """没覆盖 `replay` 的 store → 协议默认实现一次性读完，**不是** AttributeError、也不是
    core 里的某条降级分支。

    从前这里是两条用例，钉的是 core 的两种降级形态（继承了没覆盖 → `NotImplementedError`；
    鸭子类型没这个属性 → `AttributeError`）。那两种形态是那个坏设计的产物，不是世界的性质：
    分批归 store 之后，「不能分批」就只有一种表现——默认实现返回一整批。
    """
    class _NoBatch(_CountingStore):
        replay = EventStore.replay          # 退回协议默认实现

    store = _NoBatch()
    await _seed(store, n_noise=10)

    view = await rebuild_view(store, _SID)

    assert store.full_reads == 1, "默认实现就是读一次全量"
    assert view.session_id == _SID
    assert view.events_total == 14


async def test_default_replay_of_an_empty_session_yields_nothing() -> None:
    """空会话：默认实现不产出空批（省掉一次 `apply_events([])`）。"""
    class _NoBatch(_CountingStore):
        replay = EventStore.replay

    store = _NoBatch()

    view = await rebuild_view(store, "s_empty")

    assert view.tasks == {}
    assert view.events_total == 0
