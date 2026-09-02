"""`RunInterrupted`（run 域）与 `TaskInterrupted`（task 域）分家。

两条约束：

1. **run 域的事实只从 run 里发**。`TaskManager` 不拥有 run（连 `run_id` 都拿不到），
   所以它的挂起收尾发的是 task 域的 `TaskInterrupted`，不再发 `RunInterrupted`。
2. **`TaskInterrupted` 发在重试判定之后**：崩溃后还能重试的 task 走 `TaskRequeued`
   （→ `PENDING`），中间不得先被打成 `INTERRUPTED` 再翻回 `PENDING`。

判据一律是事件类型；`reason` 只作溯源。
"""

from __future__ import annotations

from ctx_weft.core.control.reducers import TASK_STATUS_BY_EVENT, reduce_events
from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols.events import Event, EventType
from tests.unit._stub_runner import StubRunner

# 实现里还没有这个枚举值时也要能跑出**行为**失败，故用字面量。
TASK_INTERRUPTED = "TaskInterrupted"


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


class _Retriable(Exception):
    retriable = True


class _NonRetriable(Exception):
    retriable = False
    code = "LLM_AUTH_FAILED"


def _tm(bus: _CapturingBus) -> tuple[TaskManager, Session, Task]:
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0  # drain 空转：重排后不真正重跑
    session = Session(id="s1", user_prompt="", status="RUNNING")
    tm.set_session(session)
    tm.set_runner(StubRunner(tm))
    t = Task(id="A", session_id="s1", status="ACTIVE")
    tm.register_task(t)
    return tm, session, t


def _types(bus: _CapturingBus) -> list:
    return [e.type for e in bus.events]


async def test_retriable_crash_requeues_without_task_interrupted() -> None:
    """崩溃 + 还有重试额度 → `TaskRequeued`，**没有** `TaskInterrupted`，终态 `PENDING`。

    这是今天就存在的错：先 `INTERRUPTED` 再 `PENDING`，中间那一下是假的。
    """
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)

    await tm._handle_task_failure(
        "A", reason="assembly_failure", error="transient", exc=_Retriable("boom"),
    )

    assert EventType.TASK_REQUEUED in _types(bus)
    assert TASK_INTERRUPTED not in _types(bus)
    assert t.status == "PENDING"


async def test_task_manager_emits_task_interrupted_not_run_interrupted() -> None:
    """TM 在 run 外面，发的是 task 域的事实。溯源字段照抄旧 `RunInterrupted` 的四个。"""
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)

    await tm._handle_task_failure(
        "A", reason="assembly_failure", error="401 unauthorized", exc=_NonRetriable("boom"),
    )

    assert EventType.RUN_INTERRUPTED not in _types(bus)
    interrupted = [e for e in bus.events if e.type == TASK_INTERRUPTED]
    assert interrupted and interrupted[0].task_id == "A"
    p = interrupted[0].payload
    assert p["reason"] == "run_crash"
    assert p["error_code"] == "LLM_AUTH_FAILED"
    assert p["error_message"] == "401 unauthorized"
    assert p["retry_count"] == 0
    assert t.status == "INTERRUPTED"


async def test_retry_exhausted_falls_through_to_task_interrupted() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    t.retry_count = t.max_retries

    await tm._handle_task_failure(
        "A", reason="assembly_failure", error="transient", exc=_Retriable("boom"),
    )

    assert EventType.TASK_REQUEUED not in _types(bus)
    assert TASK_INTERRUPTED in _types(bus)
    assert t.status == "INTERRUPTED"
    # 聚合读的是内存 status，与改动前同口径
    queue_sig = [e for e in bus.events if e.type == EventType.TASK_QUEUE_INTERRUPTED]
    assert queue_sig


async def test_context_overflow_still_reaches_task_interrupted() -> None:
    bus = _CapturingBus()
    tm, _session, t = _tm(bus)
    exc = ContextOverflowError(context_limit=100_000, required=171_808,
                               effective_limit=92_000, reserved_output_tokens=8_000)

    await tm._handle_task_failure(
        "A", reason="assembly_failure", error=str(exc), exc=exc,
    )

    interrupted = [e for e in bus.events if e.type == TASK_INTERRUPTED]
    assert interrupted and interrupted[0].payload["error_code"] == "CONTEXT_OVERFLOW"
    assert t.status == "INTERRUPTED"


def _ev(t: str, payload: dict, seq: int) -> Event:
    return Event(id=f"evt_{seq:04d}", run_id="run_1", sequence=seq, session_id="s1",
                 type=t, timestamp=now_utc(), task_id="tsk_1", payload=payload)


def test_task_status_mapping_moves_to_task_interrupted() -> None:
    assert EventType.RUN_INTERRUPTED not in TASK_STATUS_BY_EVENT
    assert TASK_STATUS_BY_EVENT[TASK_INTERRUPTED] == "INTERRUPTED"


def test_run_interrupted_alone_does_not_write_task_status() -> None:
    """run 域的事实不写 task 状态——写的是随后的 `TaskInterrupted`。"""
    events = [
        _ev(EventType.TASK_CREATED, {"task": {"id": "tsk_1", "session_id": "s1",
                                              "status": "PENDING"}}, 1),
        _ev(EventType.TASK_STARTED, {"assigned_agent_id": "agt_1"}, 2),
        _ev(EventType.RUN_INTERRUPTED, {"reason": "run_crash"}, 3),
    ]
    view = reduce_events(events, run_id="run_1")
    assert view.tasks["tsk_1"].status == "ACTIVE"

    view2 = reduce_events(
        [*events, _ev(TASK_INTERRUPTED, {"reason": "run_crash"}, 4)], run_id="run_1")
    assert view2.tasks["tsk_1"].status == "INTERRUPTED"
