"""Observe execution: task judgement (optional) and execution-history recap.

The driver selects foreground/background execution. This step owns observation,
judgement and settlement in both modes.
"""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from ctx_weft.core.assembler import ContextRequest, ReviewIntent
from ctx_weft.core.assembler.prompts import connector, runtime_prompt
from ctx_weft.core.capabilities.control_tools import REPORT_OBSERVATION_NAME
from ctx_weft.core.capabilities.control_tools import ControlMetaKey as K
from ctx_weft.core.loop.driver import (
    ExecutionMode,
    LoopContext,
    LoopState,
    Step,
    StepDriver,
    StepExecution,
    StepOutcome,
    make_event,
)
from ctx_weft.core.loop.llm_gateway import (
    request_prompt_estimate,
    resolve_llm_identity,
    stream_llm_resilient,
)
from ctx_weft.core.loop.task_memory_fold import (
    pop_close_synth,
    put_close_report,
    replace_finish_report,
)
from ctx_weft.core.orchestrator.task.disposition import RunOutcome, RunOutcomeKind
from ctx_weft.core.utils.clock import now_utc
from ctx_weft.core.utils.content import content_to_text, image_tokens
from ctx_weft.core.utils.ids import generate_id, mint_turn_call_ids
from ctx_weft.core.utils.verdict import (
    VERDICT_CONTINUE,
    VERDICT_FAIL,
    VERDICT_SUCCESS,
    normalize_verdict,
)
from ctx_weft.protocols import LLMMessage, LLMRequest, LLMUsage, MemoryScope
from ctx_weft.protocols.events import EventType

if TYPE_CHECKING:
    from ctx_weft.core.capabilities.control_tools import ControlResult

logger = logging.getLogger(__name__)
_PLAIN_TEXT_NEEDS_FINISH = connector("act.review_feedback.plain_text_needs_finish")


CLOSE_BOUNDARIES = {"finish", "normal"}
JUDGING_BOUNDARIES = {"plain_text", "finish_park"}


def judges(boundary: str) -> bool:
    return boundary in JUDGING_BOUNDARIES


def has_observe_role(state: "Any") -> bool:
    """A nonempty host ROLE enables judgement and finish-park review.

    Missing ROLE causes mechanical fallback in the step. Background observation
    defaults to recap-only for those agents; the actor does not park finish for
    a judgement that cannot run.
    """
    template = state.extra.get("template")
    if template is None:
        return False
    facet = template.identity.get("observe")
    # getattr：与 composer 取 facet 正文的口径一致（手构 request / 鸭子类型替身在本仓很常见）
    return facet is not None and bool((getattr(facet, "text", "") or "").strip())


def _execution(ctx) -> StepExecution:
    return getattr(ctx, "execution", StepExecution())


def _is_own_root(task) -> bool:
    return task.parent_task_id is None or task.creator_agent_id != task.assigned_agent_id


@dataclass
class Verdict:
    """Final business judgement, not a task-status transition."""
    task_outcome: str
    act_recap: str
    task_summary: str = ""
    reported: bool = False
    next_step_hint: str = ""
    failure_reason: str = ""


@dataclass
class ObservationResult:
    """A recap may exist without a verdict, and a mechanical verdict without a recap."""
    act_recap: str = ""
    task_summary: str = ""
    verdict: Verdict | None = None

    def metadata(self) -> dict:
        result = {K.OBSERVER_ACT_RECAP: self.act_recap,
                  K.OBSERVER_TASK_SUMMARY: self.task_summary}
        if self.verdict is not None:
            result.update({
                K.OBSERVER_OUTCOME: self.verdict.task_outcome,
                K.OBSERVER_NEXT_STEP_HINT: self.verdict.next_step_hint,
                K.OBSERVER_FAILURE_REASON: self.verdict.failure_reason,
            })
        return result


class ObserveStep(Step):
    name = "observe"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        execution = _execution(ctx)
        boundary = self.boundary_or_exit(state, execution.options.get("boundary"))
        background = execution.mode == ExecutionMode.BACKGROUND
        judge_task = execution.options.get(
            "judge_task", judges(boundary) and has_observe_role(state) if background else True)
        if background:
            return await self._execute_background(state, ctx, boundary, judge_task)

        events = [make_event(state, EventType.OBSERVE_STARTED, {"task_id": state.task.id})]
        result = await self._observe(state, ctx, events, judge_task, boundary)
        verdict = result.verdict
        used_llm = verdict is not None and verdict.reported
        if verdict is not None:
            if used_llm:
                self._apply_assessment(state.task, verdict)
            elif boundary in ("max_turns", "context_limit"):
                state.task.observer_outcome = verdict.task_outcome
                state.task.task_summary = verdict.task_summary
                state.task.actor_done = True
            if verdict.next_step_hint:
                state.task.next_step_hint = verdict.next_step_hint
            if verdict.task_outcome == VERDICT_CONTINUE:
                await self._fold_retry_segment(state, ctx, verdict, events)
            if not used_llm:
                recap_boundary = "mechanical"
                if (state.act_exit_reason in ("normal", "actor_done")
                        and verdict.task_outcome != VERDICT_CONTINUE and _is_own_root(state.task)):
                    recap_boundary = "finish" if state.act_exit_reason == "actor_done" else "normal"
                StepDriver.for_context(ctx).start(
                    "observe", state, ctx,
                    options={"boundary": recap_boundary, "judge_task": False})
        events.append(make_event(state, EventType.OBSERVE_COMPLETED, {
            "task_id": state.task.id, "outcome": verdict.task_outcome if verdict else "",
            "summary_length": len(result.act_recap), "used_llm": used_llm}))
        return self._outcome(result, "finalize" if judge_task else None, events)

    @staticmethod
    def _outcome(result, next_step=None, events=None):
        patch = {"observation": result}
        if result.verdict is not None:
            patch["verdict"] = result.verdict
        return StepOutcome(next_step, state_patch=patch, events=events or [])

    async def _observe(self, state, ctx, events, judge_task, boundary):
        result = ObservationResult()
        tok = getattr(ctx, "cancel_token", None)
        background = _execution(ctx).mode == ExecutionMode.BACKGROUND
        cancelled = tok is not None and tok.is_cancelled
        if ((background or not cancelled)
                and (not judge_task or self._should_use_llm(state))):
            try:
                result = await self._llm_observe(state, ctx, events)
            except Exception as exc:
                logger.warning("ObserveStep LLM failed; retaining raw history: %s", exc)
        if judge_task:
            verdict = result.verdict
            if verdict is None:
                verdict = dataclasses.replace(
                    self._mechanical_verdict(state, boundary),
                    act_recap=result.act_recap, task_summary=result.task_summary)
            if boundary in ("max_turns", "context_limit"):
                verdict = dataclasses.replace(verdict, task_outcome=VERDICT_CONTINUE)
            result.verdict = verdict
        return result

    def boundary_or_exit(self, state, boundary=None) -> str:
        return boundary or state.act_exit_reason or "normal"

    async def _llm_observe(self, state, ctx, events) -> ObservationResult:
        execution = _execution(ctx)
        background = execution.mode == ExecutionMode.BACKGROUND
        boundary = self.boundary_or_exit(state, execution.options.get("boundary"))
        judge_task = execution.options.get(
            "judge_task", judges(boundary) and has_observe_role(state) if background else True)
        cache = ctx.capability_cache
        # Session-global control tools survive per-agent cache eviction.
        bound = cache.get(state.agent.id) if cache is not None else []
        extra = {"observe_boundary": boundary}
        if judge_task:
            extra["subtasks"] = self._subtask_handles(state, ctx)
        request = ContextRequest(
            purpose="observe", scope=state.scope, task=state.task, agent=state.agent,
            session=state.session, template=state.extra.get("template"),
            bound_capabilities=bound, token_counter=ctx.llm.tokenizer.count,
            extra=extra, intent=ReviewIntent(judge_task=judge_task),
        )
        prompt = await ctx.assembler.assemble(request)
        submitted, last_text = await self._run_react(
            state, ctx, system=prompt.system, messages=list(prompt.messages),
            tools=prompt.tools, max_rounds=state.agent.loop_config.max_turns_per_observe,
            terminal_tool_name=REPORT_OBSERVATION_NAME,
            validate_submission=lambda result: self._submission_error(result, judge_task=judge_task),
        )
        return self._interpret_submission(state, submitted, last_text,
                                          judge_task=judge_task, background=background)

    async def _run_react(
        self,
        state: "Any",
        ctx: "Any",
        *,
        system: str,
        messages: "list[LLMMessage]",
        tools: "Any",
        max_rounds: int,
        terminal_tool_name: str,
        validate_submission: Callable[[Any], str | None] | None = None,
    ) -> "tuple[ControlResult | None, str]":
        """Run observation rounds until a valid terminal submission or the round limit.

        Returns the accepted tool result (or None) and the last nonempty text.
        Only advertised tools may execute; submission validation comes from this step.
        This method does not apply task judgement or fold memory.

        The driver supplies the foreground/background event origin. The LLM gateway
        emits streaming events; this method emits LLM_RESPONSE_FINISHED.
        """
        agent = state.agent
        model, llm_account = resolve_llm_identity(state)
        current_messages = list(messages)
        last_text = ""
        # 动态 max_tokens 的增量基线：上一轮实际发送条数（本轮 usage 对应的真实 prompt 基线）。
        baseline_msg_count: int | None = None

        for round_num in range(max_rounds):
            # req_id：与 llm_gateway.stream_llm_resilient 内部同一确定性公式独立算出（同
            # act.py._run_llm_turn 的手法）——两边都在本次 LLM 调用任何事件发射前求值，故
            # state.sequence_counter 两处读到同一个值，天然一致，不需要新增参数或跨函数传值。
            req_id = f"req_{agent.id}_{state.sequence_counter}"

            sent_msg_count = len(current_messages)  # 本轮发送条数（append 前）→ 下轮增量基线
            llm_request = LLMRequest(
                model=model,
                system=system,
                messages=list(current_messages),
                tools=tools,
            )
            llm_request.prompt_token_estimate = request_prompt_estimate(
                ctx.llm.tokenizer, llm_request, getattr(agent, "loop_guard", None), baseline_msg_count)
            # turn：gateway 从 metadata["turn"] 取（spec §9.4 统一口径：LLM_* 事件一律用 turn，
            # 不用 round）；round_num 仍是本函数内部的轮次计数局部变量，只是不再直接进 payload。
            llm_request.metadata["turn"] = round_num

            accumulated_text = ""
            reasoning_text = ""
            tool_calls = []
            usage = LLMUsage()

            async for chunk in stream_llm_resilient(ctx, state, llm_request):
                if chunk.kind == "token":
                    accumulated_text += chunk.text
                elif chunk.kind == "reasoning":
                    reasoning_text += chunk.text
                elif chunk.kind == "tool_call" and chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
                elif chunk.kind == "usage" and chunk.usage is not None:
                    usage = chunk.usage

            if accumulated_text:
                last_text = accumulated_text

            # 摄入前铸造内部调用标识（spec: conversation-integrity，与 act._run_llm_turn 同口径）：
            # observer 回合不入 task 层 memory（工具是 SILENT/控制面），锚仅取唯一性、不持久；
            # live 消息面（current_messages 的 tool_call↔result 配对）与事件 payload 用同一份值。
            if tool_calls:
                tool_calls = [
                    m.call for m in mint_turn_call_ids(
                        tool_calls, anchor=generate_id("asst"), turn_seq=round_num)
                ]

            # 更新 loop_guard（对齐 miniAgents _run_observer：取 actor/observer 的最大值）
            if usage.prompt_tokens > 0:
                agent.loop_guard.context_tokens = max(
                    agent.loop_guard.context_tokens, usage.prompt_tokens
                )
                baseline_msg_count = sent_msg_count  # 真实刷新才前移基线（否则下轮退回整份估算）
            # 同步累加 session.token_used
            state.session.token_used += usage.prompt_tokens + usage.completion_tokens

            await ctx.event_bus.emit(make_event(
                state, EventType.LLM_RESPONSE_FINISHED,
                payload={
                    "request_id": req_id,
                    "content": accumulated_text,
                    "reasoning": reasoning_text,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
                    ],
                    "usage": dataclasses.asdict(usage),
                    # 本次调用实际使用的模型/账号（host 云端上报按此计账，不受切换竞态影响）
                    "llm_model": model,
                    "llm_account": llm_account,
                    "finish_reason": "tool_use" if tool_calls else "stop",
                    "turn": round_num,
                },
            ))

            if not tool_calls:
                # 纯文本轮不直接放弃：observer 常把复述写成正文而忘了调 terminal 工具——
                # 还有剩余轮次时催促其改用工具提交后重试；耗尽轮次才返回 (None, last_text)。
                if round_num + 1 >= max_rounds:
                    break
                current_messages.append(LLMMessage(
                    role="assistant", content=accumulated_text or runtime_prompt('no_reply'),
                ))
                current_messages.append(LLMMessage(
                    role="user",
                    content=connector("review.react_nudge", terminal_tool=terminal_tool_name),
                ))
                continue

            current_messages.append(LLMMessage(
                role="assistant",
                content=accumulated_text,
                tool_calls=[{"id": tc.id, "name": tc.name, "input": tc.arguments} for tc in tool_calls],
            ))

            terminal_result = None
            for tc in tool_calls:
                if tc.name not in {tool.name for tool in tools}:
                    content = runtime_prompt("observation_tool_unavailable", tool=tc.name)
                elif ctx.capability_gateway is not None:
                    result = await ctx.capability_gateway.invoke(
                        tool_name=tc.name,
                        arguments=tc.arguments,
                        state=state,
                        ctx=ctx,
                        tool_call_id=tc.id,
                    )
                    content = result.content
                    # terminal 工具的失败（is_error，如参数非法）不终止循环：错误内容照常 append
                    # 进 messages（下方）供模型下一轮改参重试；只有成功结果才是终止结果。否则一次
                    # 坏调用的错误文案会被当成 Process Report 终结整个观察（spec: capability-gateway）。
                    if tc.name == terminal_tool_name and not result.is_error:
                        error = validate_submission(result) if validate_submission else None
                        if error:
                            content = error
                        else:
                            terminal_result = result
                else:
                    logger.warning(
                        "run_observe_react: no CapabilityGateway for tool '%s'", tc.name
                    )
                    content = runtime_prompt('gateway_missing', tool=tc.name)
                current_messages.append(LLMMessage(
                    role="tool",
                    content=content,
                    tool_call_id=tc.id,
                ))

            if terminal_result is not None:
                return terminal_result, last_text

        return None, last_text

    def _interpret_submission(self, state, submitted, last_text="", *, judge_task=True, background=False) -> ObservationResult:
        meta = (submitted.metadata or {}) if submitted is not None else {}
        recap = meta.get(K.OBSERVER_ACT_RECAP, "") or ""
        # Preserve the existing background plain-text recap fallback; it is never a verdict.
        if background and not recap:
            recap = content_to_text(last_text or "").strip()
        elif submitted is not None and not recap:
            recap = content_to_text(last_text or "")[:500]
        result = ObservationResult(recap, meta.get(K.OBSERVER_TASK_SUMMARY, "") or "")
        if judge_task and submitted is not None and meta.get(K.OBSERVER_OUTCOME):
            outcome = normalize_verdict(meta[K.OBSERVER_OUTCOME])
            hint = meta.get(K.OBSERVER_NEXT_STEP_HINT, "") or ""
            hint = f"Next Step Hint: {hint}" if hint else ""
            # A successful model verdict still requires an actual final deliverable.
            if outcome == VERDICT_SUCCESS and not state.task.outputs:
                outcome = VERDICT_CONTINUE
                missing_output = runtime_prompt("observation_missing_output")
                hint = f"{hint}\n\n{missing_output}" if hint else missing_output
            failure_reason = meta.get(K.OBSERVER_FAILURE_REASON, "") or ""
            if outcome == VERDICT_SUCCESS:
                failure_reason = ""
            result.verdict = Verdict(outcome, result.act_recap, result.task_summary,
                                     reported=True, next_step_hint=hint,
                                     failure_reason=failure_reason)
        return result

    def _submission_error(self, result, *, judge_task=True) -> str | None:
        if judge_task and not str((result.metadata or {}).get(K.OBSERVER_OUTCOME) or "").strip():
            return runtime_prompt("observation_missing_verdict")
        return None

    def _mechanical_verdict(self, state, boundary=None) -> Verdict:
        boundary = self.boundary_or_exit(state, boundary)
        # A parked segment is not evidence of task completion. Failed background
        # judgement must never silently release successors or close the task.
        if boundary in ("plain_text", "finish_park"):
            return Verdict(VERDICT_CONTINUE, "")
        if not state.transcript:
            return Verdict(VERDICT_FAIL, "")
        if boundary in ("max_turns", "context_limit"):
            return Verdict(VERDICT_CONTINUE, "")
        if boundary == "normal":
            return Verdict(VERDICT_CONTINUE, "", next_step_hint=_PLAIN_TEXT_NEEDS_FINISH)
        return Verdict(VERDICT_SUCCESS, "")

    @staticmethod
    def _apply_assessment(task, verdict: Verdict) -> None:
        """Apply an accepted foreground assessment, never task.status."""
        task.process_report = verdict.act_recap
        task.process_report_at = now_utc()
        task.task_summary = verdict.task_summary
        task.next_step_hint = verdict.next_step_hint or None
        task.observer_outcome = verdict.task_outcome
        task.actor_done = True
        task.error = None if verdict.task_outcome == VERDICT_SUCCESS else (
            verdict.failure_reason or None)

    def _should_use_llm(self, state) -> bool:
        return has_observe_role(state)

    @staticmethod
    def _subtask_handles(state, ctx) -> list[dict]:
        tm = getattr(ctx, "task_manager", None)
        if tm is None or not hasattr(tm, "children_of"):
            return []
        out = []
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

    async def _is_short_segment(
        self,
        state: "LoopState", ctx: "LoopContext", watermark: "datetime | None" = None,
    ) -> bool:
        """短段免折门：**当前段**（最后一条 active USER_PROMPT 之后）满足以下任一即为短段：

        - 段内 LLM 回复（role=assistant 回合）≤ 1 条——**不看 token**：一条回复折成摘要
          是净亏（recap 常比原文还长，且原文对下一轮信息更全），折它只是白花一次 LLM；
        - 段内 raw token ≤ short_segment_token_threshold。

        「短 → 原文成胶囊」决策（finalize._is_short_leaf）在段级的判定，
        `ObserveStep._execute_background`（plain_text/interrupt 边界）与
        `ObserveStep._fold_retry_segment`（retry 段折）共用。配置缺失（手构 state /
        单测）→ False = 门关闭，照常折叠。

        段作用域（2026-07-21）：只数末条 UP 之后的 raw，与折叠的 since_last=USER_PROMPT
        对齐——免折残留的前段 raw 不计入，否则「前段累积 + 当前段极短」会被误判为可折，
        而折叠又只折当前段，产出比原文还长的摘要。
        """
        threshold = getattr(state.agent.loop_config, "short_segment_token_threshold", 0)
        if threshold <= 0:
            return False
        from ctx_weft.protocols import MemoryKind, MemoryScope

        # v2 P3a：TASK 视图（对话 + audit，无 SUMMARY——旧类型清单不含段摘要）升序；
        # 段界 = 末条 role=user 回合，其后即当前段 raw。
        view = await ctx.memory.load_view(
            state.scope, MemoryScope.TASK, ctx.provider_ctx,
            kinds=[MemoryKind.CONVERSATION_TURN, MemoryKind.TOOL_AUDIT],
        )
        # 段界水位线（2026-09-22，同 `segment_fold`）：只认这段后台 observe 开跑时就已存在
        # 的记录。不设限的话，人中途说的话会成为新段界，把「当前段」判成空段而误判为短段。
        if watermark is not None:
            view = [r for r in view if r.timestamp <= watermark]
        seg_records: list = []
        for r in view:
            if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "user":
                seg_records = []  # 新段界：清空重计
                continue
            seg_records.append(r)
        # 单回复段免折（2026-08-19）：token 再多也不折——见 docstring。
        n_llm = sum(1 for r in seg_records
                    if r.kind is MemoryKind.CONVERSATION_TURN and r.role == "assistant")
        if n_llm <= 1:
            return True
        seg_text = " ".join(
            r.content if isinstance(r.content, str) else content_to_text(r.content)
            for r in seg_records
        )
        # 保留「join 后数一次」的文本口径（逐条估算会引入 4×N 的 framing 漂移），
        # 图片另行求和补上——不补则图片密集段被误判短段免折、该段 raw 永久保留。
        seg_images = sum(image_tokens(r.content) for r in seg_records)
        return ctx.llm.tokenizer.count(seg_text) + seg_images <= threshold

    async def _fold_retry_segment(
        self, state: LoopState, ctx: LoopContext, verdict: Verdict, events: list[Any]
    ) -> None:
        """retry 前台同步段折：本轮 attempt raw → 一条 TASK_COMPACT_SUMMARY（复用 act_recap），
        supersede 本轮全部 raw（keep_last=0），保 USER_PROMPT 锚 + 既往段摘要（累积）。
        protect TASK_COMPACT_SUMMARY → 多轮 retry 段摘要累积（不替换），由 L3 按 collapse_keep_last
        坍缩控界。无 keep_last 门、无额外 LLM。

        仅当 verdict.task_outcome=="continue" 才折（三来源：max_turns/context_limit/observer 判未完）；
        其余 outcome 不折，no-op。

        act_recap 来自本轮 observation；空则不折、段保 raw（不写占位摘要）。
        短段免折（ObserveStep._is_short_segment，与段边界折叠同门）：当前段（末条 UP
        之后）active raw 低于 short_segment_token_threshold 时不折——recap 常比短原文更长，
        raw 原样留给下个 attempt 反而信息更全；attempt 之间无新 UP，raw 在段内跨 attempt
        累积，超阈值后下一次 retry 一并折叠。折叠带 since_last=USER_PROMPT（段作用域，
        2026-07-21）：交互任务中若有免折残留的前段 raw，不被跨段合折。
        """

        if verdict.task_outcome != VERDICT_CONTINUE:
            return
        summary = (verdict.act_recap or "").strip()
        if not summary:
            logger.warning(
                "_fold_retry_segment: empty act_recap for task=%s; skip fold, segment kept raw",
                state.task.id,
            )
            return
        if await self._is_short_segment(state, ctx):
            logger.info(
                "_fold_retry_segment: short segment kept raw (task=%s); skip fold",
                state.task.id,
            )
            return
        # 与后台观察共用段作用域原子折叠，保护 user 回合与既有摘要。
        from ctx_weft.core.loop.task_memory_fold import segment_fold
        result = await segment_fold(
            ctx.memory, state.scope, MemoryScope.TASK, summary, ctx.provider_ctx,
        )
        events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
            "events_before": result.events_before,
            "events_after": result.events_after,
            "summary_event_id": result.summary_event_id,
            "summary_length": len(summary),
            "layer": "task",
            "trigger": "observe_retry",
        }))

    async def _execute_background(self, state, ctx, boundary, judging):
        watermark = _execution(ctx).started_at
        await ctx.event_bus.emit(make_event(
            state, EventType.TASK_RECAP_STARTED,
            {"task_id": state.task.id, "boundary": boundary, "agent_id": state.agent.id}))
        try:
            async with StepDriver.for_context(ctx).executions.task_guard(state.task.id, "observe"):
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
                        return self._outcome(ObservationResult())
                    # 短段免折（_is_short_segment）：当前段（末条 UP 之后）active raw 低于阈值时
                    # 跳过折叠——花一次后台 LLM 调用换一段常比原文还长的摘要不划算。跳过 = 该段
                    # **永久**保 raw（与观察失败的降级同语义）：后续折叠带 since_last=USER_PROMPT
                    # 只折各自的当前段，免折残留不会被跨段合折。
                    short_segment = await self._is_short_segment(state, ctx, watermark)
                    if short_segment and not judging:
                        logger.info(
                            "short segment kept raw (task=%s boundary=%s); skip fold",
                            state.task.id, boundary,
                        )
                        return self._outcome(ObservationResult())
                try:
                    observation = await self._observe(state, ctx, [], judging, boundary)
                    meta = observation.metadata()
                    act_recap = observation.act_recap.strip()
                    task_summary = observation.task_summary
                    if not act_recap:
                        # 无任何可用报告：与异常路径同语义——段保 raw，不写占位摘要、不动 finish 对。
                        if boundary in CLOSE_BOUNDARIES:
                            pop_close_synth(state.task.id)  # 弹掉登记防泄漏；finalize 占位 finish 对保持原样
                        logger.warning(
                            "background observe produced no usable report (task=%s boundary=%s); "
                            "segment kept raw", state.task.id, boundary,
                        )
                        return self._outcome(ObservationResult())
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
                        from ctx_weft.core.loop.task_memory_fold import segment_fold
                        await segment_fold(
                            ctx.memory, state.scope, MemoryScope.TASK, act_recap,
                            ctx.provider_ctx, watermark,
                        )
                    return self._outcome(observation)
                except Exception:
                    # close 边界防泄漏：finalize 可能已 register_close_synth，本次失败后永远无人
                    # 消费（task_id 唯一 + close 单入口），弹掉——与「无可用报告」分支对称。
                    # 已知残余窗口（接受，不另引状态同步）：bg 比 finalize 先死时登记发生在 pop
                    # 之后，仍漏一条；对称地，bg 先写 _close_report 而 finalize 异常中止也漏。
                    # 两者触发概率与单次代价（几百字节/次）都低一个量级。
                    if boundary in CLOSE_BOUNDARIES:
                        pop_close_synth(state.task.id)
                    logger.exception("background observe failed (ignored); segment kept raw")
                    return self._outcome(ObservationResult())

        finally:
            await ctx.event_bus.emit(make_event(
                state, EventType.TASK_RECAP_DONE, {"task_id": state.task.id}))



_PARK_CLOSED_BY_VERDICT = runtime_prompt('park_closed_by_verdict')

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

    The independent observation run carries the task scope and final-output anchors.

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
