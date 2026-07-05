"""同 agent 不并发（调度层串行）+ resume-parent 原子化。

覆盖：
1. TaskQueue.pop 的 skip 谓词：命中的条目留在队列，不被弹出。
2. TaskManager._effective_agent：subagent → 每任务唯一；非 subagent → root_agent_id。
3. drain 的 agent-busy 闸：两个同 agent（非 subagent）任务不并发；不同 subagent 仍并行。
4. _try_resume_parent：全部子任务终态才 resume；空子任务集不 resume（vacuous 守卫）；
   父已 ACTIVE 不二次 resume（原子性回归）。
"""

from __future__ import annotations

import asyncio

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.orchestrator.task_queue import QueueEntry, TaskQueue
from ctx_weft.core.state.models import NormalTaskSettings, Session, Task
from tests.unit._stub_runner import StubRunner


def _session(root_agent_id: str) -> Session:
    return Session(id="s1", user_prompt="", status="RUNNING", root_agent_id=root_agent_id)


def _task(tid: str, parent: str | None = None) -> Task:
    """非 subagent 任务 → 有效 agent = root_agent_id。"""
    return Task(id=tid, session_id="s1", status="PENDING", parent_task_id=parent)


def _subagent_task(tid: str, parent: str | None = None) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING", parent_task_id=parent,
                settings=NormalTaskSettings(use_subagent=True))


# ── 1) TaskQueue.pop skip 谓词 ────────────────────────────────────────────────


def test_pop_skip_leaves_entry_in_queue() -> None:
    q = TaskQueue()
    q.push(QueueEntry(task_id="A", session_id="s1"))
    q.push(QueueEntry(task_id="B", session_id="s1"))  # LIFO：B 在栈顶

    # 跳过 B → 扫描到 A 返回；B 仍留在队列
    e = q.pop(skip=lambda en: en.task_id == "B")
    assert e is not None and e.task_id == "A"
    assert q.has_pending()  # B 还在

    e2 = q.pop()
    assert e2 is not None and e2.task_id == "B"
    assert q.pop() is None


def test_pop_skip_all_returns_none() -> None:
    q = TaskQueue()
    q.push(QueueEntry(task_id="A", session_id="s1"))
    assert q.pop(skip=lambda _e: True) is None
    assert q.has_pending()  # 未被弹出


# ── 2) _effective_agent ───────────────────────────────────────────────────────


def test_effective_agent_non_subagent_uses_root() -> None:
    tm = TaskManager(session_id="s1")
    tm.set_session(_session("root"))
    assert tm._effective_agent(_task("A")) == "root"


def test_effective_agent_assigned_overrides_root() -> None:
    tm = TaskManager(session_id="s1")
    tm.set_session(_session("root"))
    t = _task("A")
    t.assigned_agent_id = "agtX"
    assert tm._effective_agent(t) == "agtX"


def test_effective_agent_subagent_is_unique_token() -> None:
    tm = TaskManager(session_id="s1")
    tm.set_session(_session("root"))
    # 未 assigned 的 subagent → 每任务唯一 token（互不冲突）
    assert tm._effective_agent(_subagent_task("A")) == "__sub__A"
    assert tm._effective_agent(_subagent_task("B")) == "__sub__B"


def test_effective_agent_none_is_empty() -> None:
    tm = TaskManager(session_id="s1")
    assert tm._effective_agent(None) == ""


# ── 3) drain agent-busy 闸 ────────────────────────────────────────────────────


async def test_same_agent_tasks_do_not_run_concurrently() -> None:
    tm = TaskManager(session_id="s1", max_concurrent=4)
    tm.set_session(_session("root"))

    start = {t: asyncio.Event() for t in ("A", "B")}
    release = {t: asyncio.Event() for t in ("A", "B")}

    async def runner(_sid: str, tid: str) -> None:
        start[tid].set()
        await release[tid].wait()

    tm.set_runner(StubRunner(tm, runner))
    await tm.push_task(_task("A"))
    await tm.push_task(_task("B"))

    await tm.drain()
    # 只有一个会被调度（另一个的有效 agent = root，正忙 → 被 skip、留在队列）
    done, _pending = await asyncio.wait(
        [asyncio.ensure_future(start["A"].wait()), asyncio.ensure_future(start["B"].wait())],
        return_when=asyncio.FIRST_COMPLETED, timeout=1,
    )
    assert done, "至少一个同 agent 任务应开始"
    await asyncio.sleep(0)  # 给"若会启动的另一个"一个机会
    n_started = sum(1 for t in ("A", "B") if start[t].is_set())
    assert n_started == 1, "同 agent 任务不得并发"

    first = "A" if start["A"].is_set() else "B"
    other = "B" if first == "A" else "A"

    # 放行第一个 → 完成 → on_task_finished → drain → 另一个才启动
    release[first].set()
    await asyncio.wait_for(start[other].wait(), timeout=1)
    assert start[other].is_set()

    release[other].set()
    await asyncio.sleep(0)


async def test_distinct_subagents_run_in_parallel() -> None:
    tm = TaskManager(session_id="s1", max_concurrent=4)
    tm.set_session(_session("root"))

    start = {t: asyncio.Event() for t in ("A", "B")}
    release = {t: asyncio.Event() for t in ("A", "B")}

    async def runner(_sid: str, tid: str) -> None:
        start[tid].set()
        await release[tid].wait()

    tm.set_runner(StubRunner(tm, runner))
    await tm.push_task(_subagent_task("A"))
    await tm.push_task(_subagent_task("B"))

    await tm.drain()
    # 不同 subagent 有效 agent 各异 → 两者都应启动
    await asyncio.wait_for(
        asyncio.gather(start["A"].wait(), start["B"].wait()), timeout=1,
    )
    assert start["A"].is_set() and start["B"].is_set()

    release["A"].set()
    release["B"].set()
    await asyncio.sleep(0)


# ── 4) _try_resume_parent ─────────────────────────────────────────────────────


async def test_resume_parent_when_all_children_terminal() -> None:
    tm = TaskManager(session_id="s1", max_concurrent=0)  # drain 空转：不真正重跑父
    parent = Task(id="P", session_id="s1", status="SUSPENDED")
    a = Task(id="A", session_id="s1", status="FINISHED", parent_task_id="P")
    b = Task(id="B", session_id="s1", status="FINISHED", parent_task_id="P")
    for t in (parent, a, b):
        tm.register_task(t)
    tm._parent_map.update({"A": "P", "B": "P"})
    tm._children_of["P"] = {"A", "B"}

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(StubRunner(tm, runner))
    await tm._try_resume_parent("B")

    assert parent.status == "ACTIVE"
    assert tm._queue.has_pending()  # 父已重新入队


async def test_resume_parent_blocked_by_running_sibling() -> None:
    tm = TaskManager(session_id="s1", max_concurrent=0)
    parent = Task(id="P", session_id="s1", status="SUSPENDED")
    a = Task(id="A", session_id="s1", status="FINISHED", parent_task_id="P")
    b = Task(id="B", session_id="s1", status="ACTIVE", parent_task_id="P")  # 兄弟仍在跑
    for t in (parent, a, b):
        tm.register_task(t)
    tm._parent_map.update({"A": "P", "B": "P"})
    tm._children_of["P"] = {"A", "B"}

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(StubRunner(tm, runner))
    await tm._try_resume_parent("A")

    assert parent.status == "SUSPENDED", "有子任务在跑时父不得 resume"
    assert not tm._queue.has_pending()


async def test_resume_parent_empty_children_set_no_resume() -> None:
    """vacuous 守卫：_children_of[P] 空时绝不 resume（all([]) 恒 True 的防御）。"""
    tm = TaskManager(session_id="s1", max_concurrent=0)
    parent = Task(id="P", session_id="s1", status="SUSPENDED")
    tm.register_task(parent)
    tm._parent_map["C"] = "P"  # 有父映射，但 _children_of[P] 未登记

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(StubRunner(tm, runner))
    await tm._try_resume_parent("C")

    assert parent.status == "SUSPENDED"
    assert not tm._queue.has_pending()


async def test_resume_parent_not_double_resumed() -> None:
    """两个子任务先后触发 resume：父翻成 ACTIVE 后第二次不得再入队（原子性 + 状态守卫）。"""
    tm = TaskManager(session_id="s1", max_concurrent=0)
    parent = Task(id="P", session_id="s1", status="SUSPENDED")
    a = Task(id="A", session_id="s1", status="FINISHED", parent_task_id="P")
    b = Task(id="B", session_id="s1", status="FINISHED", parent_task_id="P")
    for t in (parent, a, b):
        tm.register_task(t)
    tm._parent_map.update({"A": "P", "B": "P"})
    tm._children_of["P"] = {"A", "B"}

    async def runner(_s: str, _t: str) -> None:
        pass

    tm.set_runner(StubRunner(tm, runner))
    await tm._try_resume_parent("A")
    assert parent.status == "ACTIVE"
    n_after_first = tm._queue.pending_count()

    await tm._try_resume_parent("B")  # 父已 ACTIVE → 不再入队
    assert tm._queue.pending_count() == n_after_first
