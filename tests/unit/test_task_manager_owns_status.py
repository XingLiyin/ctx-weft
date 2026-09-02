"""task 状态与状态事件只归 TaskManager（Task 4）。

两条静态守卫 + 一组行为测试：
- 守卫 A：loop 侧（steps/ 与 runtime.py）一条 task 状态事件都不许发。
  **按 AST 扫 `EventType.TASK_*` 的属性访问**，而不是按行匹配 `emit(`——真实代码里
  emit 调用经常跨行（`await bus.emit(make_event(\n state, EventType.TASK_CANCELED, ...))`），
  按行匹配会漏掉，守卫就成了摆设。注释里的字面量不进 AST，天然不误报。
- 守卫 B：判决归 loop、状态归 TM——loop 侧不许出现 `task.status` 的赋值。
- 行为：五种 run 结局（含崩溃就地构造那一条）经同一张处置表落到状态 + 事件。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ctx_weft.core.orchestrator.task_disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_runner import AgentBinding, effective_agent_id
from ctx_weft.core.state.models import Session, Task
from ctx_weft.protocols.events import EventType

TASK_STATUS_EVENTS = (
    "TASK_FINISHED", "TASK_FAILED", "TASK_REQUEUED", "TASK_SUSPENDED",
    "TASK_AWAITING_HUMAN", "TASK_INTERRUPTED", "TASK_CANCELED",
)

#: 允许出现 `EventType.TASK_*` 的文件：唯一发射者 + 事件表 + 投影映射表。
_ALLOWED = {"task_manager.py", "events.py", "reducers.py"}


def test_only_task_manager_emits_task_status_events() -> None:
    """loop 侧（steps/ 与 runtime.py）一条 task 状态事件都不许发。"""
    assert _task_status_event_sites(pathlib.Path("src/ctx_weft")) == []


def _task_status_event_sites(root: pathlib.Path) -> list[str]:
    """AST 扫描：非白名单文件里所有 `EventType.<TASK_状态事件>` 的出现点。

    比「同一行里同时有 emit 和 EventType.X」强：跨行的 emit 调用、藏在 helper
    里的构造、`payload=` 后面才换行的写法，一个都跑不掉。
    """
    offenders: list[str] = []
    for p in sorted(root.rglob("*.py")):
        if p.name in _ALLOWED:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in TASK_STATUS_EVENTS
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "EventType"):
                offenders.append(f"{p}:{node.lineno}:{node.attr}")
    return offenders


def test_loop_side_does_not_write_task_status() -> None:
    """判决归 loop，状态归 TM——loop 侧不许出现 task.status 的赋值。"""
    targets = [
        "src/ctx_weft/core/loop/steps/observe.py",
        "src/ctx_weft/core/loop/steps/finalize.py",
        "src/ctx_weft/core/loop/steps/act.py",
        "src/ctx_weft/core/loop/steps/suspend.py",
        "src/ctx_weft/core/orchestrator/control_capability.py",
        "src/ctx_weft/core/runtime.py",
    ]
    offenders = [f"{t}:{lineno}" for t in targets
                 for lineno in _task_status_writes(pathlib.Path(t))]
    assert offenders == []


def _task_status_writes(path: pathlib.Path) -> list[int]:
    """AST 扫描：对任何 `<...>.task.status` 的赋值（含增量赋值）。

    `runtime.py` 里 TM 之外的模块也在此列——`_run_loop` 的 except 链正是被搬走的
    那批写入。读（`if task.status != ...`）不算，只抓写。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []

    def _is_task_status(n: ast.expr) -> bool:
        return (isinstance(n, ast.Attribute) and n.attr == "status"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "task")

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        hits += [node.lineno for t in targets if _is_task_status(t)]
    return sorted(hits)


# ── 行为：处置表在 TM 落地 ────────────────────────────────────────────────────


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


class _OutcomeRunner:
    """execute 直接交回一个 RunOutcome（或抛异常，走崩溃入口）。"""

    def __init__(self, tm: TaskManager, outcome: RunOutcome | None = None,
                 exc: BaseException | None = None) -> None:
        self._tm = tm
        self._outcome = outcome
        self._exc = exc

    async def assemble(self, task_id: str) -> AgentBinding | None:
        t = self._tm.get_task(task_id)
        if t is None:
            return None
        root = self._tm.session.root_agent_id if self._tm.session else ""
        return AgentBinding(agent_id=effective_agent_id(t, root))

    async def execute(self, binding: AgentBinding, task_id: str) -> RunOutcome | None:
        if self._exc is not None:
            raise self._exc
        return self._outcome


def _setup(bus: _CapturingBus, **task_kw) -> tuple[TaskManager, Task]:
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING"))
    t = Task(id="A", session_id="s1", status="PENDING", **task_kw)
    tm.register_task(t)
    return tm, t


async def _run(tm: TaskManager) -> None:
    await tm._run_task("A")


def _of(bus: _CapturingBus, et: EventType):
    return next(e for e in bus.events if e.type == et)


async def test_retry_exhausted_now_decided_by_task_manager() -> None:
    """重试耗尽的降级判断从 FinalizeStep 搬到了 TM，结果必须一模一样。"""
    bus = _CapturingBus()
    tm, t = _setup(bus, retry_count=3, max_retries=3)
    tm.set_runner(_OutcomeRunner(tm, RunOutcome(
        kind=RunOutcomeKind.COMPLETED, verdict="retry", error="stuck")))

    await _run(tm)

    ev = _of(bus, EventType.TASK_FAILED)
    assert ev.payload["error_code"] == "TASK_FAILED_RETRY_EXHAUSTED"
    assert ev.payload["error_message"] == "stuck"
    assert t.status == "FAILED"


async def test_retry_within_budget_writes_back_retry_count() -> None:
    """处置表不 mutate；新的 retry_count 必须由 TM 写回 task，否则预算永不消耗。"""
    bus = _CapturingBus()
    tm, t = _setup(bus, retry_count=1, max_retries=3)
    tm._max_concurrent = 0                       # drain 空转，不再次派发
    tm.set_runner(_OutcomeRunner(tm, RunOutcome(
        kind=RunOutcomeKind.COMPLETED, verdict="retry", summary="again")))

    await _run(tm)

    ev = _of(bus, EventType.TASK_REQUEUED)
    assert ev.payload["retry_count"] == 2
    assert t.retry_count == 2                    # ← 写回
    assert t.status == "PENDING"


async def test_success_finishes_from_task_manager() -> None:
    bus = _CapturingBus()
    tm, t = _setup(bus)
    tm.set_runner(_OutcomeRunner(tm, RunOutcome(
        kind=RunOutcomeKind.COMPLETED, verdict="success", summary="done", outputs="x")))

    await _run(tm)

    ev = _of(bus, EventType.TASK_FINISHED)
    assert ev.payload == {"outcome": "success", "summary": "done", "outputs": "x"}
    assert ev.run_id is None                     # TM 在 run 外面
    assert t.status == "FINISHED"


async def test_awaiting_human_and_suspended_come_from_task_manager() -> None:
    bus = _CapturingBus()
    tm, t = _setup(bus)
    tm.set_runner(_OutcomeRunner(tm, RunOutcome(
        kind=RunOutcomeKind.AWAITING_HUMAN, hitl_id="h1")))
    await _run(tm)
    assert _of(bus, EventType.TASK_AWAITING_HUMAN).payload == {"hitl_id": "h1"}
    assert t.status == "AWAITING_HUMAN"

    bus2 = _CapturingBus()
    tm2, t2 = _setup(bus2)
    tm2.set_runner(_OutcomeRunner(tm2, RunOutcome(
        kind=RunOutcomeKind.SUSPENDED_ON_CHILDREN, summary="s", spawn_titles=("a",))))
    await _run(tm2)
    assert _of(bus2, EventType.TASK_SUSPENDED).payload == {
        "summary": "s", "spawn_titles": ["a"]}
    assert t2.status == "SUSPENDED"


async def test_cancel_comes_from_task_manager_and_payload_is_empty() -> None:
    """取消走正常返回（CancelledError 在 _run_loop 里被吞、不重抛），并入处置表。"""
    bus = _CapturingBus()
    tm, t = _setup(bus)
    tm.set_runner(_OutcomeRunner(tm, RunOutcome(kind=RunOutcomeKind.CANCELED)))

    await _run(tm)

    assert _of(bus, EventType.TASK_CANCELED).payload == {}
    assert t.status == "CANCELED"


async def test_crash_entry_builds_outcome_in_place() -> None:
    """崩溃：`_run_loop` 重抛 → `_run_task` 的 except 就地构造 RunOutcome 喂同一张表。"""

    class _Boom(Exception):
        retriable = False

    bus = _CapturingBus()
    tm, t = _setup(bus, retry_count=0, max_retries=3)
    tm.set_runner(_OutcomeRunner(tm, exc=_Boom("kaboom")))

    await _run(tm)

    ev = _of(bus, EventType.TASK_INTERRUPTED)
    assert ev.payload["reason"] == "run_crash"
    assert ev.payload["error_message"] == "kaboom"
    assert ev.payload["retry_count"] == 0
    assert t.status == "INTERRUPTED"
    assert t.error == "kaboom"


async def test_crash_retriable_requeues_and_consumes_budget() -> None:
    bus = _CapturingBus()
    tm, t = _setup(bus, retry_count=0, max_retries=2)
    tm._max_concurrent = 0
    tm.set_runner(_OutcomeRunner(tm, exc=RuntimeError("transient")))

    await _run(tm)

    ev = _of(bus, EventType.TASK_REQUEUED)
    assert ev.payload == {"reason": "run_crash", "retry_count": 1}
    assert t.retry_count == 1
    assert t.status == "PENDING"


async def test_tm_terminal_status_wins_over_run_outcome() -> None:
    """熔断已把 root 判死（TM 自己写的终态）→ run 的取消结局不得把 FAILED 盖回 CANCELED。"""
    bus = _CapturingBus()
    tm, t = _setup(bus)

    class _TripRunner(_OutcomeRunner):
        async def execute(self, binding, task_id):  # noqa: ANN001, ANN201
            self._tm.get_task(task_id).status = "FAILED"      # 熔断 trip 的先手
            return RunOutcome(kind=RunOutcomeKind.CANCELED)

    tm.set_runner(_TripRunner(tm))
    await _run(tm)

    assert EventType.TASK_CANCELED not in [e.type for e in bus.events]
    assert t.status == "FAILED"


@pytest.mark.parametrize("kind", list(RunOutcomeKind))
def test_every_run_outcome_kind_is_handled(kind: RunOutcomeKind) -> None:
    """处置表对五个结局都给得出状态——新增结局忘了接会在这里红。"""
    from ctx_weft.core.orchestrator.task_disposition import disposition_for

    disp = disposition_for(RunOutcome(kind=kind), retry_count=0, max_retries=3)
    assert disp.status and disp.event_type
