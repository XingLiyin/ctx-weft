"""ActStep：多 turn LLM 子循环 + capability 调用。

工具调用通过 CapabilityGateway 统一执行（授权 + 审计 + 内存），
ActStep 只处理 LLM 流 + turn 循环逻辑。
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ctx_weft.protocols import LLMMessage, LLMRequest, LLMUsage, ToolCall
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.park import HitlPark
from ctx_weft.core.orchestrator.control_capability import (
    ASK_USER_NAME,
    FINISH_TASK_NAME,
    WAIT_FOR_USER_CAPABILITY_ID,
)
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import now_utc
from ctx_weft.protocols import MemoryEvent, MemoryEventType

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

        current_messages = list(prompt.messages)
        # 临时拼上 task title/description + 后继任务 + 完成方式（仅发送，不入 memory）。
        current_messages = _inject_act_guidance(current_messages, state, ctx)

        for turn_num in range(1, max_turns + 1):
            if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                ctx.cancel_token.raise_if_cancelled()

            await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_STARTED, payload={
                "turn": turn_num,
            }))

            req_id = f"req_{agent.id}_{state.sequence_counter}"
            await ctx.event_bus.emit(make_event(state, EventType.LLM_REQUEST_STARTED, payload={
                "request_id": req_id,
                "model": agent.runtime.get("llm_model", "mock"),
                "turn": turn_num,
            }))

            llm_request = LLMRequest(
                model=agent.runtime.get("llm_model", "mock"),
                system=prompt.system,
                messages=list(current_messages),
                tools=prompt.tools,
            )

            await ctx.event_bus.emit(make_event(state, EventType.LLM_PROMPT_SENT, payload={
                "request_id": req_id,
                "turn": turn_num,
                "system": prompt.system,
                "messages": [
                    {"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
                    for m in current_messages
                ],
                "tool_names": [t.name for t in prompt.tools],
            }))

            accumulated_text = ""
            accumulated_reasoning = ""
            tool_calls: list[ToolCall] = []
            usage = LLMUsage()

            async for chunk in ctx.llm.complete(llm_request, stream=True):
                if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                    ctx.cancel_token.raise_if_cancelled()
                if chunk.kind == "token":
                    accumulated_text += chunk.text
                    await ctx.event_bus.emit(make_event(
                        state, EventType.LLM_TOKEN_STREAMED,
                        payload={"request_id": req_id, "delta": chunk.text},
                    ))
                elif chunk.kind == "reasoning":
                    accumulated_reasoning += chunk.text
                    await ctx.event_bus.emit(make_event(
                        state, EventType.LLM_REASONING_STREAMED,
                        payload={"request_id": req_id, "delta": chunk.text},
                    ))
                elif chunk.kind == "tool_call" and chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
                elif chunk.kind == "usage" and chunk.usage is not None:
                    usage = chunk.usage

            await ctx.event_bus.emit(make_event(
                state, EventType.LLM_RESPONSE_FINISHED,
                payload={
                    "request_id": req_id,
                    "content": accumulated_text,
                    "reasoning": accumulated_reasoning,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in tool_calls
                    ],
                    "usage": dataclasses.asdict(usage),
                    "finish_reason": "tool_use" if tool_calls else "stop",
                    "turn": turn_num,
                },
            ))

            # ── 更新 LoopGuard + session.token_used ──────────────────────────
            if usage.prompt_tokens > 0:
                agent.loop_guard.context_tokens = usage.prompt_tokens
                try:
                    agent.loop_guard.context_message_count = await ctx.memory.count_recent(
                        scope=state.scope,
                        types=[
                            MemoryEventType.USER_PROMPT,
                            MemoryEventType.LLM_RESPONSE,
                            MemoryEventType.OBSERVER_SUMMARY,
                        ],
                        ctx=ctx.provider_ctx,
                    )
                except Exception:
                    pass

            # 累加 session.token_used（供 token_budget 检查使用）
            state.session.token_used += usage.prompt_tokens + usage.completion_tokens

            # ── context_limit_hit：80% 阈值停止当前 ActStep ─────────────────
            # 对齐 miniAgents actor.py context_limit_hit 逻辑
            context_limit = agent.loop_guard.context_limit
            context_limit_hit = (
                context_limit > 0
                and usage.prompt_tokens > 0
                and usage.prompt_tokens >= int(context_limit * 0.8)
            )

            asst_tool_dicts = [
                {"id": tc.id, "name": tc.name, "input": tc.arguments}
                for tc in tool_calls
            ]
            # 持久化 tool_calls 供无损重建；派发调用(submit_*)落 agent 层 TASK_DISPATCH，
            # 不计入 task 层 LLM_RESPONSE，避免与 agent_experience 重复（spec/06 §4）。
            # 排除 dispatch（落 agent 层 TASK_DISPATCH）与 silent（结果不入 task 对话，如
            # finish_task/report_task_outcome）：二者的 tool_call 若落 task 层 LLM_RESPONSE
            # 会因无配对 TOOL_RESULT 而成悬挂调用，破坏无损重建。
            from ctx_weft.core.loop.capability_gateway import DISPATCH_TOOLS, SILENT_TOOLS
            _excluded = DISPATCH_TOOLS | SILENT_TOOLS
            non_dispatch_tool_dicts = [d for d in asst_tool_dicts if d["name"] not in _excluded]
            await ctx.memory.ingest(
                MemoryEvent(
                    type=MemoryEventType.LLM_RESPONSE,
                    scope=state.scope,
                    content=accumulated_text,
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

            # Append assistant message (with tool_call refs if any)
            current_messages.append(LLMMessage(
                role="assistant",
                content=accumulated_text,
                tool_calls=asst_tool_dicts,
                reasoning_content=accumulated_reasoning or None,
            ))

            turn_record = TurnRecord(
                turn=turn_num,
                messages_sent=list(current_messages),
                assistant_text=accumulated_text,
                tool_calls=tool_calls,
                usage=usage,
            )

            if context_limit_hit:
                transcript.append(turn_record)
                await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                    "turn": turn_num, "reason": "context_limit",
                }))
                exit_reason = "context_limit"
                logger.warning(
                    "ActStep context_limit_hit: prompt_tokens=%d >= %d * 0.8 for agent %s",
                    usage.prompt_tokens, context_limit, agent.id,
                )
                break

            if not tool_calls:
                transcript.append(turn_record)
                # interactive 任务：纯文本(无 tool call) = 让位给用户 → HITL input 冷 park。
                # 用户回复经 runtime 冷 resume 作 USER_PROMPT 注入后重入 act。
                if (
                    isinstance(state.task.settings, NormalTaskSettings)
                    and state.task.interaction_mode == "interactive"
                    and ctx.hitl_manager is not None
                ):
                    await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                        "turn": turn_num, "reason": "await_user",
                    }))
                    rid = await ctx.hitl_manager.request_parked(
                        kind="input",
                        session_id=state.session.id,
                        task_id=state.task.id,
                        agent_id=state.agent.id,
                        capability_id=WAIT_FOR_USER_CAPABILITY_ID,
                        # 纯文本输出已作为 assistant message 流式呈现,无需再塞进 question 重复展示
                        question="",
                    )
                    # 纯文本暂停 = 软待命(允许但不强制回复) → PAUSED,区别于 ask_user 的 PAUSED_HITL。
                    state.session.status = "PAUSED"
                    state.task.status = "SUSPENDED"
                    raise HitlPark(request_id=rid)
                # auto / 非普通任务 / 无 hitl_manager：旧行为——纯文本即任务产出，路由 observe。
                await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                    "turn": turn_num, "reason": "stop",
                }))
                break

            # ── Tool calls → CapabilityGateway ───────────────────────────────
            tool_results: list[Any] = []

            for tc in tool_calls:
                if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                    ctx.cancel_token.raise_if_cancelled()
                try:
                    result = await _invoke_tool(tc, state, ctx)
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

            # 退出信号延迟到本轮所有 tool 执行完毕后再处理（见循环末尾的 actor_done 检查），
            # 保证同一批次的 delegate_task / delegate_plan 全部投入缓冲、tool_call ↔ result 一一对应。

            # Append tool results to message history
            for tr in tool_results:
                current_messages.append(LLMMessage(
                    role="tool",
                    content=tr["result"],
                    tool_call_id=tr["tool_call_id"],
                ))

            turn_record.tool_results = tool_results
            transcript.append(turn_record)
            await ctx.event_bus.emit(make_event(state, EventType.ACT_TURN_COMPLETED, payload={
                "turn": turn_num, "reason": "tool_calls_processed",
            }))

            if state.task.actor_done:
                exit_reason = "actor_done"
                break

        else:
            exit_reason = "max_turns"
            logger.warning("ActStep hit max_turns=%d for agent %s", max_turns, agent.id)
            await ctx.event_bus.emit(make_event(state, EventType.MAX_TURNS_REACHED, payload={
                "max_turns": max_turns,
            }))

        # task.status == "SUSPENDED" 表示本 task 在等子任务，路由到 SuspendStep
        next_step = "suspend" if state.task.status == "SUSPENDED" else "observe"

        # ── task.outputs：把 actor 最终文本写回（对齐 miniAgents _run_actor）──
        # exit_reason == "normal" 时，最后一轮的文本即任务输出
        if exit_reason == "normal" and transcript:
            last_text = transcript[-1].assistant_text
            if last_text:
                state.task.outputs = last_text

        return StepOutcome(
            next_step=next_step,
            state_patch={
                "transcript": transcript,
                "act_exit_reason": exit_reason,
            },
        )


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


# ── 临时 task guidance 注入（仅发送，不入 memory）──────────────────────────────────


def _plan_successors(state: LoopState, ctx: LoopContext) -> list:
    """本 task 的 plan 后继（tracking_task_ids 含本 task id 的任务），按创建时间升序。"""
    if ctx.task_manager is None:
        return []
    tid = state.task.id
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    succ = [t for t in ctx.task_manager.all_tasks() if tid in (t.tracking_task_ids or [])]
    succ.sort(key=lambda t: t.created_at or epoch)
    return succ


def _build_act_guidance(state: LoopState, ctx: LoopContext) -> str:
    """构造拼到最后一条 user message 的临时 guidance（title/description + 后继 + 完成方式）。

    - title/description 都为空时，整个「## Your current task」块不出现。
    - 没有后继任务时，后继段整段不出现（不再提示 "No tasks are queued"）。
    """
    task = state.task
    parts: list[str] = ["---"]

    if task.title or task.description:
        parts.append("## Your current task")
        if task.title:
            parts.append(f"Title: {task.title}")
        if task.description:
            parts.append(f"Description: {task.description}")
        parts.append("")
    else:
        up = getattr(task, "user_prompt", None)
        if up:
            up_text = up if isinstance(up, str) else str(up)
            parts.append("## Your current task")
            parts.append("This task was started by the user's request:")
            parts.append(up_text)
            parts.append("")
            parts.append(
                f"When you have completed it, proactively call the `{FINISH_TASK_NAME}` "
                "tool with the final result to finish it."
            )
            parts.append("")

    succ = _plan_successors(state, ctx)
    if succ:
        parts.append("## Tasks queued after this one (do NOT do them yourself):")
        for i, t in enumerate(succ, 1):
            d = f" — {t.description}" if t.description else ""
            parts.append(f"{i}. {t.title}{d}")
        parts.append("")

    if task.interaction_mode == "interactive":
        parts.append(
            f"When this task is complete, call the `{FINISH_TASK_NAME}` tool with the final "
            "result to finish it. If you instead reply in plain text, execution pauses and "
            "waits for the user's next message."
        )
    elif succ:
        parts.append(
            f"When this task is complete, call the `{FINISH_TASK_NAME}` tool with the final "
            "result to finish it. Do not start the queued tasks yourself."
        )
    else:
        parts.append(
            f"When this task is complete, call the `{FINISH_TASK_NAME}` tool with the final "
            "result to finish it."
        )
    # 始终提示：需要用户输入/决策/澄清时主动调 ask_user（各完成方式下都加）。
    parts.append(
        f"Whenever you need information, a decision, or a clarification that only the user "
        f"can provide, call the `{ASK_USER_NAME}` tool to ask them — prefer asking over guessing."
    )
    return "\n".join(parts)


def _inject_act_guidance(
    messages: list[LLMMessage], state: LoopState, ctx: LoopContext
) -> list[LLMMessage]:
    """把临时 guidance 拼到最后一条 user message 内容尾部，返回新列表。

    仅普通任务（NormalTaskSettings）注入；compact/recognize_intent 等跳过。仅影响发送给 LLM 的
    messages，不写 memory。无 user message 时追加一条。
    """
    if not isinstance(state.task.settings, NormalTaskSettings):
        return messages
    guidance = _build_act_guidance(state, ctx)
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].role == "user":
            base = out[i].content if isinstance(out[i].content, str) else str(out[i].content)
            out[i] = dataclasses.replace(out[i], content=f"{base}\n\n{guidance}")
            return out
    out.append(LLMMessage(role="user", content=guidance))
    return out
