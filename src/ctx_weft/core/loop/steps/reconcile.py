"""ReconcileStep：resume 后、任何 LLM turn 之前，补完 dangling tool_call（spec/07 §6）。

被 park（HITL）或崩溃中途打断时，最近一个 assistant turn 的部分 tool_call 没有对应
TOOL_RESULT。直接把含 dangling 的消息序列喂给 LLM 会非法报错。本步对账：
  - 已有 TOOL_RESULT 的 tool_call → 跳过（复用持久结果，不重跑）
  - dangling 的 → gateway.invoke 执行（HITL 决定缓存按 tool_call_id 短路门控），
    由 gateway 写唯一 TOOL_RESULT
→ next_step="act"：assembler 重建出完整 turn，LLM 续跑。
"""

from __future__ import annotations

import logging

from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome
from ctx_weft.core.loop.steps._capabilities import resolve_and_bind
from ctx_weft.protocols import MemoryEventType

logger = logging.getLogger(__name__)


class ReconcileStep(Step):
    name = "reconcile"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        # → "prepare"（非 "act"）：填完 dangling 后须由 PrepareStep 用补齐的 memory 重装 assembled_prompt
        # 并绑定 capability,再 act 调 LLM。直接 "act" 会因缺 assembled_prompt 报错（spec/07 §6）。
        dangling = await _dangling_tool_calls(ctx.memory, state.scope, ctx.provider_ctx)
        if not dangling:
            logger.info("ReconcileStep: no dangling tool_calls for task %s", state.task.id)
            return StepOutcome(next_step="prepare")

        gateway = ctx.capability_gateway
        if gateway is None:
            raise RuntimeError("ReconcileStep requires a CapabilityGateway")

        # reconcile 跑在 prepare 之前 → 须自行绑定 capability,否则 gateway.invoke 命中空 cache
        # 找不到 dangling 工具（spec/07 §6 端到端缺陷修复）。
        await resolve_and_bind(state, ctx)

        for tc in dangling:
            if ctx.cancel_token is not None and ctx.cancel_token.is_cancelled:
                ctx.cancel_token.raise_if_cancelled()
            logger.info("ReconcileStep: re-invoking dangling tool_call %s (%s)", tc["id"], tc["name"])
            await gateway.invoke(
                tool_name=tc["name"],
                arguments=tc.get("input", {}) or {},
                state=state,
                ctx=ctx,
                tool_call_id=tc["id"],
            )

        return StepOutcome(next_step="prepare")


async def _dangling_tool_calls(memory, scope, provider_ctx) -> list[dict]:
    """最近一个 assistant turn 里，无对应 TOOL_RESULT 的 tool_call（按原顺序）。"""
    responses = await memory.recall_recent(
        scope=scope, types=[MemoryEventType.LLM_RESPONSE], limit=1, ctx=provider_ctx,
    )
    if not responses:
        return []
    tool_calls = responses[0].metadata.get("tool_calls") or []
    if not tool_calls:
        return []
    results = await memory.recall_recent(
        scope=scope, types=[MemoryEventType.TOOL_RESULT], limit=200, ctx=provider_ctx,
    )
    done_ids = {r.metadata.get("tool_call_id") for r in results}
    return [tc for tc in tool_calls if tc.get("id") not in done_ids]
