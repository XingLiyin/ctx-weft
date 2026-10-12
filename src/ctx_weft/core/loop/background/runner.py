"""后台 recap 的生命周期：怎么放出去、放出去的那些怎么记账、怎么等它们。

与「观察」无关 —— 这里全是 fire-and-forget 任务的基础设施：三个在途槽（按 task、按 agent、
无 TaskManager 时的孤儿集）、done 回调清账、只读 ctx 的构造，以及唯一的公开入口
`launch_recap`。2026-09-29 从 `background_observe` 拆出来。

段界水位线在 `launch_recap` 里取（不是等协程跑起来才取）：launch 与协程首次获得控制权之间
隔着至少一次事件循环让渡，人的消息完全可能挤进那道缝。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from ctx_weft.core.loop.background.recap import _run_recap
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.ids import generate_id
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)


# ── 在途槽 ────────────────────────────────────────────────────────────────────

_task_pending: dict[str, asyncio.Task] = {}
# task_id -> 该 task 当前在途后台 observe 那次 launch 的 run_id（与 _task_pending 同步写入，
# launch_recap 里两行相邻赋值；TaskRecapDone 事件信封上的 run_id 就是它——
# 见 pending_recap_run_id docstring）。
_task_pending_run_id: dict[str, str] = {}
# agent_id -> 该 agent **最近一次** launch 的在途后台 observe。与 _task_pending 同步写入。
#
# 为什么按 task 键还不够：`send_message` 打到一个已终态 agent 上时走
# `_start_task_for_agent` 建的是**新 task**，新 task 的 `_run_loop` 入口等的是
# `_task_pending[新 task_id]`（空），上一个 task 的折叠挂在旧 task_id 下、无人等。
# 而 close 边界的 launch 恒发生在 `TaskFinished` 之前（同协程、无 await 间隔），
# 所以「上一轮刚结束、用户立刻发下一条」这个窗口里折叠必然在飞——新 task 的首次装配
# 会经 `recall_recent_by_agent`（过滤 `not is_superseded`）读到上一轮**未被 supersede
# 的 raw**，而不是折出来的胶囊：prompt 白白胀一轮的量。
#
# 一个 agent 同时只可能有一次在途后台 observe（它自己那条 run 是串行的），故这里
# 一个槽足够；子 agent 各自一个 agent_id、互不干扰。
_agent_pending: dict[str, asyncio.Task] = {}
_orphan_tasks: set[asyncio.Task] = set()


# ── 清账 ──────────────────────────────────────────────────────────────────────

def _clear_pending(t: asyncio.Task, tid: str, aid: str = "") -> None:
    """Compare-and-clear：两个轴各自比对身份后再删，互不影响。

    比对身份（`is t`）而不是无条件 del：同一个 key 上可能已经被更晚的一次 launch 覆盖，
    那时这次 done 回调不该把别人的登记删掉。
    """
    if _task_pending.get(tid) is t:
        del _task_pending[tid]
        _task_pending_run_id.pop(tid, None)
    if aid and _agent_pending.get(aid) is t:
        del _agent_pending[aid]


def _readonly_ctx(ctx: "LoopContext") -> "LoopContext":
    """把控制工具钉成只读的 ctx 副本（2026-09-22）。

    `ControlContext.task` 是从 `TaskManager._tasks` 取的**活对象**。后台 observe 是
    fire-and-forget，常在主 run 收尾、agent 已被 evict 之后才真正跑到——它调用的工具
    若写 task，就是隔着时间改主线程状态。标记经 `provider_ctx.extra` 透传
    （`CapabilityGateway` 在 `dataclasses.replace` 时原样带上 extra），落到
    `ControlContext.readonly`。

    拿不到副本时（`provider_ctx` 缺失，或 ctx / provider_ctx 是手构的替身而非
    dataclass——测试里很常见）原样返回，不强行造一个。**但要在日志里看得见**：S5 之后
    后台的 terminal tool 会写 task，标记丢失就意味着隔着时间改主线程状态。
    """
    provider_ctx = getattr(ctx, "provider_ctx", None)
    if provider_ctx is None:
        return ctx
    if not (dataclasses.is_dataclass(ctx) and dataclasses.is_dataclass(provider_ctx)):
        logger.warning(
            "background observe: cannot derive a readonly ctx (ctx=%s, provider_ctx=%s); "
            "control tools will run in write mode",
            type(ctx).__name__, type(provider_ctx).__name__,
        )
        return ctx
    return dataclasses.replace(
        ctx,
        provider_ctx=dataclasses.replace(
            provider_ctx,
            extra={**(provider_ctx.extra or {}), "control_readonly": True},
        ),
    )



# ── 放出去与等回来 ────────────────────────────────────────────────────────────

def launch_recap(
    state: "LoopState", ctx: "LoopContext", *, boundary: str
) -> asyncio.Task:
    # 总账 A4：旧写法只 `dataclasses.replace(state)`——run_id 与主 run 相同，但
    # sequence_counter 是一份独立的 int 副本（普通字段，不可变），此后两边各自
    # +=1，(run_id, sequence) 就会撞号。后台 recap 本来就是一段独立的工作，有自己
    # 的起止事件（TaskRecapStarted/Done，现在还加了 RunStarted/Finished），不该
    # 蹭主 run 的号——给它自己的 run_id、序号从 0 重开。
    snapshot = dataclasses.replace(
        state,
        run_id=generate_id("run"),
        sequence_counter=0,
    )
    # 段界水位线（2026-09-22）：**在这里**取时刻，而不是等协程跑起来再取——launch 与
    # 协程首次获得控制权之间隔着至少一次事件循环让渡（`_cold_park` 紧接着就 await
    # `hitl.open()`），人的消息完全可能挤进那道缝。钉住它，迟到的折叠自己落回原位，
    # 两条用户注入路径都不必为此等待。
    task = asyncio.create_task(
        _run_recap(snapshot, _readonly_ctx(ctx), boundary, watermark=now_utc()))
    _task_pending[state.task.id] = task
    _task_pending_run_id[state.task.id] = snapshot.run_id
    _agent_pending[state.agent.id] = task
    tm = getattr(ctx, "task_manager", None)
    if tm is not None and hasattr(tm, "track_background"):
        tm.track_background(task)
    else:
        _orphan_tasks.add(task)
        task.add_done_callback(_orphan_tasks.discard)
    task.add_done_callback(
        lambda t, tid=state.task.id, aid=state.agent.id: _clear_pending(t, tid, aid))
    return task


async def await_pending_recap(task_id: str) -> None:
    """等该 task 在途后台 observe 完成。

    **2026-09-22 起生产代码不再调用它**：`_run_loop` 入口与 `_inject_user_reply` 那三道
    屏障已拆除，正确性由段界水位线接管（见 `launch_recap` 的 watermark），
    剩下的「首次装配可能读到未 supersede 的 raw、prompt 白胀一轮」是已接受的性能代价。

    保留它是因为**测试仍需要一个同步原语**来等那条 fire-and-forget 的协程。若将来要把
    等待加回某条路径，先想清楚换回来的是什么——那三道屏障拆掉换的是「人回复不为一次
    后台 LLM 往返买单」。
    """
    pending = _task_pending.get(task_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)


async def await_pending_recap_for_agent(agent_id: str) -> None:
    """等该 **agent** 在途后台 observe 完成。

    与 `await_pending_recap(task_id)` 同源，**同样自 2026-09-22 起无生产
    调用方**（见那一个的 docstring）。两者曾是同一份强一致保证的两个轴：task 轴覆盖
    「同一个 task 的下一轮 run」（retry / resume / reconcile 重放），agent 轴覆盖「同一个
    agent 的**下一个 task**」——后者正是 `send_message` 在 agent 已终态时走
    `_start_task_for_agent` 建新 task 的那条路，task 轴够不着（见 `_agent_pending`）。

    两轴常常指向同一个对象（同 task 的下一轮），重复 await 无害：第二次 `pending.done()`
    为真，直接返回。
    """
    pending = _agent_pending.get(agent_id)
    if pending is not None and not pending.done():
        await asyncio.shield(pending)


def pending_recap_run_id(task_id: str) -> str | None:
    """非阻塞地探一眼：该 task 此刻是否有一个还没跑完的后台 observe（同步字典读，无 await）；
    有就返回**那次 launch** 的 `run_id`，没有返回 `None`。

    `TurnHandle.wait_for_finish`（`runtime.py`）用它决定终态事件到达后要不要在**同一条
    事件流订阅**上继续等对应的 `TaskRecapDone`——而不是另起一个等这个 asyncio.Task 本身
    （`await_pending_recap`/`asyncio.shield`）的等待者。两者看似等价，调度
    顺序却不同：`TaskManager._fire_session_done` 也在等这同一个后台任务（`asyncio.gather`），
    且它的等待**总是先注册**（`_run_task` 那条协程从发 TaskFinished 到那次 gather 之间不
    过几行同步代码，中途不会把控制权交还事件循环）；若 `wait_for_finish` 也去等**同一个
    Task 对象**，两个等待者的回调都挂在它的完成清单上，`_fire_session_done` 那个先注册、
    先被唤醒——它会在 `wait_for_finish` 之前跑完 `on_session_done`，抢先把这一轮判成
    收尾（2026-09-08 之前那一步还连着 `_release_session`，会把 session 连同 agent
    record 一并拆掉，后果更重；现在只剩清控制信号，但顺序问题本身没变）。
    改成等**事件总线上的 TaskRecapDone**
    则不出现这个问题：后台 observe 在 `finally` 里先 `emit(TaskRecapDone)`（这一步只是把
    事件塞进订阅者各自的队列，不等任何人处理）、之后才真正从协程函数 return、其
    `asyncio.Task` 才转入 done 态——事件总线的那次唤醒排在 Task-done 的唤醒之前，
    `wait_for_finish` 借着「早就在等这条流」的事件订阅，比 `_fire_session_done` 的
    `gather` 更早被唤醒返回，不会撞见会话已经被拆完的中间态。

    **为什么要返回 run_id、不能只返回 bool（2026-09-04 二轮修复）**：`_task_pending`
    只挂**最新一次** launch——同一个 task 的一生里可能有多次 fire-and-forget 后台
    observe（`interrupt`/`mechanical`/`dispatch`/`finish` 等不同 boundary，跨
    suspend/resume、重试多轮发生），彼此不重叠是**通常**情况，不是**保证**情况。
    若这次 close 边界 launch 之前，上一次的后台任务碰巧还没来得及被下一轮 `_run_loop`
    入口的 `await_pending_recap` 收口（例如两次 launch 之间调度得足够
    密集），`wait_for_finish` 在终态事件之后看到的第一个 `TaskRecapDone` 可能是**那次
    更早的 launch** 发的，不是我们真正要等的这次——只按事件类型匹配会在这种重叠窗口里
    复现同一类「提前返回」问题，只是窗口更窄。`launch_recap` 给每次
    launch 都铸一个独立 `run_id`（`dataclasses.replace(state, run_id=generate_id(
    "run"), ...)`），`make_event` 把它写进事件信封的 `run_id` 字段——`TaskRecapDone`
    也不例外。返回这个 run_id，让调用方把匹配钉死到「这一次 launch 发的 TaskRecapDone」，
    而不是「随便哪次 launch 发的、类型对得上的事件」。"""
    pending = _task_pending.get(task_id)
    if pending is None or pending.done():
        return None
    return _task_pending_run_id.get(task_id)
