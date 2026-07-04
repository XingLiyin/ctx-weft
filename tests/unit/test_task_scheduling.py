"""TaskManager 缓冲区调度的顺序不变量。

覆盖三条：
1. 同一批次（一次 run 内 stage 的多个子任务）pop 出来是 FIFO（投入顺序）。
2. 跨批次（更深一层 run 后 stage 的子任务）pop 出来是 LIFO（深度优先）。
3. runner 异常/取消退出时，未 flush 的缓冲被丢弃，不泄漏、不误入队。
"""

from __future__ import annotations

import asyncio

import pytest

from ctx_weft.core.events.types import EVENT_TYPES, EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Task


def _task(tid: str, parent: str | None = None) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", parent_task_id=parent)


class _CapturingBus:
    """最小事件总线：收集 emit 的事件，供断言。"""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


def _pop_all(tm: TaskManager) -> list[str]:
    """连续 pop（独立任务、无 blocked_by），返回出队顺序。"""
    order: list[str] = []
    while True:
        entry = tm._queue.pop()
        if entry is None:
            break
        order.append(entry.task_id)
    return order


async def test_fifo_within_batch() -> None:
    tm = TaskManager(session_id="s1")
    for tid in ("A", "B", "C"):
        tm.stage_task(_task(tid, parent="P"), parent_task_id="P")
    await tm._flush_staged("P")

    assert _pop_all(tm) == ["A", "B", "C"]


async def test_lifo_across_batches() -> None:
    tm = TaskManager(session_id="s1")
    # 第一批：P spawn A, B
    tm.stage_task(_task("A", parent="P"), parent_task_id="P")
    tm.stage_task(_task("B", parent="P"), parent_task_id="P")
    await tm._flush_staged("P")

    # 模拟 A 被 pop 运行
    first = tm._queue.pop()
    assert first is not None and first.task_id == "A"

    # 第二批：A spawn A1, A2（更深一层）
    tm.stage_task(_task("A1", parent="A"), parent_task_id="A")
    tm.stage_task(_task("A2", parent="A"), parent_task_id="A")
    await tm._flush_staged("A")

    # 深度优先：A 的子任务先于兄弟 B；同批次内仍 FIFO
    assert _pop_all(tm) == ["A1", "A2", "B"]


async def test_flush_only_affects_target_bucket() -> None:
    tm = TaskManager(session_id="s1")
    tm.stage_task(_task("A", parent="P1"), parent_task_id="P1")
    tm.stage_task(_task("B", parent="P2"), parent_task_id="P2")

    await tm._flush_staged("P1")
    assert _pop_all(tm) == ["A"]          # 只 flush 了 P1
    assert "P2" in tm._staged             # P2 的缓冲仍在

    await tm._flush_staged("P2")
    assert _pop_all(tm) == ["B"]


def test_event_type_str_compat() -> None:
    # StrEnum 成员即字符串：比较 / 序列化 / 白名单全部向后兼容。
    import json

    assert EventType.TASK_CREATED == "TaskCreated"
    assert json.dumps({"type": EventType.TASK_CREATED}) == '{"type": "TaskCreated"}'
    assert f"{EventType.TASK_STARTED}" == "TaskStarted"
    assert "TaskStarted" in EVENT_TYPES and EventType.TASK_STARTED in EVENT_TYPES


async def test_emit_rejects_unknown_event_type() -> None:
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)

    # 此前直接构造 Event 会绕过白名单；现在 _emit 也校验。
    with pytest.raises(ValueError):
        await tm._emit("NotARealEvent")  # type: ignore[arg-type]

    await tm._emit(EventType.TASK_RESUMED, task_id="x")
    assert [e.type for e in bus.events] == [EventType.TASK_RESUMED]


async def test_push_task_persists_before_run() -> None:
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    await tm.push_task(_task("A"))

    # 创建即落盘：尚未运行就有 TaskCreated，崩溃可恢复。
    assert [e.type for e in bus.events] == ["TaskCreated"]
    assert bus.events[0].task_id == "A"


async def test_run_task_started_via_runner_not_created() -> None:
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    parent = _task("P")
    tm.register_task(parent)

    async def runner(_session_id: str, task_id: str) -> None:
        # TASK_STARTED 由 runner 发（契约）——_run_task 不再自发以免双发；真实 _make_task_runner
        # 在 _resolve 后发，带 resolved agent id。这里镜像该契约。
        await tm._emit(EventType.TASK_STARTED, task_id=task_id, payload={"assigned_agent_id": "ag1"})

    tm.set_runner(runner)
    await tm._run_task("P")

    types = [e.type for e in bus.events]
    # 每次派发恰好一条 TaskStarted（由 runner），不发 TaskCreated（创建由 push_task 负责）
    assert types.count("TaskStarted") == 1
    assert "TaskCreated" not in types


def test_restore_rebuilds_blocked_chain() -> None:
    """崩溃恢复：父 SUSPENDED + 两个未跑子任务（带 dag_deps 链）应被原样重建。"""
    tm = TaskManager(session_id="s1")
    parent = Task(id="P", session_id="s1", status="SUSPENDED")
    a = Task(id="A", session_id="s1", status="PENDING", parent_task_id="P")
    b = Task(id="B", session_id="s1", status="PENDING", parent_task_id="P", dag_deps=["A"])

    tm.restore([parent, a, b], terminal_ids=set())

    # 父没有全部子任务终态 → 仍 SUSPENDED，不被误重跑
    assert tm.get_task("P").status == "SUSPENDED"
    # 依赖链从 dag_deps 重建：A 先出队，B 被 A 阻塞
    first = tm._queue.pop()
    assert first is not None and first.task_id == "A"
    assert tm._queue.pop() is None          # B 仍被阻塞
    tm._queue.mark_complete("A")
    second = tm._queue.pop()
    assert second is not None and second.task_id == "B"


async def test_run_layer_failure_emits_task_failed() -> None:
    """运行层失败（非 observer 判定，如 model 名写错）必须发 TaskFailed。

    否则任务在投影里停留 ACTIVE（TASK_STATUS_BY_EVENT 只认 TASK_* 事件），
    会被 restore 误当成可恢复任务复活重跑（且用 session 投影里的旧 model）。
    """
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(runner)
    t = _task("A")
    tm.register_task(t)

    class _NonRetriable(Exception):
        retriable = False

    await tm._handle_task_failure("A", error="unknown model", exc=_NonRetriable("boom"))

    failed = [e for e in bus.events if e.type == EventType.TASK_FAILED]
    assert failed, "run-layer failure must emit TaskFailed"
    assert failed[0].task_id == "A"
    assert t.status == "FAILED"


async def test_retry_emits_task_requeued() -> None:
    """可重试失败的重排分支也要发 TaskRequeued，使投影回 PENDING（而非停留 ACTIVE）。"""
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0  # drain 空转：不在本测试里真正重跑被重排的任务

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(runner)
    t = _task("A")
    tm.register_task(t)

    class _Retriable(Exception):
        retriable = True

    await tm._handle_task_failure("A", error="transient", exc=_Retriable("boom"))

    requeued = [e for e in bus.events if e.type == EventType.TASK_REQUEUED]
    assert requeued, "retry must emit TaskRequeued"
    assert requeued[0].task_id == "A"
    assert t.status == "PENDING"
    assert t.retry_count == 1


async def test_run_task_discards_staged_on_cancel() -> None:
    tm = TaskManager(session_id="s1")
    parent = _task("P")
    tm.register_task(parent)

    async def runner(_session_id: str, _task_id: str) -> None:
        tm.stage_task(_task("child", parent="P"), parent_task_id="P")
        raise asyncio.CancelledError()

    tm.set_runner(runner)

    with pytest.raises(asyncio.CancelledError):
        await tm._run_task("P")

    assert tm._staged == {}                # 缓冲被丢弃
    assert not tm._queue.has_pending()     # child 没有入队


async def test_run_task_flushes_staged_on_normal_return() -> None:
    tm = TaskManager(session_id="s1")
    parent = _task("P")
    tm.register_task(parent)

    async def runner(_session_id: str, task_id: str) -> None:
        # 仅 P 这一轮 spawn 子任务；drain 出来的 child 复用本 runner 时为 no-op，避免 resume 循环。
        if task_id == "P":
            tm.stage_task(_task("child", parent="P"), parent_task_id="P")

    tm.set_runner(runner)
    await tm._run_task("P")

    assert tm._staged == {}                 # P 的缓冲已 flush 清空
    assert tm.get_task("child") is not None  # child 已 flush 入队并登记


# ── detach_staged：finish_task 与 delegate 同批时把派发改投为独立后继 ────────────────


async def test_detach_staged_reparents_to_grandparent() -> None:
    """把 P 本轮 staged 的子任务改投到 P 的 parent(G)，flush 后归属记到 G。"""
    tm = TaskManager(session_id="s1")
    tm.stage_task(_task("A", parent="P"), parent_task_id="P")
    tm.stage_task(_task("B", parent="P"), parent_task_id="P")

    tm.detach_staged("P", "G")
    assert "P" in tm._staged                 # 不搬桶：flush key 仍是当前运行 task 的 id

    await tm._flush_staged("P")
    assert tm.get_task("A").parent_task_id == "G"
    assert tm.get_task("B").parent_task_id == "G"
    assert tm._parent_map["A"] == "G" and tm._parent_map["B"] == "G"
    assert tm._children_of["G"] == {"A", "B"}
    assert "P" not in tm._children_of        # 不再是收尾 task 的子任务


async def test_detach_staged_to_root_makes_independent() -> None:
    """当前 task 是 root（parent=None）→ 改投任务成独立顶层 root。"""
    tm = TaskManager(session_id="s1")
    tm.stage_task(_task("A", parent="P"), parent_task_id="P")

    tm.detach_staged("P", None)
    await tm._flush_staged("P")

    assert tm.get_task("A").parent_task_id is None
    assert "A" not in tm._parent_map         # 无父 → 独立 root


async def test_detach_staged_preserves_blocked_chain() -> None:
    """delegate_plan 改投后，兄弟间 blocked_by 顺序链原样保留，只是父变了。"""
    tm = TaskManager(session_id="s1")
    tm.stage_task(_task("A", parent="P"), parent_task_id="P")
    tm.stage_task(_task("B", parent="P"), parent_task_id="P", blocked_by=["A"])

    tm.detach_staged("P", "G")
    await tm._flush_staged("P")

    first = tm._queue.pop()
    assert first is not None and first.task_id == "A"
    assert tm._queue.pop() is None           # B 仍被 A 阻塞
    tm._queue.mark_complete("A")
    second = tm._queue.pop()
    assert second is not None and second.task_id == "B"
    assert tm._parent_map["B"] == "G"


def test_detach_staged_noop_when_no_bucket() -> None:
    tm = TaskManager(session_id="s1")
    tm.detach_staged("P", "G")               # 没有缓冲：静默 no-op，不抛
    assert tm._staged == {}
