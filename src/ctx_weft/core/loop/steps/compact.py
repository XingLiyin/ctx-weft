"""CompactStep：长对话压缩，由 PrepareStep 内联直调（不再以 task 形式调度）。

  - 作用域 = 当前 state.scope（当前 task + agent）。
  - 计算可折叠层（agent 派发日志 / task 对话），任一层 active 条数 > keep_last 才折。
  - 复用 act 装配内容 + 末尾压缩指令（composer purpose="compact"），一次 summary。
  - 对每个超额层 apply_compact 同一份 summary。
"""

from __future__ import annotations

import logging
from typing import Any

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
from ctx_weft.protocols import LLMRequest, MemoryEventType, MemoryLayer

logger = logging.getLogger(__name__)

# 每层「可折叠」的对话类型；某层 active 条数 > keep_last 才值得 compact（空层守卫）。
_AGENT_COMPACT_TYPES = [
    MemoryEventType.TASK_DISPATCH,
    MemoryEventType.TASK_DISPATCH_RESULT,
    MemoryEventType.AGENT_CONVERSATION_TURN,  # root self-experience records are agent-layer foldable content
]
TASK_COMPACT_TYPES = [
    MemoryEventType.USER_PROMPT,
    MemoryEventType.LLM_RESPONSE,
    MemoryEventType.TOOL_RESULT,
]


async def summarize_for_compact(state: LoopState, ctx: LoopContext) -> str:
    """装配 purpose="compact" 上下文 + 一次 LLM 摘要，返回摘要文本。

    LLM 摘要是 compact 的硬依赖（compact + observe 回退档共用本函数）：瞬时故障由
    stream_llm_resilient 自愈，自愈耗尽抛 LLMOutageError → 走 INTERRUPTED。**不再**在
    LLM 失败时静默退化为纯截断（旧的 except Exception 兜底已移除）。
    """
    agent = state.agent
    request = ContextRequest(
        purpose="compact",
        scope=state.scope,
        task=state.task,
        agent=agent,
        session=state.session,
        template=state.extra.get("template"),
        bound_capabilities=state.extra.get("bound_capabilities", []),
        actor_transcript=state.transcript,
    )
    compact_prompt = await ctx.assembler.assemble(request)

    llm_request = LLMRequest(
        model=agent.runtime.get("llm_model", "mock"),
        system=compact_prompt.system,
        messages=compact_prompt.messages,
        tools=[],
    )
    summary_text = ""
    async for chunk in stream_llm_resilient(ctx, state, llm_request):
        if chunk.kind == "token":
            summary_text += chunk.text
    return summary_text


class CompactStep(Step):
    """Standalone compaction over state.scope. Invoked inline by PrepareStep."""

    name = "compact"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        agent = state.agent
        keep_last = agent.loop_config.compact_keep_last
        layers = await self._foldable_layers(state, ctx, keep_last)
        if not layers:
            return StepOutcome(next_step=None, events=[])

        events: list[Any] = [make_event(state, EventType.MEMORY_COMPACT_STARTED, payload={
            "task_id": state.task.id,
            "agent_id": agent.id,
            "keep_last": keep_last,
            "layers": list(layers),
        })]

        # 复用可重用的「装配 + 一次摘要」逻辑（observe 回退档也用它）。
        summary_text = await summarize_for_compact(state, ctx)

        for layer_name in layers:
            layer = MemoryLayer(layer_name)
            result = await ctx.memory.apply_compact(
                scope=state.scope,
                summary=summary_text or "[Context compacted]",
                keep_last=keep_last,
                ctx=ctx.provider_ctx,
                layer=layer,
            )
            events.append(make_event(state, EventType.MEMORY_COMPACTED, payload={
                "events_before": result.events_before,
                "events_after": result.events_after,
                "summary_event_id": result.summary_event_id,
                "summary_length": len(summary_text),
                "used_llm": bool(summary_text),
                "layer": layer_name,
            }))

        logger.info("CompactStep: agent=%s task=%s folded layers=%s summary_len=%d",
                    agent.id, state.task.id, layers, len(summary_text))
        return StepOutcome(next_step=None, events=events)

    async def _foldable_layers(
        self, state: LoopState, ctx: LoopContext, keep_last: int
    ) -> list[str]:
        """有足够内容可折叠（active 条数 > keep_last）的层。"""
        layers: list[str] = []
        for layer, types in (("agent", _AGENT_COMPACT_TYPES), ("task", TASK_COMPACT_TYPES)):
            try:
                n = await ctx.memory.count_recent(scope=state.scope, types=types, ctx=ctx.provider_ctx)
            except Exception:
                n = 0
            if n > keep_last:
                layers.append(layer)
        return layers
