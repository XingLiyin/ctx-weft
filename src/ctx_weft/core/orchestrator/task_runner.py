"""两阶段 TaskRunner 契约：装配（assemble）与执行（execute）分离。

TaskManager 在派发点先调 assemble 拿到 AgentBinding（TM 据此回填
assigned_agent_id、发 TASK_STARTED、登记同 agent 串行键），再调 execute
驱动 step loop。spec: docs/superpowers/specs/2026-07-04-two-phase-dispatch-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ctx_weft.core.state.models import NormalTaskSettings

if TYPE_CHECKING:
    from ctx_weft.core.orchestrator.agent_registry import ResolvedModel
    from ctx_weft.core.orchestrator.task_disposition import RunOutcome
    from ctx_weft.core.state.models import Task


@dataclass
class AgentBinding:
    """assemble 的产物：本次派发的执行 agent 绑定。

    TaskManager 只读 agent_id；agent/template/initial_step/run_id/model 对 TM
    不透明，由 execute 消费。model：assemble 期 `AgentRegistry.materialize` 顺带
    解出的这次要用的 LLM（client + 身份 + 窗口，批次 B）——execute 直接用它，
    不再自己解析。
    """

    agent_id: str
    agent: Any = None
    template: Any = None
    initial_step: str = "prepare"
    run_id: str = ""
    model: "ResolvedModel | None" = None


class TaskRunner(Protocol):
    """两阶段 runner 协议。assemble 返回 None 表示 task 已不存在（装配空转）。"""

    async def assemble(self, task_id: str) -> "AgentBinding | None": ...

    async def execute(
        self, binding: "AgentBinding", task_id: str,
    ) -> "RunOutcome | None":
        """驱动一次 run，交回它的**结局**（不是 task 状态）。

        返回值是 TaskManager 消费处置的**唯一数据通道**（Task 4）：不订阅 run 事件，
        因为 in-process bus 在 `emit()` 内同步 drain——TM 拿到处置后还要动队列、
        `drain()` 会派下一个 task、开新 run、发更多事件，全嵌套在最初那次 `emit()`
        里层层重入。数据走返回值，TM 在 run 返回**之后**动手。
        None = 这次执行没留下结局，由 TM 按 COMPLETED/success 兜底；崩溃则是重抛，
        由 TM 的 `except Exception` 就地构造 RunOutcome。
        """
        ...


def effective_agent_id(task: "Task | None", root_agent_id: str) -> str:
    """任务实际执行所在 agent scope 的单一真相——调度串行键与装配共用。

    - subagent 任务：每次实例化独立 agent；assigned 未定时用 task.id 造唯一
      占位 token（只作串行键，永不碰撞）。
    - 其余：assigned or creator or root——非 subagent 任务在创建者的 agent
      scope 上跑（延续创建者对话）；三者皆空退 per-task token，避免把
      「未知 agent」误并成一桶而过度串行。
    """
    if task is None:
        return ""
    s = task.settings
    if isinstance(s, NormalTaskSettings) and s.use_subagent:
        return task.assigned_agent_id or f"__sub__{task.id}"
    return (
        task.assigned_agent_id
        or task.creator_agent_id
        or root_agent_id
        or f"__task__{task.id}"
    )
