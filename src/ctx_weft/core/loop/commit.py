"""Round commit/discard boundaries shared by steps and tool execution."""
from __future__ import annotations

from ctx_weft.core.loop.driver import (
    HOT_REPLY_ROUND_KEY,
    RECOGNIZE_INTENT_PENDING_KEY,
    ROUND_COMMITTED_KEY,
    LoopContext,
    LoopState,
    StepDriver,
)
from ctx_weft.core.loop.signals import HitlPark, RoundDiscarded

_RECOGNIZE_INTENT_STARTED = "_recognize_intent_started"


def defer_until_commit(state, ctx) -> None:
    """Schedule metadata recognition only after this round becomes irrevocable."""
    if state.extra.get(_RECOGNIZE_INTENT_STARTED):
        return
    if state.extra.get(ROUND_COMMITTED_KEY):
        StepDriver.for_context(ctx).start("recognize_intent", state, ctx)
        state.extra[_RECOGNIZE_INTENT_STARTED] = True
    else:
        state.extra[RECOGNIZE_INTENT_PENDING_KEY] = True


async def commit_round(state: LoopState, ctx: LoopContext) -> None:
    """提交本轮：LLM 开口了，这一轮算数（spec 2026-09-09）。**幂等**，每个 chunk 都调。

    在此之前，这一轮的两样东西还没落地：

    1. `TASK_CREATED` / `TASK_STARTED` / `RUN_STARTED` / `LLM_PROMPT_SENT` 攒在总线的
       未提交窗口里（只到达了进程内状态机，见 `EventBus` 类 docstring）；
    2. 把这一轮唤醒的那条 HITL 答复还停在**待终局**（`pending_decision`），
       `HitlResolved` 一直没发——日志里那个气泡仍是 pending。
    3. 这一轮的 memory 写入（用户消息、HITL 答复、由答复回灌的工具结果）暂存在窗口里
       （`ingest_or_stage`），memory 里还没有它们。提交时钩子先发 `HitlResolved`、再按序
       落盘——**终局事实先于答复进 memory**。
    4. `recognize_intent` 还没起飞（判定在 `PrepareStep`，起飞在这里）——旁路只该为
       **真的发生过**的那一轮花一次 LLM 调用。它跑在 root task 上，而 root task 从第二句
       起每一轮都开窗，放在 prepare 起飞就会在窗口里跑、被撤销时整串事件白丢。

    暂存的记录压缩够不着（L0.5 图片降级 / L1 / L3 只折 memory）。所以 PrepareStep 判定要
    压缩时**先提交**再压缩——代价是那一轮不再可撤，换来带图的第一条消息照样能被降级
    （`test_media_fold_replay_e2e` 钉住）。act 离开循环去别的步骤之前同样先提交：那些步骤
    直接读 memory，而这一轮已经推进到了别处，算数。

    顺序不可换：`TASK_CREATED` 必须最先出闸，它是下游 reducer 与 host 建 task 键的那一条；
    `recognize_intent` 必须在它之后起飞，它会发自己的一串事件，且要写 `task.title/description`
    ——那两个字段正是窗口里 `TASK_CREATED` 的 payload 在出闸那一刻现算的来源。
    """
    if state.extra.get(ROUND_COMMITTED_KEY):
        return

    # 两阶段终局的第二阶段（那条把这一轮唤醒的答复——HITL 应答 / `send_message` 对旧
    # 气泡的收口——现在才算数、`HitlResolved` 在此刻才发）由 `commit_round` 的钩子完成，
    # 见 `TaskManagerHooks.commit_round`。
    tm = ctx.task_manager
    if tm is not None:
        await tm.commit_round(state.task.id)
    # **成功之后才置位**：提交失败（存储不可用）时异常上抛、标志保持 False，下一个提交点
    # （或 `_run_task` 的收尾兜底）会重试，而不是以为已经提交过了。
    state.extra[ROUND_COMMITTED_KEY] = True
    # 这一轮算数了，它就不再是「可热撤销的一轮」。
    state.extra.pop(HOT_REPLY_ROUND_KEY, None)

    if state.extra.pop(RECOGNIZE_INTENT_PENDING_KEY, False):
        defer_until_commit(state, ctx)


async def discard_uncommitted_round(state: LoopState, ctx: LoopContext) -> None:
    """用户在 LLM 开口之前按了停 → 整轮丢弃（spec 2026-09-09）。已提交则原样返回。

    只对**由一条用户消息新开出来的 task**（`send_message` 的新建分支，`TaskManager`
    给它开了未提交窗口）成立。注入既有 task 的那两条路径（agent 本就在
    `AWAITING_HUMAN` / 挂起等子任务）不在此列——那个 task 早就提交过、是一段正在进行的
    对话，丢不得；它们照常 park 出续跑气泡，只是那条刚注入的用户消息同样还没落 memory，
    所以照样零残留。

    本函数只抛信号：`RoundDiscarded` 一路 unwind 到 `_run_task`，由 `discard_round` 收尾。
    memory 这边无事可做——这一轮的用户消息 / 答复只在窗口的暂存区里（`ingest_or_stage`），
    随窗口一起扔掉。

    **一条事件都不发**：task 状态事件的发射点只有 TaskManager 一处（Task 4 的不变式，
    `test_task_manager_owns_status` 有静态守卫盯着）。把 agent 送回 `idle` 的那条
    `TASK_CANCELED`、以及关窗丢弃，都在 `TaskManager.discard_provisional` 里按正确顺序完成。
    """
    if state.extra.get(ROUND_COMMITTED_KEY):
        return
    tm = ctx.task_manager
    if tm is None or not tm.is_round_open(state.task.id):
        return

    hot_hitl_id = state.extra.pop(HOT_REPLY_ROUND_KEY, "")
    if hot_hitl_id:
        # **热撤销**：这一轮是一条热应答开出来的——同一个 run 醒来接着跑，还没等到 LLM
        # 开口就被暂停了。不能整轮 `RoundDiscarded`：这个 run 的 `RUN_STARTED` 早已落盘，
        # 连它的结尾一起丢掉，日志里就是一个永不结束的 run。
        #
        # 于是分两半：答复这一半撤掉（答复退回待答；缓冲的事件与暂存的工具结果随窗口丢弃，
        # memory 里本来就没有），run 这一半照常 park 在那个问题上——正是「人还没答」的世界，
        # `RUN_FINISHED` / `TaskAwaitingHuman` 如实落盘，重答走冷路径。
        await tm.abandon_round(state.task.id)
        req = ctx.hitl.registry.get(hot_hitl_id) if ctx.hitl is not None else None
        raise HitlPark(hitl_id=hot_hitl_id,
                       tool_call_id=req.tool_call_id if req is not None else "")

    raise RoundDiscarded(state.task.id)


def rearm_after_hot_reply(
    state: "LoopState", ctx: "LoopContext", hitl_id: str,
) -> None:
    """热应答把协程叫醒之后，给「这一轮」重新武装提交点（spec 2026-09-09）。

    `reply_to_hitl` 为这条答复开了未提交窗口，决定也只是待终局。被叫醒的 run 早就过过
    一次提交点（`ROUND_COMMITTED_KEY` 是 run 级幂等标志），不清掉它，`commit_round`
    永远是 no-op：窗口要到 run 结束才关，期间该 task 的事件全挡在缓冲里，host 看不见
    下一个问题——连续提问时「第二个问题答了没用、一直让人重答」就是这么来的。

    只在**这条答复自己开的窗**上做（`round_hitl_id` 对得上）。窗若是别的应答开的
    （例如冷续跑的一轮里，reconcile 重放的工具又热等了一次授权），那一轮本来就没提交，
    标志本就是 False，也轮不到这里决定怎么撤。

    同时记下 `HOT_REPLY_ROUND_KEY`：这一轮若在 LLM 开口前被暂停，act 要走**热撤销**
    （撤回答复后 park），而不是整轮 `RoundDiscarded`——这个 run 的 `RUN_STARTED` 早已
    落盘，把它的结尾连同缓冲一起丢掉，日志里就留下一个永不结束的 run。
    """
    tm = ctx.task_manager
    if tm is None or not tm.is_round_open(state.task.id):
        return
    if tm.round_hitl_id(state.task.id) != hitl_id:
        return
    state.extra[ROUND_COMMITTED_KEY] = False
    state.extra[HOT_REPLY_ROUND_KEY] = hitl_id
