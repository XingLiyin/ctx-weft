"""两阶段 TaskRunner 的测试 stub：旧式 (session_id, task_id) 协程适配器。

assemble 用 effective_agent_id 镜像真实装配的串行键（预测==真实），
使 same-agent 串行语义在 stub 下与生产一致。
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from ctx_weft.core.orchestrator.task.disposition import RunOutcome
from ctx_weft.core.orchestrator.task.manager import TaskManager
from ctx_weft.core.orchestrator.task.runner import AgentBinding, effective_agent_id

ExecuteFn = Callable[[str, str], Coroutine[Any, Any, Any]]


class StubRunner:
    def __init__(self, tm: TaskManager, execute_fn: ExecuteFn | None = None,
                 session_id: str = "s1") -> None:
        self._tm = tm
        self._fn = execute_fn
        self._session_id = session_id

    async def assemble(self, task_id: str) -> AgentBinding | None:
        t = self._tm.get_task(task_id)
        if t is None:
            return None
        root = self._tm.session.root_agent_id if self._tm.session else ""
        return AgentBinding(agent_id=effective_agent_id(t, root))

    async def execute(self, binding: AgentBinding, task_id: str) -> "RunOutcome | None":
        """execute_fn 返回的 RunOutcome 原样交回 TaskManager（Task 4 的处置通道）。

        返回 None（含没有 execute_fn 的空转 stub）= 这次 run 没留下结局，TM 按
        COMPLETED/success 兜底——与旧 stub「跑完什么都不改」的效果一致。
        """
        if self._fn is not None:
            res = await self._fn(self._session_id, task_id)
            if isinstance(res, RunOutcome):
                return res
        return None
