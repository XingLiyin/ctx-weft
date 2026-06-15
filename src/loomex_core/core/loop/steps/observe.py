"""ObserveStep：评估 actor transcript，产出 verdict。

miniAgents 对齐版：
- 有 ROLE 配置 + 非 root-normal 场景 → LLM 多轮 ReAct（用 report_task_outcome 工具）
- 其他情况 → 规则降级（从 transcript + exit_reason 推断）

降级条件（按 task 排除）：
  1. template 未配置 identity["observe"]（assigned agent 无 ROLE）
  2. root task（task.parent_task_id is None）——顶层任务无 parent 可上报，不需要 LLM observer
  3. LLM 调用失败
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loomex_core.core.assembler import ContextRequest
from loomex_core.core.events import EventType
from loomex_core.protocols import LLMMessage, LLMRequest, LLMUsage
from loomex_core.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from loomex_core.core.utils import now_utc

if TYPE_CHECKING:
    from loomex_core.core.state.models import Task

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    """Observer 输出（三态）。"""
    task_outcome: str   # "retry" | "success" | "fail"
    summary: str        # 本轮工作的简短总结（进入 memory + 父 agent 读取）


class ObserveStep(Step):
    name = "observe"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        events: list[Any] = []

        if self._should_use_llm(state):
            try:
                verdict = await self._llm_observe(state, ctx, events)
            except Exception as exc:
                logger.warning("ObserveStep LLM call failed, falling back to rules: %s", exc)
                verdict = self._rule_observe(state)
        else:
            verdict = self._rule_observe(state)

        # 机械退出（max_turns/context_limit）：任务未完成、只是耗尽 turn/context，非终态——
        # 强制 retry 重排（覆盖 success/fail），摘要作为执行记录带入下一轮（受 max_retries 兜底）。
        if state.act_exit_reason in ("max_turns", "context_limit") and verdict.task_outcome != "retry":
            verdict = Verdict(task_outcome="retry", summary=verdict.summary)
            self._apply_assessment(state.task, verdict)

        events.append(make_event(
            state, EventType.OBSERVE_COMPLETED,
            payload={
                "task_id": state.task.id,
                "outcome": verdict.task_outcome,
                "summary_length": len(verdict.summary),
                "used_llm": self._should_use_llm(state),
            },
        ))

        return StepOutcome(
            next_step="finalize",
            state_patch={"verdict": verdict},
            events=events,
        )

    # ── LLM 路径 ──────────────────────────────────────────────────────────────

    async def _llm_observe(
        self,
        state: LoopState,
        ctx: LoopContext,
        events: list[Any],
    ) -> Verdict:
        """LLM ReAct 循环：装配 observe prompt → 最多 max_turns_per_observe 轮 LLM 调用。

        每轮：若 LLM 调用 report_task_outcome → 立即返回 verdict；
              若 LLM 返回纯文本 → 继续下一轮（最多 max_turns_per_observe 次）。
        对齐 miniAgents Observer._llm_observe 多轮循环。
        """
        agent = state.agent
        max_rounds = agent.loop_config.max_turns_per_observe

        cache = ctx.capability_cache
        bound_caps = (
            cache.get(agent.id)
            if cache is not None and cache.has_agent(agent.id)
            else []
        )
        request = ContextRequest(
            purpose="observe",
            scope=state.scope,
            task=state.task,
            agent=agent,
            session=state.session,
            template=state.extra.get("template"),
            bound_capabilities=bound_caps,
            actor_transcript=state.transcript,
        )
        prompt = await ctx.assembler.assemble(request)
        current_messages = list(prompt.messages)
        last_text = ""

        for round_num in range(max_rounds):
            req_id = f"obs_{agent.id}_{state.sequence_counter}_r{round_num}"
            await ctx.event_bus.emit(make_event(state, EventType.LLM_REQUEST_STARTED, payload={
                "request_id": req_id,
                "model": agent.runtime.get("llm_model", "mock"),
                "round": round_num,
            }))

            llm_request = LLMRequest(
                model=agent.runtime.get("llm_model", "mock"),
                system=prompt.system,
                messages=list(current_messages),
                tools=prompt.tools,
            )

            await ctx.event_bus.emit(make_event(state, EventType.LLM_PROMPT_SENT, payload={
                "request_id": req_id,
                "round": round_num,
                "system": prompt.system,
                "messages": [
                    {"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
                    for m in current_messages
                ],
                "tool_names": [t.name for t in prompt.tools],
            }))

            accumulated_text = ""
            tool_calls = []
            usage = LLMUsage()

            async for chunk in ctx.llm.complete(llm_request, stream=True):
                if chunk.kind == "token":
                    accumulated_text += chunk.text
                    await ctx.event_bus.emit(make_event(
                        state, EventType.LLM_TOKEN_STREAMED,
                        payload={"request_id": req_id, "delta": chunk.text},
                    ))
                elif chunk.kind == "tool_call" and chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
                elif chunk.kind == "usage" and chunk.usage is not None:
                    usage = chunk.usage

            if accumulated_text:
                last_text = accumulated_text

            # 更新 loop_guard（对齐 miniAgents _run_observer：取 actor/observer 的最大值）
            if usage.prompt_tokens > 0:
                agent.loop_guard.context_tokens = max(
                    agent.loop_guard.context_tokens, usage.prompt_tokens
                )
            # 同步累加 session.token_used
            state.session.token_used += usage.prompt_tokens + usage.completion_tokens

            await ctx.event_bus.emit(make_event(
                state, EventType.LLM_RESPONSE_FINISHED,
                payload={
                    "request_id": req_id,
                    "content": accumulated_text,
                    "tool_calls": [{"name": tc.name} for tc in tool_calls],
                    "usage": dataclasses.asdict(usage),
                    "round": round_num,
                },
            ))

            if not tool_calls:
                # LLM returned only text — no more rounds needed
                break

            current_messages.append(LLMMessage(
                role="assistant",
                content=accumulated_text,
                tool_calls=[{"id": tc.id, "name": tc.name, "input": tc.arguments} for tc in tool_calls],
            ))

            terminate_result = None
            for tc in tool_calls:
                if ctx.capability_gateway is not None:
                    status_before = state.task.status
                    result = await ctx.capability_gateway.invoke(
                        tool_name=tc.name,
                        arguments=tc.arguments,
                        state=state,
                        ctx=ctx,
                        tool_call_id=tc.id,
                    )
                    content = result.content
                    if state.task.status != status_before:
                        terminate_result = result
                else:
                    logger.warning(
                        "ObserveStep: no CapabilityGateway for tool '%s'", tc.name
                    )
                    content = f"[Error: CapabilityGateway not configured, tool '{tc.name}' skipped]"
                current_messages.append(LLMMessage(
                    role="tool",
                    content=content,
                    tool_call_id=tc.id,
                ))

            if terminate_result is not None:
                # report_task_outcome 已把三态裁决写入 task.observer_outcome
                return Verdict(
                    task_outcome=state.task.observer_outcome or "success",
                    summary=state.task.process_report or last_text[:500],
                )

        logger.warning("ObserveStep: LLM did not call report_task_outcome in %d rounds, falling back to rules", max_rounds)
        return self._rule_observe(state)

    # ── 规则降级 ───────────────────────────────────────────────────────────────

    def _rule_observe(self, state: LoopState) -> Verdict:
        """不调 LLM，从 transcript + exit_reason 直接推断结果（miniAgents _rule_observe 对齐版）。

        直接对 state.task 执行与 report_task_outcome 相同的 status 变更。
        """
        transcript = state.transcript
        exit_reason = state.act_exit_reason

        if not transcript:
            verdict = Verdict(task_outcome="fail", summary="[No actor execution recorded]")
            self._apply_assessment(state.task, verdict)
            return verdict

        tools_used = list({tc.name for turn in transcript for tc in turn.tool_calls})

        lines: list[str] = [f"Ran {len(transcript)} conversation round(s)."]
        if tools_used:
            lines.append(f"Tools used: {', '.join(tools_used)}.")
        else:
            lines.append("No tools were called.")

        if exit_reason in ("max_turns", "context_limit"):
            # 机械退出 → retry（重排再跑，非终态，受 max_retries 兜底）；摘要即执行记录
            lines.append("Actor reached its turn/context limit; will retry.")
            outcome = "retry"
        else:
            # "normal"（LLM 自然停止）或 "actor_done"（控制工具退出）均视为成功
            lines.append("Task completed.")
            outcome = "success"

        verdict = Verdict(task_outcome=outcome, summary=" ".join(lines))
        self._apply_assessment(state.task, verdict)
        return verdict

    @staticmethod
    def _apply_assessment(task: Task, verdict: Verdict) -> None:
        """将 verdict 三态结果写入 task，对齐 report_task_outcome。"""
        outcome = verdict.task_outcome
        task.observer_outcome = outcome
        if outcome == "success":
            task.status = "FINISHED"
        elif outcome == "fail":
            task.status = "FAILED"
        else:  # retry
            task.status = "PENDING"
        task.actor_done = True

    # ── 条件判断 ──────────────────────────────────────────────────────────────

    def _should_use_llm(self, state: LoopState) -> bool:
        """规则降级条件（按 task 排除）：root task 或 assigned agent 无 observe ROLE → 跳过 LLM。"""
        # assigned agent 没有 observe ROLE → 规则降级
        template = state.extra.get("template")
        if template is None or template.identity.get("observe") is None:
            return False
        # root task（无 parent）→ 规则降级；委派出的子任务才需要 LLM observer 给出可上报的裁决
        if state.task.parent_task_id is None:
            return False

        return True
