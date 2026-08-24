"""ActStep：多 turn LLM 子循环 + capability 调用。

工具调用通过 CapabilityGateway 统一执行（授权 + 审计 + 内存），
ActStep 只处理 LLM 流 + turn 循环逻辑。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from ctx_weft.protocols import LLMMessage, LLMRequest, LLMUsage, ToolCall
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import (
    PROMPT_EST_BASE_KEY, PROMPT_EST_SEG_KEY, request_prompt_estimate, resolve_llm_identity,
    stream_llm_resilient,
)
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.orchestrator.control_capability import (
    FINISH_TASK_NAME,
    WAIT_FOR_USER_CAPABILITY_ID,
)
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import effective_limit, now_utc
from ctx_weft.protocols import MemoryEvent, MemoryKind, MemoryScope

logger = logging.getLogger(__name__)


@dataclass
class TurnRecord:
    """Single LLM turn record (for Observer + memory)."""

    turn: int
    messages_sent: list[LLMMessage]
    assistant_text: str
    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    tool_results: list[Any] = dataclasses.field(default_factory=list)
    usage: LLMUsage = dataclasses.field(default_factory=LLMUsage)


class ActStep(Step):
    """ActStep: multi-turn LLM loop + capability invocation via CapabilityGateway."""

    name = "act"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        prompt = state.assembled_prompt
        if prompt is None:
            raise RuntimeError("ActStep requires assembled_prompt — PrepareStep must run first")

        agent = state.agent
        max_turns = agent.loop_config.max_turns_per_act
        transcript: list[TurnRecord] = []
        exit_reason = "normal"

        # 运行时态势 guidance（任务树/已完成子任务/收尾提醒）已随装配管线注入
        # （PrepareStep → GuidanceSource → composer 末条 user 尾部），此处不再修饰。
        current_messages = list(prompt.messages)
        # 动态 max_tokens 的增量基线：上一轮实际发送的 message 条数（= 本轮 usage 对应的真实
        # prompt 基线）。None=本轮无循环内真实基线（首轮）→ request_prompt_estimate 走整份估算。
        baseline_msg_count: int | None = None

        for turn_num in range(1, max_turns + 1):
            await _interrupt_checkpoint(state, ctx)
            await ctx.event_bus.emit(make_event(
                state, EventType.ACT_TURN_STARTED, payload={"turn": turn_num}))

            # 1) 单轮 LLM：流式累积文本 / reasoning / tool_calls / usage（软打断在内部 park）
            sent_msg_count = len(current_messages)  # 本轮发送条数（append 前）→ 下轮增量基线
            turn = await _run_llm_turn(state, ctx, prompt, current_messages, turn_num, baseline_msg_count)

            # 2) token 记账 + context_limit 判定
            context_limit_hit = await _account_tokens(state, ctx, turn.usage)
            # 仅当本轮真实刷新了 context_tokens（usage>0）才前移基线；否则保持旧值/None，
            # 让下一轮退回整份估算（更保守），避免基线与陈旧 context_tokens 错配。
            if turn.usage.prompt_tokens > 0:
                baseline_msg_count = sent_msg_count

            # 3) assistant 回合落 memory + 接回 message 历史 + 记 transcript
            asst_tool_dicts = await _ingest_assistant_turn(
                state, ctx, turn.text, turn.tool_calls, turn.usage, turn_num)
            current_messages.append(LLMMessage(
                role="assistant", content=turn.text, tool_calls=asst_tool_dicts,
                reasoning_content=turn.reasoning or None))
            turn_record = TurnRecord(
                turn=turn_num, messages_sent=list(current_messages),
                assistant_text=turn.text, tool_calls=turn.tool_calls, usage=turn.usage)

            # 4) context_limit 命中：记录本轮后停止
            if context_limit_hit:
                transcript.append(turn_record)
                await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                    "turn": turn_num, "reason": "context_limit"}))
                exit_reason = "context_limit"
                logger.warning(
                    "ActStep context_limit_hit: prompt_tokens=%d >= %d * 0.8 for agent %s",
                    turn.usage.prompt_tokens, agent.loop_guard.context_limit, agent.id)
                break

            # 5) 纯文本（无 tool call）：interactive 让位用户 / 否则即任务产出 → observe
            if not turn.tool_calls:
                transcript.append(turn_record)
                await _finish_plain_text_turn(state, ctx, turn_num)
                break

            # 6) 派发前压缩（含派发调用且越阈值时；在 dispatch 执行 / 子 spawn-inherit 之前）
            await _maybe_predispatch_compact(state, ctx, turn.tool_calls, turn.usage)

            # 7) 执行 tool calls（同批次延后退出信号，保证 tool_call ↔ result 一一对应）
            tool_results = await _execute_tool_calls(state, ctx, turn.tool_calls)
            for tr in tool_results:
                current_messages.append(LLMMessage(
                    role="tool", content=tr["result"], tool_call_id=tr["tool_call_id"]))
            turn_record.tool_results = tool_results
            transcript.append(turn_record)
            await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                "turn": turn_num, "reason": "tool_calls_processed"}))

            # finish_task 与 delegate 同批：finish 胜出（派发改投为独立后继）。
            _reconcile_finish_vs_dispatch(state, ctx, turn.tool_calls)

            if state.task.actor_done:
                exit_reason = "actor_done"
                break

        else:
            exit_reason = "max_turns"
            logger.warning("ActStep hit max_turns=%d for agent %s", max_turns, agent.id)
            await ctx.event_bus.emit(make_event(state, EventType.MAX_TURNS_REACHED, payload={
                "max_turns": max_turns}))

        # task.status == "SUSPENDED" 表示本 task 在等子任务，路由到 SuspendStep
        next_step = "suspend" if state.task.status == "SUSPENDED" else "observe"

        # ── task.outputs：收尾时合成最终交付物（spec 2026-07-01 反转契约）──
        # 收尾路径 = 纯文本收尾(normal) 或 finish_task 收尾(actor_done 且未挂起)；答复即消息正文，
        # finish_task 的 deliverables_summary 为可选产出小结。max_turns / context_limit /
        # delegate-suspend 不在此列（不产最终输出，维持现状）。
        if (
            exit_reason in ("normal", "actor_done")
            and state.task.status != "SUSPENDED"
            and transcript
        ):
            outputs = _compose_final_outputs(transcript)
            if outputs:
                state.task.outputs = outputs

        return StepOutcome(
            next_step=next_step,
            state_patch={
                "transcript": transcript,
                "act_exit_reason": exit_reason,
            },
        )


def _compose_final_outputs(transcript: list[TurnRecord]) -> str:
    """合成收尾交付物 = 收尾回合正文 + finish_task 的 deliverables_summary（spec 2026-07-01）。

    body = 收尾回合(transcript[-1])正文；空则回溯本段最近一段非空 assistant_text（兼容模型把
    答复写在上一回合、收尾回合只调 finish_task 的情况）。summary = 收尾回合 finish_task 调用的
    deliverables_summary（可空）。两段按存在情况拼接；全空返回 "" → 交给 observer 护栏。
    """
    last = transcript[-1]
    body = (last.assistant_text or "").strip()
    if not body:
        body = next(
            (t.assistant_text.strip() for t in reversed(transcript)
             if (t.assistant_text or "").strip()),
            "",
        )
    summary = ""
    for tc in last.tool_calls:
        if tc.name == FINISH_TASK_NAME:
            val = (tc.arguments or {}).get("deliverables_summary", "")
            summary = val.strip() if isinstance(val, str) else ""
            break
    if body and summary:
        return f"{body}\n\n{summary}"
    return body or summary


@dataclass
class _LLMTurnOutput:
    """单轮 LLM 流式产出。"""

    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    usage: LLMUsage


async def _run_llm_turn(
    state: LoopState, ctx: LoopContext, prompt: Any, current_messages: list[LLMMessage],
    turn_num: int, baseline_msg_count: int | None = None,
) -> _LLMTurnOutput:
    """发请求事件 → 流式累积 token/reasoning/tool_calls/usage → 处理软打断 → 发 RESPONSE_FINISHED。

    软打断（pause）时提交半截 assistant 文本并 park（raises HitlPark），不返回；硬取消 →
    CancelledError。正常结束返回 _LLMTurnOutput。
    """
    agent = state.agent
    model, llm_account = resolve_llm_identity(state)
    req_id = f"req_{agent.id}_{state.sequence_counter}"
    await ctx.event_bus.emit(make_event(state, EventType.LLM_REQUEST_STARTED, payload={
        "request_id": req_id, "model": model, "llm_account": llm_account, "turn": turn_num}))

    llm_request = LLMRequest(
        model=model, system=prompt.system, messages=list(current_messages), tools=prompt.tools)
    llm_request.prompt_token_estimate = request_prompt_estimate(
        ctx.llm.tokenizer, llm_request, getattr(agent, "loop_guard", None), baseline_msg_count)

    await ctx.event_bus.emit(make_event(state, EventType.LLM_PROMPT_SENT, payload={
        "request_id": req_id, "turn": turn_num, "system": prompt.system,
        "messages": [
            {"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
            for m in current_messages
        ],
        "tool_names": [t.name for t in prompt.tools]}))

    text = ""
    reasoning = ""
    tool_calls: list[ToolCall] = []
    usage = LLMUsage()
    interrupted = False

    async for chunk in stream_llm_resilient(ctx, state, llm_request):
        tok = ctx.cancel_token
        if _interrupt_pending(ctx):
            interrupted = True          # ② 软打断：停收 token，下面提交半截
            break
        if tok is not None and tok.is_cancelled:
            tok.raise_if_cancelled()    # 硬取消 → CancelledError
        if chunk.kind == "token":
            ctx.run_phase.produced = True
            text += chunk.text
            await ctx.event_bus.emit(make_event(
                state, EventType.LLM_TOKEN_STREAMED,
                payload={"request_id": req_id, "delta": chunk.text}))
        elif chunk.kind == "reasoning":
            reasoning += chunk.text
            await ctx.event_bus.emit(make_event(
                state, EventType.LLM_REASONING_STREAMED,
                payload={"request_id": req_id, "delta": chunk.text}))
        elif chunk.kind == "tool_call" and chunk.tool_call is not None:
            tool_calls.append(chunk.tool_call)
        elif chunk.kind == "tool_call_partial":
            # 工具调用参数流式期间的心跳：不携带数据，仅为让上面的暂停/取消检查点
            # 有机会触发（adapter 累积工具调用参数时不产出 token）。此处刻意不累积。
            pass
        elif chunk.kind == "usage" and chunk.usage is not None:
            usage = chunk.usage

    if interrupted:
        # ② 已吐 token：把半截 assistant 文本入 memory 并标注「被用户打断」；
        # ① 未吐任何内容：不留记录、续接补说明。随后 park 待用户续接。
        has_partial = bool(text.strip() or reasoning.strip())
        await _commit_interrupted_partial(state, ctx, text, reasoning, turn_num)
        if _is_own_root(state.task):
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(state, ctx, boundary="interrupt")
        await _park_wait_for_user(state, ctx, source="interrupt", edit=not has_partial)

    # token 自校准回喂：真实 usage 与发送前估算段作比（基线不参与），喂给该模型 tokenizer。
    # 估算段取 PROMPT_EST_SEG_KEY（tokenizer.count 直接产出）而非
    # prompt_token_estimate − base——整份路径上返回值可能被 max(full, ctx_tokens) floor 成
    # 上一轮真实值，拿 floor 后的值回喂会产出 ratio≈1 的假样本，把伺服系统性拖向 1。
    base = llm_request.metadata.get(PROMPT_EST_BASE_KEY)
    est_seg = llm_request.metadata.get(PROMPT_EST_SEG_KEY)
    if usage.prompt_tokens > 0 and est_seg is not None and base is not None:
        ctx.llm.tokenizer.observe(est_seg, usage.prompt_tokens - base)

    await ctx.event_bus.emit(make_event(
        state, EventType.LLM_RESPONSE_FINISHED,
        payload={
            "request_id": req_id, "content": text, "reasoning": reasoning,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
            ],
            "usage": dataclasses.asdict(usage),
            # 本次调用实际使用的模型/账号（host 云端上报按此计账，不再受切换竞态影响）
            "llm_model": model, "llm_account": llm_account,
            "finish_reason": "tool_use" if tool_calls else "stop", "turn": turn_num}))

    return _LLMTurnOutput(text=text, reasoning=reasoning, tool_calls=tool_calls, usage=usage)


async def _account_tokens(state: LoopState, ctx: LoopContext, usage: LLMUsage) -> bool:
    """更新 LoopGuard 基线 + session.token_used；返回是否命中 context_limit（80% 阈值）。

    对齐 miniAgents actor.py context_limit_hit 逻辑。
    """
    agent = state.agent
    if usage.prompt_tokens > 0:
        agent.loop_guard.context_tokens = usage.prompt_tokens
        try:
            from ctx_weft.protocols import MemoryKind, MemoryScope

            # v2 P3a：len(视图谓词) 取代 count_recent（= 旧 USER_PROMPT+LLM_RESPONSE 口径，
            # 与 prepare._estimate_tokens 对齐）。
            view = await ctx.memory.load_view(state.scope, MemoryScope.TASK, ctx.provider_ctx)
            agent.loop_guard.context_message_count = sum(
                1 for r in view
                if r.kind is MemoryKind.CONVERSATION_TURN and r.role in ("user", "assistant")
            )
        except Exception as exc:
            # best-effort 计数：失败不影响主流程，但记 debug 便于排查（不静默吞）。
            logger.debug("load_view count for loop_guard failed: %s", exc)

    # 累加 session.token_used（供 token_budget 检查使用）
    state.session.token_used += usage.prompt_tokens + usage.completion_tokens

    context_limit = agent.loop_guard.context_limit
    reserve = getattr(agent.loop_guard, "reserved_output_tokens", 0)
    eff = effective_limit(context_limit, reserve)
    return (
        eff > 0
        and usage.prompt_tokens > 0
        and usage.prompt_tokens >= int(eff * 0.8)
    )


async def _ingest_assistant_turn(
    state: LoopState, ctx: LoopContext, text: str, tool_calls: list[ToolCall],
    usage: LLMUsage, turn_num: int,
) -> list[dict]:
    """把本轮 assistant 回合入 task 层 memory；返回完整 tool_call dicts 供 message 重建。

    派发(submit_*)/silent 工具的 tool_call 排除出 LLM_RESPONSE.metadata（派发落 agent 层
    delegate conversation turn、silent 结果不入对话），避免无配对 TOOL_RESULT 的悬挂调用破坏
    无损重建（spec 2026-06-28 §2.3）。
    """
    from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS, SILENT_TOOLS
    asst_tool_dicts = [{"id": tc.id, "name": tc.name, "input": tc.arguments} for tc in tool_calls]
    _excluded = DISPATCH_TOOLS | SILENT_TOOLS
    non_dispatch_tool_dicts = [d for d in asst_tool_dicts if d["name"] not in _excluded]
    await ctx.memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=text,
            timestamp=now_utc(),
            role="assistant",
            metadata={
                "turn": turn_num,
                "tool_call_count": len(tool_calls),
                "tool_calls": non_dispatch_tool_dicts,
                "usage": dataclasses.asdict(usage),
            },
        ),
        ctx.provider_ctx,
    )
    return asst_tool_dicts


async def _maybe_predispatch_compact(
    state: LoopState, ctx: LoopContext, tool_calls: list[ToolCall], usage: LLMUsage,
) -> None:
    """本轮含派发调用且越过 predispatch 阈值 → 派发执行前先走一遍 compact（同 CompactStep）。

    在 gateway 写 delegate conversation turn / 子 spawn-inherit 之前完成，使子继承到压缩后的记忆；每轮至多一次。
    随后 dispatch 执行（父转 SUSPENDED）→ 本 Act 末尾路由到 SuspendStep。usage.prompt_tokens 是本轮真实计数。
    """
    from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS
    if not any(tc.name in DISPATCH_TOOLS for tc in tool_calls):
        return
    from ctx_weft.core.loop.steps.compact import maybe_compact_before_dispatch
    for ev in await maybe_compact_before_dispatch(state, ctx, prompt_tokens=usage.prompt_tokens):
        await ctx.event_bus.emit(ev)


async def _execute_tool_calls(
    state: LoopState, ctx: LoopContext, tool_calls: list[ToolCall],
) -> list[dict]:
    """逐个经 CapabilityGateway 执行 tool calls，返回 tool_results。

    工具间 / 执行中命中软打断 → 给本 tc 及之后补「已取消/被打断」result + park（raises HitlPark）；
    硬取消 → CancelledError；被 park 的工具 → task 转 SUSPENDED 并上抛（spec/07 §7）。
    """
    tool_results: list[dict] = []
    ctx.run_phase.in_tool_loop = True

    for i, tc in enumerate(tool_calls):
        # 工具间命中：软打断 → 本 tc 及之后全部「未开始」→ 补「已取消」+ park；硬取消 → CancelledError。
        if _interrupt_pending(ctx):
            for rest in tool_calls[i:]:
                await _ingest_synthetic_tool_result(state, ctx, rest, CANCELLED_MARK, cancelled=True)
            ctx.run_phase.in_tool_loop = False
            if _is_own_root(state.task):
                from ctx_weft.core.loop.steps.background_observe import launch_background_observe
                launch_background_observe(state, ctx, boundary="interrupt")
            await _park_wait_for_user(state, ctx, source="interrupt")
        if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
            ctx.cancel_token.raise_if_cancelled()

        # 可取消地执行在途工具：执行中被打断 → 取消该工具、补「被打断」result。
        invoke_task = asyncio.ensure_future(_invoke_tool(tc, state, ctx))
        completed = await _await_tool_or_stop(invoke_task, ctx)
        if not completed:
            if _interrupt_pending(ctx):
                await _ingest_synthetic_tool_result(state, ctx, tc, INTERRUPTED_MARK, interrupted=True)
                for rest in tool_calls[i + 1:]:
                    await _ingest_synthetic_tool_result(state, ctx, rest, CANCELLED_MARK, cancelled=True)
                ctx.run_phase.in_tool_loop = False
                if _is_own_root(state.task):
                    from ctx_weft.core.loop.steps.background_observe import launch_background_observe
                    launch_background_observe(state, ctx, boundary="interrupt")
                await _park_wait_for_user(state, ctx, source="interrupt")
            if ctx.cancel_token is not None:
                ctx.cancel_token.raise_if_cancelled()  # 硬取消
        try:
            result = invoke_task.result()
        except HitlPark:
            # 被 park 的工具未执行；task 落 SUSPENDED 并 unwind（spec/07 §7）。
            state.task.status = "SUSPENDED"
            raise

        tool_results.append({
            "tool_call_id": tc.id,
            "name": tc.name,
            "result": result.content,
            "is_error": result.is_error,
        })

    ctx.run_phase.in_tool_loop = False
    return tool_results


def _reconcile_finish_vs_dispatch(
    state: LoopState, ctx: LoopContext, tool_calls: list[ToolCall],
) -> None:
    """finish_task 与 delegate_task / delegate_plan 同批出现时仲裁：finish 胜出。

    两类工具语义互斥——一个要当前 task 收尾(→observe)，一个要它挂起等子任务(→suspend)。
    用户意图是「我做完了，顺手派生独立后续」：故 finish 胜出，被派发任务从「当前 task 的
    阻塞子任务」改投为「当前 task 的 parent 名下的独立后继」(当前是 root 则为顶层)，自行调度。

    - detach_staged：把本轮 staged 子任务改挂到 parent，切断与收尾 task 的阻塞链。
    - status 复位为 ACTIVE：撤销 delegate 置的 SUSPENDED，使路由走 observe(task.outputs
      已由 finish_task 写好)。
    - 清 spawn_titles：避免 SuspendStep 误报(虽已不路由到 suspend，仍清掉防脏状态)。

    顺序无关：只看本批最终是否两类工具都出现。
    """
    from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS
    if ctx.task_manager is None:
        return
    names = {tc.name for tc in tool_calls}
    if FINISH_TASK_NAME not in names or not any(n in DISPATCH_TOOLS for n in names):
        return
    ctx.task_manager.detach_staged(state.task.id, state.task.parent_task_id)
    state.task.status = "ACTIVE"
    if isinstance(state.task.settings, NormalTaskSettings):
        state.task.settings.spawn_titles = []
    logger.info(
        "act: finish_task + dispatch in same batch on task %s — finishing it; "
        "detaching delegated work to parent %s",
        state.task.id, state.task.parent_task_id,
    )


async def _finish_plain_text_turn(state: LoopState, ctx: LoopContext, turn_num: int) -> None:
    """纯文本回合（无 tool call）收尾。

    interactive 普通任务：让位给用户 → HITL input 冷 park（raises HitlPark；用户回复经 runtime
    冷 resume 作 USER_PROMPT 注入后重入 act）。auto / 非普通任务 / 无 hitl_manager：纯文本即任务
    产出，发 stop 事件路由 observe。
    """
    if (
        isinstance(state.task.settings, NormalTaskSettings)
        and state.task.interaction_mode == "interactive"
        and ctx.hitl_manager is not None
    ):
        await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
            "turn": turn_num, "reason": "await_user"}))
        # 纯文本暂停 = 软待命(允许但不强制回复) → PAUSED,区别于 ask_user 的 PAUSED_HITL。
        if _is_own_root(state.task):
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(state, ctx, boundary="plain_text")
        await _park_wait_for_user(state, ctx, source="plain_text")
    await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
        "turn": turn_num, "reason": "stop"}))


async def _invoke_tool(tc: ToolCall, state: LoopState, ctx: LoopContext) -> Any:
    """通过 CapabilityGateway 执行工具调用。gateway 不存在时降级为旧路径。"""
    if ctx.capability_gateway is not None:
        return await ctx.capability_gateway.invoke(
            tool_name=tc.name,
            arguments=tc.arguments,
            state=state,
            ctx=ctx,
            tool_call_id=tc.id,
        )

    # 降级：无 gateway 时的最简路径（保持向后兼容）
    from ctx_weft.core.loop.capability_gateway import CapabilityGateway, InvocationResult
    logger.warning("No CapabilityGateway in LoopContext; tool '%s' cannot be dispatched", tc.name)
    return InvocationResult(
        invocation_id="noop",
        tool_name=tc.name,
        content=f"[Error: CapabilityGateway not configured, tool '{tc.name}' skipped]",
        is_error=True,
    )


INTERRUPTED_MARK = "[interrupted by the user]"
CANCELLED_MARK = "[cancelled]"


async def _ingest_synthetic_tool_result(
    state: LoopState, ctx: LoopContext, tc: ToolCall, content: str, *,
    interrupted: bool = False, cancelled: bool = False,
) -> None:
    """为被打断/未执行的工具补一条 TOOL_RESULT，使 tool_call↔result 一一对应（无 dangling）。"""
    await ctx.memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=content,
            timestamp=now_utc(),
            role="tool",
            metadata={
                "tool_name": tc.name,
                "tool_call_id": tc.id,
                "is_error": True,
                "interrupted": interrupted,
                "cancelled": cancelled,
            },
        ),
        ctx.provider_ctx,
    )


async def _await_tool_or_stop(invoke_task: "asyncio.Future", ctx: LoopContext) -> bool:
    """运行在途工具；任一停止信号（硬取消 cancel_token / 软打断 pause_token）在执行中触发
    → 取消该工具并等其清理跑完，返回 False；正常完成返回 True。
    """
    waiters: list[asyncio.Future] = []
    if ctx.cancel_token is not None:
        waiters.append(asyncio.ensure_future(ctx.cancel_token.wait()))
    if ctx.pause_token is not None:
        waiters.append(asyncio.ensure_future(ctx.pause_token.wait_paused()))
    if not waiters:
        await asyncio.wait({invoke_task})
        return True
    try:
        await asyncio.wait({invoke_task, *waiters}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        await asyncio.wait(set(waiters))
    if invoke_task.done():
        return True
    invoke_task.cancel()
    await asyncio.wait({invoke_task})
    return False


def _interrupt_pending(ctx: LoopContext) -> bool:
    """软打断挂起中：pause_token 被 pause 且有 hitl_manager 可 park。"""
    tok = ctx.pause_token
    return (
        tok is not None
        and tok.is_paused
        and ctx.hitl_manager is not None
    )


def _is_own_root(task) -> bool:
    """root task 判定（与 observe._is_own_root / finalize._close_one 保持一致）。

    True  → session root（parent_task_id is None）或跨 agent own-root。
    False → 同 agent 子任务（parent 非空且 creator==assigned）。
    """
    same_agent = task.creator_agent_id == task.assigned_agent_id
    cross_agent = bool(task.parent_task_id) and not same_agent
    return task.parent_task_id is None or cross_agent


async def _commit_interrupted_partial(
    state: LoopState, ctx: LoopContext, text: str, reasoning: str, turn_num: int,
) -> None:
    """把被打断的半截 assistant 文本入 memory（标注「被用户打断」）。无内容则不留记录（①）。"""
    if not (text.strip() or (reasoning or "").strip()):
        return
    content = f"{text}\n\n{INTERRUPTED_MARK}" if text.strip() else INTERRUPTED_MARK
    await ctx.memory.ingest(
        MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
            address=state.scope,
            content=content,
            timestamp=now_utc(),
            role="assistant",
            metadata={
                "turn": turn_num,
                "interrupted": True,
                "reasoning": reasoning or None,
            },
        ),
        ctx.provider_ctx,
    )


def _interrupt_edit_prefix(prev_request: str) -> str:
    """产出①打断续接的前缀说明；prev 为空则返回空串。

    抽出成纯文本→纯文本的小函数，供多模态调用点用 content_with_prefix 组装
    （new_input 可能是 list[ContentPart]，f-string 直接拼会把图片拍扁）。
    """
    prev = (prev_request or "").strip()
    if not prev:
        return ""
    return f'(I cancelled my previous request: "{prev}" — replacing it with the request below.)\n\n'


def interrupt_edit_note(prev_request: str, new_input: str) -> str:
    """① 打断（未吐 token）续接时的说明：上一条请求被取消、改为新请求。空 prev 时原样返回。"""
    prefix = _interrupt_edit_prefix(prev_request)
    return f"{prefix}{new_input}" if prefix else new_input


async def _park_wait_for_user(
    state: LoopState, ctx: LoopContext, *, source: str, edit: bool = False,
) -> None:
    """起 wait_for_user 冷 park：会话 PAUSED、任务 SUSPENDED，抛 HitlPark。

    ``source`` 标记触发来源（``plain_text`` 纯文本暂停 / ``interrupt`` 中途打断），供前端区分。
    ``edit=True``（仅 interrupt 的 ① 阶段，未吐 token/未进工具）→ context=``interrupt:edit``，
    续接时 runtime 补「上一条取消」说明。
    """
    context = "interrupt:edit" if (source == "interrupt" and edit) else source
    rid = await ctx.hitl_manager.request_parked(
        form="wait",
        session_id=state.session.id,
        task_id=state.task.id,
        agent_id=state.agent.id,
        capability_id=WAIT_FOR_USER_CAPABILITY_ID,
        question="",
        context=context,
    )
    state.session.status = "PAUSED"
    state.task.status = "SUSPENDED"
    raise HitlPark(hitl_id=rid)


async def _interrupt_checkpoint(state: LoopState, ctx: LoopContext) -> None:
    """协作式停止点：软打断（pause）→ park；硬取消（cancel）→ CancelledError。"""
    if _interrupt_pending(ctx):
        edit = not ctx.run_phase.produced and not ctx.run_phase.in_tool_loop
        if _is_own_root(state.task):
            from ctx_weft.core.loop.steps.background_observe import launch_background_observe
            launch_background_observe(state, ctx, boundary="interrupt")
        await _park_wait_for_user(state, ctx, source="interrupt", edit=edit)  # raises HitlPark
    tok = ctx.cancel_token
    if tok is not None and tok.is_cancelled:
        tok.raise_if_cancelled()
