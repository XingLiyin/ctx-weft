"""TaskManager pause 弃子控制面：abandon_pending / set_pause_abandon 守卫 / staged 丢弃。"""

import pytest

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc

pytestmark = pytest.mark.asyncio


class _StubRunner:
    async def assemble(self, task_id):
        return None

    async def execute(self, binding, task_id):
        return None


def _task(tid: str, status: str = "PENDING") -> Task:
    return Task(
        id=tid, session_id="s1", status=status, tenant_id="default",
        assigned_agent_id="", creator_agent_id="agr",
        title=tid, description="", user_prompt="x", created_at=now_utc(),
    )


def _tm_with_session() -> tuple[TaskManager, Session]:
    tm = TaskManager(session_id="s1")
    sess = Session(id="s1", tenant_id="default", user_prompt="x",
                   status="RUNNING", token_budget=0, root_agent_id="agr")
    tm.set_session(sess)
    tm.set_runner(_StubRunner())
    return tm, sess


async def test_abandon_pending_cancels_queue_without_touching_session():
    tm, sess = _tm_with_session()
    await tm.push_task(_task("t1"))
    await tm.push_task(_task("t2"))
    dropped = await tm.abandon_pending(reason="pause_abandon")
    assert set(dropped) == {"t1", "t2"}
    assert tm.get_task("t1").status == "CANCELED"
    assert tm.get_task("t2").status == "CANCELED"
    assert sess.status == "RUNNING"      # 弃子不动 session 状态
    assert tm.is_done() is True          # 队列已空、无在跑


async def test_pause_abandon_guard_keeps_session_status_on_cancel():
    tm, sess = _tm_with_session()
    tm.register_task(_task("t1", status="ACTIVE"))
    tm.set_pause_abandon(True)
    await tm.on_task_finished("t1", status="CANCELED")
    assert tm.get_task("t1").status == "CANCELED"
    assert sess.status != "CANCELED"     # pause 弃子 ≠ 用户取消


async def test_cancel_without_pause_abandon_still_cancels_session():
    tm, sess = _tm_with_session()
    tm.register_task(_task("t1", status="ACTIVE"))
    await tm.on_task_finished("t1", status="CANCELED")
    assert sess.status == "CANCELED"     # 既有语义不回归


async def test_flush_staged_dropped_under_pause_abandon():
    tm, _ = _tm_with_session()
    tm.register_task(_task("tp", status="ACTIVE"))
    tm.stage_task(_task("tc"), parent_task_id="tp")
    tm.set_pause_abandon(True)
    await tm._flush_staged("tp")
    assert tm.get_task("tc") is None     # 未入队、未登记（push 时才发 TASK_CREATED，无投影幽灵）
