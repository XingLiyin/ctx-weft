"""判定档专属：把后台判决交出去，以及判 success 之后补跑 FinalizeStep 没跑的那几件事。

只摘要档（`background_recap` purpose）一行都用不到本模块——它没有判决工具，也不会终结任何
task。2026-09-29 从 `background_observe` 拆出来，正是因为这 200 行此前横在摘要路径中间。

**默认态是 park**：拿不到判决就什么都不提交（见 `submit_verdict`）。只有 success 触发状态
转移，绝不因为观察失败而静默放行 DAG 后继。
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.utils.verdict import VERDICT_SUCCESS, normalize_verdict

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)


#: 收口 park 气泡时写进 `HitlResolved` 的理由（供重放溯源；没有人看得见它——那个气泡从头到
#: 尾没被渲染过，前端两个入口都滤掉 `form=wait`）。
_PARK_CLOSED_BY_VERDICT = (
    "[Closed by the reviewer: this task was judged complete. Send another message to open "
    "a new round.]"
)

def subtask_handles(state: "LoopState", ctx: "LoopContext") -> list[dict]:
    """子任务句柄清单（task_id / title / outcome），与前台 observe 同一形状。

    只作信息：observer 据它在 `next_step_hint` 里**指名**哪个子任务的产出不合格，
    重派还是自己做由下一轮 actor 决定。拿不到 TaskManager 就返回空——清单缺失只是让
    hint 无法指名，不该把这次观察打挂。
    """
    tm = getattr(ctx, "task_manager", None)
    if tm is None or not hasattr(tm, "children_of"):
        return []
    out: list[dict] = []
    for cid in tm.children_of(state.task.id):
        child = tm.get_task(cid)
        if child is None:
            continue
        entry = {"task_id": child.id, "title": child.title or "",
                 "outcome": (child.status or "").lower()}
        if child.status == "CANCELED" and child.error_code:
            entry["note"] = child.error or child.error_code
        out.append(entry)
    return out


async def submit_verdict(state: "LoopState", ctx: "LoopContext", meta: dict) -> bool:
    """把 observer 的判决交给 TaskManager 的带外入口。

    返回 **这次判决是否把 task close 掉了**（= 判 success 且仲裁接受）。调用方据它决定
    这一段归谁记账：close 掉了就由带外 finalize 的 finish 对承载，**不能再产段摘要**
    （`finalize._supersede_final_raw_segment` 的不变量：同一段不记两遍）。

    **verdict 缺失 ≡ retry**：拿不到判决（LLM 失败、没调 terminal tool、字段为空）就
    什么都不提交，task 维持 park 等人。默认态是 park，只有 success 触发状态转移——
    绝不因为观察失败而静默放行 DAG 后继。

    带外入口自己做仲裁（task 是否仍在 `AWAITING_HUMAN`），这里不必先查一遍：人可能在
    这两行之间开口，查了也不作数。
    """
    raw = (meta.get(K.OBSERVER_OUTCOME) or "").strip()
    # 空 = 压根没判（工具没被调 / LLM 失败）→ 维持 park。非空才归一：`retry` 是别名，
    # 认不出的归 continue。**不能**把空串也喂进 normalize_verdict——那会把「没判决」
    # 变成「判了 continue」，两者对本函数的意义相同（都维持 park），但日志会说谎。
    verdict = normalize_verdict(raw) if raw else ""
    if not verdict:
        logger.info(
            "background observe produced no verdict (task=%s); staying parked", state.task.id)
        return False
    tm = getattr(ctx, "task_manager", None)
    if tm is None or not hasattr(tm, "apply_out_of_band_verdict"):
        logger.warning(
            "background observe: no TaskManager to submit the verdict to (task=%s)",
            state.task.id)
        return False
    outcome = RunOutcome(
        kind=RunOutcomeKind.COMPLETED,
        verdict=verdict,
        summary=meta.get(K.OBSERVER_TASK_SUMMARY, "") or "",
        outputs=getattr(state.task, "outputs", None),
        error=meta.get(K.OBSERVER_FAILURE_REASON, "") or "",
    )
    accepted = await tm.apply_out_of_band_verdict(
        state.task.id, outcome,
        process_report=meta.get(K.OBSERVER_ACT_RECAP, "") or "",
        task_summary=meta.get(K.OBSERVER_TASK_SUMMARY, "") or "",
        next_step_hint=meta.get(K.OBSERVER_NEXT_STEP_HINT, "") or "",
        # 带外收尾：只在真发生转移（success）时被调，跑在「状态已写定」与「转移已宣布」
        # 之间。见 `apply_out_of_band_verdict` 的 docstring 对那个窗口的三条约束。
        finalize=partial(_out_of_band_finalize, state, ctx, meta),
    )
    logger.info(
        "background observe verdict '%s' for task %s: %s",
        verdict, state.task.id, "accepted" if accepted else "rejected (the user spoke first)")
    return bool(accepted) and verdict == VERDICT_SUCCESS


async def _out_of_band_finalize(
    state: "LoopState", ctx: "LoopContext", meta: dict,
) -> None:
    """判 success 终结了这个 task 之后的收尾——**`FinalizeStep` 在这条路上没跑过**。

    park 在 `ActStep` 里抛 `HitlPark`，driver 到不了 observe/finalize。于是 S5/S6 之后每个
    「park → 后台判 success」的 task 都跳过了整个 `FinalizeStep`，而它是三件事的唯一发生地：

    - `finalize_task_memory` → `_close_one`：bubble 给 parent、同 agent 派发 ack 终态化、
      写 finish 对、折末段 raw。**子任务漏得最重**——parent 醒来只看到「派发框 + 停在
      running 的 ack」，拿不到任何产出。
    - `BLACKBOARD_PUBLISHED`（topic=task.id）：parent `recall_topic(task.id)` 的正路。
    - `TASK_FINALIZED`：host 的 `tasks.outputs_json` / `error` 两列。S5 起一直是空的。

    所以这里把 `apply_task_close` + `task_finalized_event` 照原样跑一遍，两个调用方共用同
    一份实现（不是在这里另写一份镜像——`synthesize_cancel_closure` 就是那样来的，它的
    docstring 自己承认在镜像 `_close_one`，两份必然漂）。

    手里的料都是全的：`state` 是 `launch_recap` 的快照（`scope` / `agent` /
    活的 `task` 都在，`extra` 里还有 `ActStep` 在 park **之前**写好的
    `final_body`/`final_summary`），`ctx` 是 `_readonly_ctx` 整份 replace 出来的
    （`memory` / `llm` / `event_bus` / `provider_ctx` 全在；那面 `control_readonly` 旗只
    gate 控制工具，不 gate `ctx.memory` 直写）。

    `has_llm_summary=True`：判决方就是 LLM，`act_recap` 是真摘要，close 即折末段 raw，不走
    「占位 + `replace_finish_report` 替换」那条为「finalize 先跑、bg 后到」设计的延迟路径
    ——带外路径顺序正好反过来。

    事件用这份快照发，于是带着 `LOOP_BACKGROUND_OBSERVE` 的 origin。这是对的：host 按
    origin 把后台事件挡在**会话状态折叠**和**对话流**之外（两者都不该被一段后台工作改写），
    而投影更新器的 `TASK_FINALIZED` 支不按 origin 过滤，`outputs_json` 照样落库。

    best-effort 在调用方：`apply_out_of_band_verdict` 把本函数包在 try 里，抛了也照常宣布
    转移——状态已是终态，不宣布会让槽位永不释放、交互线永久占着。
    """
    from ctx_weft.core.loop.steps.finalize import apply_task_close, task_finalized_event

    events = await apply_task_close(
        state, state.task, ctx,
        outcome=VERDICT_SUCCESS,
        act_recap=meta.get(K.OBSERVER_ACT_RECAP, "") or "",
        task_summary=meta.get(K.OBSERVER_TASK_SUMMARY, "") or "",
        has_llm_summary=True,
    )
    events.append(task_finalized_event(state, state.task, outcome=VERDICT_SUCCESS))
    for event in events:
        await ctx.event_bus.emit(event)
    await _close_park_bubble(state, ctx)


async def _close_park_bubble(state: "LoopState", ctx: "LoopContext") -> None:
    """判 success 终结了这个 task 之后，收口它 park 时开的那个「等你说话」气泡。

    **不分 root 与子任务**（2026-09-27 定）：task 被判完成了，那个让位入口就用掉了。人后面
    还想说话就再发一条消息——`send_message` 看到 `current_task_id` 已终态，自己会开新的一轮
    （`_start_task_for_agent`），根本不需要留着这个气泡当入口。

    留着它的代价是实打实的，两头都疼：

    - 宿主折会话状态时气泡**优先于** task 终态（`SessionStatusFold.status` 里 `pending_hitl`
      排在 `terminal` 之前），于是会话永久停在 PAUSED——SSE 的终态收口不触发，投影里
      `sessions.status` 一直 PAUSED，重启还被 `list_active_session_ids` 当活会话捞回来，
      `session_is_quiescent` 也一律拒绝逐出（宿主的空闲逐出器形同虚设）。
    - 子任务更糟：线已经交回 parent（`_try_resume_parent` 随即接管），再没有人会来答它，
      每个这样的子任务留一个、会累积。

    `faedd25` 曾为 root 保下这个气泡，两条理由只有一条成立，而那条也只是「会话会显示
    已完成」——判决说它完成了，显示已完成是实话。另一条「投递路径跟着状态分叉」本就与气泡
    无关：`send_message` 的路由只看 `current_task_id` 是否终态（`_task_is_terminal`），从不
    看气泡；那个分叉来自「判决抵达 vs 人先开口」的仲裁，收不收气泡都在。

    按 **task + user_turn 投递** 双重过滤：task 收窄是因为同一 agent 上可能还挂着别的
    请求（某个兄弟任务的 `ask_user`），投递收窄是因为「等人开口」和「等人拍板」是两回事
    ——后者即便挂在这个 task 上也该由它自己的路径处置，本函数只认前者。

    只在 `success` 时被调到（带外入口只在真发生转移时调 `finalize`）。`retry`/`fail` 维持
    park，那个气泡正是它等人的入口。

    best-effort：收不掉只记日志。判决已经落定，不能让收尾把它变成异常。
    """
    from ctx_weft.protocols.hitl import UserTurnDelivery

    task = state.task
    hitl = getattr(ctx, "hitl", None)
    if hitl is None or getattr(hitl, "registry", None) is None:
        return
    try:
        for req in hitl.registry.list_pending(state.session.id):
            if req.task_id != task.id or not isinstance(req.delivery, UserTurnDelivery):
                continue
            await hitl.cancel(req.id, message=_PARK_CLOSED_BY_VERDICT)
            logger.info(
                "closed the park bubble %s of task %s (judged complete; the next message "
                "opens a new round)", req.id, task.id)
    except Exception:
        logger.exception(
            "failed to close the park bubble of task %s; it will stay pending and hold the "
            "session at PAUSED", task.id)


