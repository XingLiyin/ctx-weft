"""熔断真终结（failure threshold trip）的清场分类。**纯函数，不碰总线、不碰队列。**

与 `task_disposition.py` 同一范式：这里回答「哪个 task 该落哪一桶」，
`TaskManager._trip_failure_threshold` 负责「照办 + 按定死的 8 步顺序发事件」。

**顺序不在本模块**——它是那段代码真正的契约（HitlCancelled 必须全部先于会话终态；
root 必须先标 FAILED 再对在跑的 root 发协作取消，两道守卫才接得住），必须留在有
`await` 和 `emit` 的地方。本模块只消化**分类**，那是 144 行里占掉大半阅读负担的部分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctx_weft.core.domain.status import TERMINAL_TASK_STATUSES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence, Set as AbstractSet

    from ctx_weft.core.domain.models import Task

__all__ = ["TripPlan", "plan_threshold_trip"]


def _has_dispatch_frame(t: "Task") -> bool:
    """「已启动的子任务必有框」：框由 ensure_dispatch_frame_at_start 在 start 时铸。

    不看 `origin_tool_call_id`——它是瞬态字段，重启重建后为 None，拿它当条件会把跨
    重启的在途子任务误判成「无框」而漏掉 ack 替换（框其实在，靠 child_task_id 认）。
    """
    return bool(t.started_at and t.parent_task_id)


@dataclass(frozen=True)
class TripPlan:
    """清场分类的结果。每个字段是一组 task id，语义见字段注释。"""

    #: 清队命中的非 root 条目 → 标 CANCELED + 发 TASK_CANCELED。
    #: root 条目直接丢弃（它的去向是 `fail_roots`，不在清队步骤发事件）。
    cancel_queued: tuple[str, ...] = ()

    #: SUSPENDED 且非 root → 标 CANCELED + 发 TASK_CANCELED。
    cancel_suspended: tuple[str, ...] = ()

    #: 在途非 root → **只发协作取消信号，不发事件**。它的 TASK_CANCELED 由 run 结束
    #: 后的 `apply_run_outcome` 发；finish 对交由 `on_task_finished` 的取消胶囊闭合
    #: funnel 补写。
    signal_inflight: tuple[str, ...] = ()

    #: 在途且已启动带框 → 交 threshold_finalizer 做 eager ack 替换（幂等自愈）。
    #: 是 `signal_inflight` 的子集。
    ack_task_ids: tuple[str, ...] = ()

    #: 已被直接标 CANCELED（清队 + 挂起两批）且已启动 → 终态已坐实，交
    #: cancel_finalizer 立即整对闭合（ack + finish 对一次写完），不走 ack-only 半闭合。
    #: 未启动过的任务从未铸框 / 写过 memory，不入此列——零 memory 写。
    cancel_now_ids: tuple[str, ...] = ()

    #: 非终态的 root（`parent_task_id is None`）→ 标 FAILED + 发 TASK_FAILED。
    #: 已终态的 root（自己就是第 N 败，或时序尾巴已 FINISHED）不改状态、不发事件。
    fail_roots: tuple[str, ...] = ()

    #: 在途的 root（`fail_roots` 的子集）→ **标 FAILED 之后**再发协作取消信号。
    signal_roots: tuple[str, ...] = ()


def plan_threshold_trip(
    tasks: "Mapping[str, Task]",
    *,
    pending_ids: "Sequence[str]",
    running_ids: "AbstractSet[str]",
) -> TripPlan:
    """把会话里的全部 task 分类成清场动作。

    ``pending_ids``：刚从队列 drain 出来的排队条目（调用方已持锁取出）。
    ``running_ids``：当前在途（已派发、`_run_task` 未返回）的 task id。

    `cancel_now_ids` 的追加顺序是「先清队批、后挂起批」，与改造前逐字一致——
    finalizer 按列表顺序写 memory，顺序变了会改变落盘次序。
    """
    cancel_queued: list[str] = []
    cancel_now: list[str] = []
    for tid in pending_ids:
        t = tasks.get(tid)
        if t is None or t.parent_task_id is None:
            continue
        cancel_queued.append(tid)
        if t.started_at:
            cancel_now.append(tid)

    cancel_suspended: list[str] = []
    signal_inflight: list[str] = []
    ack_tasks: list[str] = []
    for t in tasks.values():
        if t.parent_task_id is None:
            continue
        if t.status == "SUSPENDED":
            cancel_suspended.append(t.id)
            if t.started_at:
                cancel_now.append(t.id)
        elif t.id in running_ids:
            signal_inflight.append(t.id)
            if _has_dispatch_frame(t):
                ack_tasks.append(t.id)

    fail_roots: list[str] = []
    signal_roots: list[str] = []
    for t in tasks.values():
        if t.parent_task_id is not None or t.status in TERMINAL_TASK_STATUSES:
            continue
        fail_roots.append(t.id)
        if t.id in running_ids:
            signal_roots.append(t.id)

    return TripPlan(
        cancel_queued=tuple(cancel_queued),
        cancel_suspended=tuple(cancel_suspended),
        signal_inflight=tuple(signal_inflight),
        ack_task_ids=tuple(ack_tasks),
        cancel_now_ids=tuple(cancel_now),
        fail_roots=tuple(fail_roots),
        signal_roots=tuple(signal_roots),
    )
