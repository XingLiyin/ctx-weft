"""两阶段派发契约：TASK_STARTED 归 TM 单发、装配失败标注且不发 TASK_STARTED、
在跑任务串行键用装配的真实 agent id。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ctx_weft.protocols.events import Event, EventType
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_runner import AgentBinding
from ctx_weft.core.domain.models import Session, Task

from tests.unit._stub_runner import StubRunner


@dataclass
class _CapturingBus:
    events: list = field(default_factory=list)

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _task(tid: str, parent: str | None = None) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", parent_task_id=parent)


def _session(root: str = "root") -> Session:
    return Session(id="s1", user_prompt="", status="RUNNING", root_agent_id=root)


async def test_task_started_emitted_once_by_tm_with_agent_id() -> None:
    """每次派发恰好一条 TaskStarted，由 TM 发、payload 带装配的 agent id（99edd41 双发不回归）。"""
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm.set_session(_session())
    t = _task("A")
    tm.register_task(t)
    tm.set_runner(StubRunner(tm))

    await tm._run_task("A")

    started = [e for e in bus.events if e.type == EventType.TASK_STARTED]
    assert len(started) == 1
    assert started[0].task_id == "A"
    assert started[0].payload["assigned_agent_id"] == "root"
    assert t.assigned_agent_id == "root"          # TM 回填
    assert t.started_at is not None


async def test_assembly_failure_no_task_started_and_labeled_requeue() -> None:
    """装配抛错：不发 TaskStarted（无幽灵 ACTIVE），走重试且 reason=assembly_failure。"""
    bus = _CapturingBus()
    tm = TaskManager(session_id="s1", event_bus=bus)
    tm._max_concurrent = 0            # drain 空转，不真正重跑
    tm.set_session(_session())
    tm.register_task(_task("A"))

    class _Boom(StubRunner):
        async def assemble(self, task_id: str):
            raise RuntimeError("template gone")

    tm.set_runner(_Boom(tm))
    await tm._run_task("A")

    types = [e.type for e in bus.events]
    assert EventType.TASK_STARTED not in types
    requeued = [e for e in bus.events if e.type == EventType.TASK_REQUEUED]
    assert requeued and requeued[0].payload["reason"] == "assembly_failure"


async def test_running_serial_key_uses_real_binding_agent_id() -> None:
    """在跑任务的串行键 = 装配返回的真实 agent id，而非预测值。"""
    tm = TaskManager(session_id="s1", max_concurrent=4)
    tm.set_session(_session())
    release = asyncio.Event()

    class _RealId(StubRunner):
        async def assemble(self, task_id: str):
            return AgentBinding(agent_id="agt_real")

        async def execute(self, binding, task_id: str) -> None:
            await release.wait()

    tm.set_runner(_RealId(tm))
    await tm.push_task(_task("A"))
    await tm.drain()
    for _ in range(10):               # 让 _run_task 协程跑到 execute 阻塞点
        if "A" in tm._running_agents:
            break
        await asyncio.sleep(0)

    assert tm._running_agents.get("A") == "agt_real"
    release.set()
