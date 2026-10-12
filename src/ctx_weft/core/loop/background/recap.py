"""后台 recap 的编排本体：一次段边界观察从头到尾。

两档共用这一个函数 —— 判定档（`background_observe` purpose）与只摘要档
（`background_recap` purpose）的差别只有函数开头那个 `judging`，以及它派生的四处：短段门过
不过、purpose、terminal 工具、要不要提交判决。**故意不拆成两个模块**：其余两百行（锁、重跑
幂等护栏、prompt 装配、ReAct、报告取值、close 交接、段折、事件信封、异常与取消）逐字共用，
拆开只会让「两档差在哪」从一处六行散成三个文件。

`judging` 的两个条件相与：这个边界要不要判（`boundaries.judges`），以及这个 agent 有没有
observer（`observe.has_observe_role`）。

失败一律吞掉，降级 = 该段保 raw（spec §3.6）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime  # noqa: F401  （watermark 的类型注解）
from typing import TYPE_CHECKING

from ctx_weft.protocols.events import EventOrigin, EventType
from ctx_weft.core.assembler import ContextRequest
from ctx_weft.core.loop.driver import make_event
from ctx_weft.core.loop.background.boundaries import CLOSE_BOUNDARIES, judges
from ctx_weft.core.loop.background.verdict import submit_verdict, subtask_handles
from ctx_weft.core.loop.finish_pair import (
    pop_close_synth,
    put_close_report,
    replace_finish_report,
)
from ctx_weft.core.loop.fold import is_short_segment
from ctx_weft.core.loop.observing import has_observe_role, run_observe_react
from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K
from ctx_weft.core.orchestrator.task.disposition import RunOutcomeKind
from ctx_weft.core.utils.content import content_to_text

if TYPE_CHECKING:
    from ctx_weft.core.loop.driver import LoopContext, LoopState

logger = logging.getLogger(__name__)


# ── 串行锁：同一 task 至多一次 recap 在跑 ────────────────────────────────────

_task_locks: dict[str, asyncio.Lock] = {}
def _lock_for(task_id: str) -> asyncio.Lock:
    lock = _task_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _task_locks[task_id] = lock
    return lock



# ── 编排 ──────────────────────────────────────────────────────────────────────

async def _run_recap(
    state: "LoopState", ctx: "LoopContext", boundary: str,
    *, watermark: "datetime | None" = None,
) -> None:
    from ctx_weft.core.capabilities.control_tools import (
        COLLECT_PROCESS_REPORT_NAME,
        REPORT_TASK_OUTCOME_NAME,
    )

    # 判定档 or 只摘要档——**一次算清，本函数三处共用**（短段免折门、purpose/工具面、提交
    # 判决）。两个条件相与：这个边界要不要判（`judges`），以及这个 agent 有没有 observer
    # （`has_observe_role`）。后者 2026-09-28 加：模板没有 ROLE 时前台 observe 走机械判决，
    # 后台没理由反倒去判——那等于让 actor 顶着自己的 SOUL 判自己（装配层缺 observe facet
    # 时曾回退 act，现已改为不回退，见 `sources/identity.py::_OBSERVE_PURPOSES`）。
    #
    # 与之配套的是 `act.py` 里 `finish_park` 那条让位判据也认 `has_observe_role`：否则
    # park 了却没人判，root 永远停在 AWAITING_HUMAN。
    judging = judges(boundary) and has_observe_role(state)

    # Task 5 复审修复：origin 必须在本函数**任何**发射点之前钉住，不能拖到调
    # run_observe_react 前才改——RUN_STARTED/TASK_RECAP_STARTED（下面紧接着）以及
    # 两条早退路径（re-fold 幂等护栏、短段免折，均在拿到 origin 之前就 return）此前
    # 都带着调用方快照进来的 origin（正常路径是 LOOP_OBSERVE，act.py interrupt 边界
    # 路径是 LOOP_ACT）发出，host 按 origin 前缀过滤后台事件时会把它们误判成前台
    # 事件渲染进对话流（docs/events-v2.md §4）。这份 state 是 launch_recap
    # 给这段后台工作单独快照出的（独立 run_id/sequence_counter），改写 origin 不会
    # 污染主 run 的 state。
    state.origin = EventOrigin.LOOP_BACKGROUND_OBSERVE

    # 总账 C5（控制方裁定 R1）：`launch_recap` 给这段快照发了自己的
    # run_id（解 A4）——它在 host 眼里就是一段独立的 run，得有起有止，不能只补
    # recognize_intent / compact_agent 两处而漏掉它自己。payload 结构照抄
    # `_run_loop` 的实际发射点（`runtime.py`，见 task-5-report）；`initial_step`
    # 用它实际做的事 "background_observe"（这条路径没有 StepDriver，取不到真实
    # step 名）。TaskRecapStarted/Done 是任务级的 recap 记账，与这里的 run 级起止
    # 是两层，不互相顶替。
    await ctx.event_bus.emit(make_event(state, EventType.RUN_STARTED, payload={
        "run_id": state.run_id,
        "initial_step": "background_observe",
    }))
    await ctx.event_bus.emit(make_event(
        state, EventType.TASK_RECAP_STARTED,
        payload={"task_id": state.task.id, "boundary": boundary, "agent_id": state.agent.id},
    ))
    run_error: Exception | None = None
    was_cancelled = False
    try:
        async with _lock_for(state.task.id):
            # 重跑幂等护栏（恢复重跑时才生效）：非 close 边界若该段已无 active raw，说明上次
            # 崩溃前已折叠（raw 被 supersede），再折会产冗余胶囊 → 跳过（finally 仍发 DONE）。
            # 正常运行时该段刚产生 raw、计数 > 0，护栏为 no-op。
            if boundary not in CLOSE_BOUNDARIES:
                from ctx_weft.protocols import MemoryKind, MemoryScope
                view = await ctx.memory.load_view(
                    state.scope, MemoryScope.TASK, ctx.provider_ctx,
                    kinds=[MemoryKind.CONVERSATION_TURN],
                )
                # 旧口径 = LLM_RESPONSE 计数 = assistant 回合
                n_raw = sum(1 for r in view if r.role == "assistant")
                if n_raw == 0:
                    logger.info(
                        "task recap re-fold guard: segment already folded (task=%s); skip",
                        state.task.id,
                    )
                    return
                # 短段免折（is_short_segment）：当前段（末条 UP 之后）active raw 低于阈值时
                # 跳过折叠——花一次后台 LLM 调用换一段常比原文还长的摘要不划算。跳过 = 该段
                # **永久**保 raw（与观察失败的降级同语义）：后续折叠带 since_last=USER_PROMPT
                # 只折各自的当前段，免折残留不会被跨段合折。
                short_segment = await is_short_segment(state, ctx, watermark)
                if short_segment and not judging:
                    logger.info(
                        "short segment kept raw (task=%s boundary=%s); skip fold",
                        state.task.id, boundary,
                    )
                    return
            try:
                agent = state.agent
                # 不能像 observe._llm_observe 那样用 has_agent() 短路：background observe 是
                # fire-and-forget（launch_recap → asyncio.create_task），常在
                # 本 run 的 _run_loop finally evict(agent.id) 之后才真正跑到这——per-agent 快照
                # 已被逐出，has_agent 为 False。控制工具（collect_process_report 等）是 session
                # 全局区（register_global，不随 evict 逐出），故这里应始终尝试 .get()（内部自动
                # 合并全局区），不能因 per-agent 快照缺失就整体清零、连全局控制工具也丢了。
                bound_caps = (
                    ctx.capability_cache.get(agent.id)
                    if ctx.capability_cache is not None
                    else []
                )
                # 判定边界要带子任务清单：判 retry 时 observer 要在 `next_step_hint`
                # 里指名哪个子任务的产出不合格（与前台 observe 同一用途）。
                # 两档（2026-09-28）：判定档给判决工具，只摘要档给 `collect_process_report`。
                # **由 purpose 裁工具面**（`request.purpose in cap.purposes`），于是「这个边界不判」
                # 是工具面的事实，不靠 cue 里一句叮嘱——那句叮嘱曾与「task_status 必填」冲突。
                purpose = "background_observe" if judging else "background_recap"
                terminal_tool = (
                    REPORT_TASK_OUTCOME_NAME if judging else COLLECT_PROCESS_REPORT_NAME)
                extra: dict = {"observe_boundary": boundary}
                if judging:
                    extra["subtasks"] = subtask_handles(state, ctx)
                request = ContextRequest(
                    purpose=purpose,
                    scope=state.scope,
                    task=state.task,
                    agent=agent,
                    session=state.session,
                    template=state.extra.get("template"),
                    bound_capabilities=bound_caps,
                    token_counter=ctx.llm.tokenizer.count,
                    extra=extra,
                )
                prompt = await ctx.assembler.assemble(request)
                # Task 5：不再有 event_types 间接层——LLM_* 的「后台 vs 前台」区分改靠
                # state.origin（EventOrigin.LOOP_BACKGROUND_OBSERVE，已在函数入口钉住），
                # 由 host 据此决定后台 observe 的 LLM 交互不进前端对话流。
                result, last_text = await run_observe_react(
                    state, ctx,
                    system=prompt.system,
                    messages=list(prompt.messages),
                    tools=prompt.tools,
                    max_rounds=agent.loop_config.max_turns_per_observe,
                    terminal_tool_name=terminal_tool,
                )
                # 报告取值：terminal 工具的**结构化回传**（S3 的 OBSERVER_* key）→ 纯文本
                # 复述兜底（observer 把复述写成正文而没调工具）。
                #
                # 不再解析 `result.content`：2026-09-22 起 terminal tool 与前台同一个
                # （`report_task_outcome`），它的 content 是给 LLM 看的确认话术
                # （"Assessment recorded: outcome=…"），拿它当 act_recap 会把话术折进段摘要。
                # content_to_text 仍留给兜底那一支：`last_text` 可能是 list[ContentPart]。
                meta = (result.metadata or {}) if result else {}
                act_recap = (meta.get(K.OBSERVER_ACT_RECAP)
                             or content_to_text(last_text or "") or "").strip()
                task_summary = meta.get(K.OBSERVER_TASK_SUMMARY, "") or ""
                if not act_recap:
                    # 无任何可用报告：与异常路径同语义——段保 raw，不写占位摘要、不动 finish 对。
                    if boundary in CLOSE_BOUNDARIES:
                        pop_close_synth(state.task.id)  # 弹掉登记防泄漏；finalize 占位 finish 对保持原样
                    logger.warning(
                        "background observe produced no usable report (task=%s boundary=%s); "
                        "segment kept raw", state.task.id, boundary,
                    )
                    return
                # **判决先落地，折叠再决定**（2026-09-27）：判 success 会经带外 finalize 走
                # close（写 finish 对 + 折末段 raw），而那条路**刻意不产段摘要**——
                # `finalize._supersede_final_raw_segment` 的不变量是「末段 raw 与真实 Process
                # Report 至少存其一」，段摘要与 finish 对同时存在就是把同一段记了两遍。所以
                # 「这一段归谁记账」只有拿到判决才知道，不能先折了再判。
                #
                # 判 retry/fail、判决缺失、仲裁被拒（人先开口）→ 没有 close，段摘要照产；
                # 人先开口那一支还多一层保障：段界水位线让折叠只认这次 launch 之前的记录，
                # 新那条 user 回合不会被卷进来。
                closed = await submit_verdict(state, ctx, meta) if judging else False

                if boundary in CLOSE_BOUNDARIES:
                    synth = pop_close_synth(state.task.id)  # sync check-and-clear（无 await）
                    if synth is not None:
                        tool_call_id, scope, outcome, raw_fold_scope = synth
                        from ctx_weft.core.loop.steps.finalize import _output_text
                        await replace_finish_report(
                            ctx.memory, ctx.provider_ctx, scope, state.task.id,
                            tool_call_id, act_recap, task_summary, outcome,
                            state.task.title or "",
                            # raw 此刻才折 → 答复随之补进锚点槽（占位两槽升三槽）
                            # getattr 兜底：本路径异常被整段吞掉并「保 raw」，缺字段的
                            # task 替身若在此炸 AttributeError，真报告就永远替换不上去。
                            final_reply=(_output_text(getattr(state.task, "outputs", None))
                                         if raw_fold_scope is not None else ""),
                        )
                        if raw_fold_scope is not None:
                            # 真摘要已替换进 finish 对 → 补删末段 raw（延迟折叠收口，
                            # spec 2026-07-20：占位 close 不即折，真报告落地才折）。
                            from ctx_weft.core.loop.steps.finalize import (
                                _supersede_final_raw_segment,
                            )
                            await _supersede_final_raw_segment(
                                ctx.memory, raw_fold_scope, ctx.provider_ctx)
                    else:
                        # root 的 finish/normal 是终结点（单次 close）：槽写一次弹一次，不存在
                        # 跨 rerun 乱序覆盖（retry 仅在机械退出时产生，不经此路径）。
                        put_close_report(state.task.id, act_recap, task_summary)
                elif not short_segment and not closed:
                    # v2 P3c：策展上移——段作用域折叠（护 user 回合与既有段摘要、锚点/
                    # 段尾语义，与 observe._fold_retry_segment 同门）由框架侧 segment_fold
                    # 执行原子 fold。旧 apply_compact 的 TypeError 协议错配特判随之消亡
                    # （segment_fold 是框架内函数，签名错配不再是运行时 provider 风险）。
                    #
                    # `short_segment` 为真时跳到这里之外：**免折不免判**（2026-09-22）——
                    # 判定必须跑（短回合恰恰是提问最典型的形态），但短段的 recap 常比原文
                    # 还长，折它是净亏，段保 raw。
                    from ctx_weft.core.loop.fold import segment_fold
                    await segment_fold(
                        ctx.memory, state.scope, MemoryScope.TASK, act_recap,
                        ctx.provider_ctx, watermark,
                    )
            except Exception:
                # close 边界防泄漏：finalize 可能已 register_close_synth，本次失败后永远无人
                # 消费（task_id 唯一 + close 单入口），弹掉——与「无可用报告」分支对称。
                # 已知残余窗口（接受，不另引状态同步）：bg 比 finalize 先死时登记发生在 pop
                # 之后，仍漏一条；对称地，bg 先写 _close_report 而 finalize 异常中止也漏。
                # 两者触发概率与单次代价（几百字节/次）都低一个量级。
                if boundary in CLOSE_BOUNDARIES:
                    pop_close_synth(state.task.id)
                logger.exception("background observe failed (ignored); segment kept raw")
    except asyncio.CancelledError:
        # F2：CancelledError 不是 Exception 子类（3.8+ 起继承 BaseException），上面
        # `except Exception` 接不住它——不接住就意味着 run_error 仍是 None，下面
        # RunFinished 会把一次真取消谎报成 completed。记下来，供 finally 里的
        # RunFinished.outcome 用（在异常继续传播之前发出，见下）。
        #
        # 这里**要重新抛出**——与 runtime.py::_run_loop 的 `except asyncio.CancelledError`
        # 刻意不同，不是疏漏：_run_loop 吞是因为它把取消结果转成了 `RunOutcome{kind=
        # CANCELED}` 这个**返回值契约**塞回给调用方（run 层报结局靠返回值），取消信息
        # 没丢，只是换了个载体。这里没有这种契约——虽是 fire-and-forget、没人 await
        # 结果，但吞掉真正的 CancelledError 没有任何好处，也没理由让协作取消在这里
        # 被悄悄吸收掉。
        was_cancelled = True
        raise
    except Exception as exc:
        # 上面那个 except 只吞真正跑出 fold/observe 的失败（业务已降级 = 段保
        # raw，run 仍算跑完）；这里接的是护栏段（幂等检查 / is_short_segment）本身
        # 炸的异常——那条既有行为是原样往外传，不吞。只是顺手记一笔，好让下面的
        # RunFinished 如实报 outcome=interrupted，不撒谎报 completed。
        run_error = exc
        raise
    finally:
        await ctx.event_bus.emit(make_event(
            state, EventType.TASK_RECAP_DONE, payload={"task_id": state.task.id},
        ))
        await ctx.event_bus.emit(make_event(state, EventType.RUN_FINISHED, payload={
            "outcome": (
                RunOutcomeKind.CANCELED.value if was_cancelled
                else RunOutcomeKind.COMPLETED.value if run_error is None
                else RunOutcomeKind.INTERRUPTED.value
            ),
            "final_status": state.task.status,
            "will_retry": False,
            "total_events": state.sequence_counter,
            "total_turns": len(state.transcript),
            "error": str(run_error) if run_error else None,
            "error_type": type(run_error).__name__ if run_error else None,
        }))


