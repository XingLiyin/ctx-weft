"""不变式 `unattended=False ⟹ port_key 非空`：要等对端，就得先有个口。

一个「要等应答、却没有任何口」的 task 是个死结——应答没有通道能回来，它会 park 到死。
校验只落在 `push_task`（唯一的新建路径），**刻意不落 `Task.__post_init__`**：崩溃重建
走 `restore`，那条路重建的是既有事实，在那里拦只会让恢复在坏数据上炸，而正确的做法是
把坏数据恢复出来、让它照常收敛。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.models.session import Session
from ctx_weft.core.models.task import PORT_MAIN, PORT_NONE, Task
from ctx_weft.core.orchestrator.task.manager import TaskManager


def _tm() -> TaskManager:
    tm = TaskManager(session_id="s1")
    tm.set_session(Session(id="s1", user_prompt="", status="RUNNING", root_agent_id="root"))
    return tm


def _task(**kw) -> Task:
    kw.setdefault("status", "PENDING")
    return Task(id="t1", session_id="s1", **kw)


async def test_push_task_rejects_attended_task_without_a_port() -> None:
    tm = _tm()
    with pytest.raises(ValueError, match="port_key"):
        await tm.push_task(_task(port_key=PORT_NONE, unattended=False))
    # 入口即拒、不落库：登记表与队列都不该留下半个 task。
    assert tm.get_task("t1") is None
    assert tm._queue.pending_count() == 0


@pytest.mark.parametrize("port_key,unattended", [
    (PORT_MAIN, False),   # 常规对话
    ("btw", False),       # 旁支交互线
    (PORT_MAIN, True),    # 接口但自治——产出推到主口，不参与往返
    (PORT_NONE, True),    # 完全自治
])
async def test_push_task_accepts_the_three_legal_combinations(port_key, unattended) -> None:
    tm = _tm()
    await tm.push_task(_task(port_key=port_key, unattended=unattended))
    assert tm.get_task("t1") is not None


def test_restore_does_not_enforce_the_invariant() -> None:
    """存量/坏数据照常恢复——`restore` 重建既有事实，不是新建路径。"""
    tm = _tm()
    tm.restore([_task(port_key=PORT_NONE, unattended=False, status="ACTIVE")],
               terminal_ids=set())
    t = tm.get_task("t1")
    assert t is not None and t.status == "PENDING"     # 已重排，没有抛
