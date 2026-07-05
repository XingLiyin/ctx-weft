"""pause_session 弃子语义：只留 root agent 当前那一轮（pause→park），其余 cancel/放弃。"""

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.state.models import Session, Task
from ctx_weft.core.utils import now_utc
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio


class _StubRunner:
    async def assemble(self, task_id):
        return None

    async def execute(self, binding, task_id):
        return None


def _rt():
    return CtxWeftRuntime(llm=MockLLMAdapter(responses=[]),
                          template_resolver=InMemoryTemplateResolver())


def _task(tid: str, status: str = "PENDING") -> Task:
    return Task(
        id=tid, session_id="s1", status=status, tenant_id="default",
        assigned_agent_id="", creator_agent_id="agr",
        title=tid, description="", user_prompt="x", created_at=now_utc(),
    )


def _wire(rt) -> tuple[TaskManager, Session]:
    tm = TaskManager(session_id="s1")
    sess = Session(id="s1", tenant_id="default", user_prompt="x",
                   status="RUNNING", token_budget=0, root_agent_id="agr")
    tm.set_session(sess)
    tm.set_runner(_StubRunner())
    rt._task_managers["s1"] = tm
    return tm, sess


async def test_pause_session_partitions_root_run_vs_rest():
    rt = _rt()
    tm, sess = _wire(rt)
    # 在跑两轮：root agent 的一轮 + 子 agent 的一轮（真实执行 agent 以 _running_agents 为准）
    for tid, agent in (("t_root", "agr"), ("t_sub", "ag_sub")):
        tm.register_task(_task(tid, status="ACTIVE"))
        tm._running_tasks.add(tid)
        tm._running_agents[tid] = agent
    root_tokens = rt._register_run_tokens("s1", "t_root")
    sub_tokens = rt._register_run_tokens("s1", "t_sub")
    await tm.push_task(_task("t_q"))     # 排队中一个

    assert await rt.pause_session("s1") is True
    # root agent 那一轮：pause → park；不 cancel
    assert root_tokens.pause.is_paused and not root_tokens.cancel.is_cancelled
    # 其余在途 run：cancel → 终态；不 pause
    assert sub_tokens.cancel.is_cancelled and not sub_tokens.pause.is_paused
    # 排队任务被放弃
    assert tm.get_task("t_q").status == "CANCELED"
    # 闩锁置位（等 root park 后 _on_idle 清除）、session 状态未被弃子污染
    assert "s1" in rt._pausing
    assert sess.status == "RUNNING"


async def test_pause_session_unknown_task_runs_are_cancelled():
    # 旧 TM inflight：registry 在册但当前 TM 的 _running_agents 不认识 → 按"非 root 那一轮"cancel
    rt = _rt()
    tm, _ = _wire(rt)
    tm.register_task(_task("t_keepalive", status="ACTIVE"))
    tm._running_tasks.add("t_keepalive")
    tm._running_agents["t_keepalive"] = "agr"
    rt._register_run_tokens("s1", "t_keepalive")
    stale = rt._register_run_tokens("s1", "t_stale")   # 旧代 run，当前 TM 不认识
    assert await rt.pause_session("s1") is True
    assert stale.cancel.is_cancelled is True


async def test_pause_session_idle_session_is_noop_false():
    rt = _rt()
    _wire(rt)   # TM 存在但无在跑、无排队 → is_done
    assert await rt.pause_session("s1") is False
    assert "s1" not in rt._pausing


async def test_pause_task_targets_single_run():
    # 定向暂停单个在途 task，其他 task 不受影响；task 不在跑→False
    rt = _rt()
    a = rt._register_run_tokens("s1", "ta")
    b = rt._register_run_tokens("s1", "tb")
    assert rt.pause_task("s1", "ta") is True
    assert a.pause.is_paused and not b.pause.is_paused
    assert rt.pause_task("s1", "nope") is False    # 不在跑 → False
