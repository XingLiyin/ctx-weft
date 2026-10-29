"""PrepareStep（资源装配）：能力解析/绑定 + token 估算 + compact 触发 + prompt 装配。

token 估算对齐 miniAgents Reasoner._fetch_base：
  - 有真实基线（loop_guard.context_tokens > 0）→ 增量估算：基线 + 新增消息估算
  - 无基线 → 用装配后的 prompt.token_count（全量估算）

compact 触发（纯预算，spec 2026-07-01 §3.6）：
  - token_estimate / context_limit >= compact_token_ratio
  命中后调 escalating_compact 升级式压缩，再完整重装配一次 prompt（Q4=c 校正）。

本步还是**上下文恢复**的落点：ActStep 越过 `context_limit_stop_ratio` 时不再直接退出吃一次
retry，而是路由回本步（见 `act._can_recover_context`）——本步照常估算 → 压缩 → 重装配，act
随后在压缩后的 prompt 上原地续跑。act 手里的真实 `context_tokens` 刚被写成越线值，所以这条
路径上 compact 的门必然开。压缩若一无所获，本步把 `COMPACT_NOOP_KEY` 置起，act 据此放弃恢复。
"""

from __future__ import annotations

from typing import Callable

from ctx_weft.protocols import load_records
from ctx_weft.core.assembler import ContextRequest
from ctx_weft.core.loop.commit import commit_round, defer_until_commit
from ctx_weft.core.loop.driver import (
    COMPACT_NOOP_KEY,
    LoopContext,
    LoopState,
    Step,
    StepOutcome,
    make_event,
)
from ctx_weft.core.loop.steps._capabilities import resolve_and_bind
from ctx_weft.core.loop.steps.recognize_intent import should_recognize_intent
from ctx_weft.core.models.task import NormalTaskSettings
from ctx_weft.core.utils.estimate import (
    effective_limit,
    estimate_content_tokens,
    estimate_tokens,
    estimate_tool_calls_tokens,
)
from ctx_weft.protocols.events import EventType


def _estimate_record_tokens(r, count: Callable[[str], int] | None = None) -> int:
    """一条 memory 记录的 compact 估算：content + tool_calls 参数（在 metadata）+ reasoning。

    与 gateway._estimate_message_tokens 同口径（同 core.utils.estimate 计费项），使 prepare 的触发估算
    不再漏 tool_calls 参数/图片/framing（此前只 join content 文本、且丢弃非 str content）。

    count：文本费率经 count 回调走 tokenizer（通常是 ``ctx.llm.tokenizer.count``，已校准）；
    None 回退未校准启发式（纯单测/无 llm 场景）。
    """
    md = getattr(r, "metadata", None) or {}
    total = estimate_content_tokens(r.content, count=count) + estimate_tool_calls_tokens(
        md.get("tool_calls"), count=count)
    reasoning = md.get("reasoning")
    if reasoning:
        total += (count or estimate_tokens)(str(reasoning))
    return total


def _staged_memory(state, ctx) -> list:
    """当前 task 这一轮暂存、还没落盘的 memory 写入（没开窗 / 无 TaskManager → 空）。"""
    staged = getattr(getattr(ctx, "task_manager", None), "staged_memory", None)
    return staged(state.task.id) if staged is not None else []


def _estimate_assembled_tokens(prompt, count: Callable[[str], int] | None = None) -> int:
    """无真实基线（首轮/一次性）时对整份装配 prompt 的估算：system + 每条消息（含 tool_calls
    参数/图片/framing）+ tools schema。

    比 composer 的 ``prompt.token_count``（纯文本、且不含 tools）更全，与 gateway 首次估算同口径——
    tools schema 是每个 act prompt 的固定占用，composer 完全没数，此处补上。

    count：文本费率经 count 回调走 tokenizer（通常是 ``ctx.llm.tokenizer.count``，已校准）；
    None 回退未校准启发式（纯单测/无 llm 场景）。
    """
    from ctx_weft.core.utils.estimate import estimate_tools_tokens

    c = count or estimate_tokens
    total = c(prompt.system or "")
    for m in prompt.messages:
        total += estimate_content_tokens(m.content, count=count) + estimate_tool_calls_tokens(
            m.tool_calls, count=count)
        if getattr(m, "reasoning_content", None):
            total += c(m.reasoning_content)
    total += estimate_tools_tokens(getattr(prompt, "tools", None), count)
    return total


class PrepareStep(Step):
    name = "prepare"

    async def execute(self, state: LoopState, ctx: LoopContext) -> StepOutcome:
        agent = state.agent
        session = state.session

        # ── 0. token budget guard ─────────────────────────────────────────────

        # ── 1. token 估算 ─────────────────────────────────────────────────────
        token_estimate, has_baseline = await self._estimate_tokens(state, ctx)

        # ── 2. capability 解析 ────────────────────────────────────────────────
        template = state.extra.get("template")
        bound_capabilities = await resolve_and_bind(state, ctx)
        # 供下游步骤复用（compact 内联、recognize_intent 并发）：单次解析，多步共享。
        state.extra["bound_capabilities"] = bound_capabilities

        # ── 3. prompt purpose ─────────────────────────────────────────────────
        settings = state.task.settings
        purpose = settings.purpose if isinstance(settings, NormalTaskSettings) else "act"

        def _assemble():
            # 这一轮暂存、还没落盘的对话记录（用户消息 / HITL 答复 / 答复回灌的工具结果，
            # 见 `ingest_or_stage`）。LLM 开口之前这一轮要用的恰恰是它们，所以装配时叠加进
            # 历史。**每次装配现取**：压缩前会先提交，那之后它们已经在 memory 里了。
            staged = _staged_memory(state, ctx)
            return ctx.assembler.assemble(ContextRequest(
                purpose=purpose,
                scope=state.scope,
                task=state.task,
                agent=agent,
                session=session,
                template=template,
                bound_capabilities=bound_capabilities,
                token_counter=ctx.llm.tokenizer.count,
                extra={
                    "staged_memory": staged,
                },
            ))

        prompt = await _assemble()
        if token_estimate == 0:
            token_estimate = _estimate_assembled_tokens(prompt, ctx.llm.tokenizer.count)

        # ── 5. compact 触发：命中则跑升级式 compact，再在压缩后 memory 上重装配一次（Q4=c 校正）──
        if await self._should_compact(state, ctx, token_estimate):
            # **先提交再压缩**：压缩（L0.5 图片降级 / L1 / L3）只折 memory，够不着这一轮暂存
            # 的记录——带图的第一条消息、带图的答复就一张也降不了。提交让它们落盘、进入
            # 压缩的视野；代价是这一轮从此不可撤（LLM 开口前按暂停也不再丢弃）。
            await commit_round(state, ctx)
            from ctx_weft.core.loop.steps.compact import escalating_compact
            compact_events = await escalating_compact(
                state, ctx, token_estimate=token_estimate, trigger="compact")
            for ev in compact_events:
                await ctx.event_bus.emit(ev)
            # 「门开了但一条 MEMORY_COMPACTED 都没产」= 各级可折性 guard 全 noop = 这个 scope
            # 已经压不动了。记进 state.extra 供 ActStep 的 `_can_recover_context` 读：它据此
            # 立刻放弃上下文恢复，而不是把配额一次次耗在注定压不出东西的往返上。
            # 每次开门都重写（含置 False）——压不动是**当时**的结论，不是永久标签：折区随
            # 新回合增长，下一次可能就有可折对象了。
            # 这里只作事实记录，**不判断它该不该否决恢复**：run 开头那次 prepare 的 compact
            # 几乎必然 noop（task 层通常只有一条 USER_PROMPT），那面旗不该算在恢复头上。
            # 解释权在 `_can_recover_context`（它只在已恢复过至少一次时才采信）。
            state.extra[COMPACT_NOOP_KEY] = not any(
                ev.type == EventType.MEMORY_COMPACTED for ev in compact_events)
            prompt = await _assemble()

        # GuidanceSource 已完成本轮所有可能的读取后，再消费一次性转向。这样 Prepare
        # 不需要知道 source 的收集函数，compact 后的第二次 assemble 也仍能看到同一 hint。
        if purpose == "act" and isinstance(settings, NormalTaskSettings):
            state.task.next_step_hint = None

        # ── 6. 元信息补全：此处决定是否需要，commit 决定何时可以启动 ──
        # 未提交时只登记；若本轮已因压缩提前提交，则现在启动。每个 run 只启动一次。
        # 不在可撤销窗口内发模型请求，否则整轮撤销会丢弃它的事件并浪费调用。
        if should_recognize_intent(state.task) and bound_capabilities:
            defer_until_commit(state, ctx)

        # ── 7. 发事件 + 返回 ─────────────────────────────────────────────────
        return StepOutcome(
            next_step="act",
            state_patch={"assembled_prompt": prompt},
            events=[
                make_event(state, EventType.CONTEXT_TOKENS_ESTIMATED, payload={
                    "estimated_tokens": token_estimate,
                    "assembled_tokens": prompt.token_count,
                    "has_baseline": has_baseline,
                }),
                make_event(state, EventType.CONTEXT_ASSEMBLED, payload={"token_count": prompt.token_count}),
                make_event(state, EventType.PREPARE_COMPLETED, payload={
                    "estimated_tokens": token_estimate,
                    "assembled_token_count": prompt.token_count,
                }),
            ],
        )

    # ── private helpers ───────────────────────────────────────────────────────

    async def _should_compact(
        self, state: LoopState, ctx: LoopContext, token_estimate: int
    ) -> bool:
        """纯预算触发（spec 2026-07-01 §3.6）：token 估算 / context_limit ≥ compact_token_ratio。
        消息条数门控（compact_message_delta）已废。"""
        loop_config = state.agent.loop_config
        loop_guard = state.agent.loop_guard
        context_limit = loop_guard.context_limit
        reserve = getattr(loop_guard, "reserved_output_tokens", 0)
        eff = effective_limit(context_limit, reserve)
        if eff > 0 and token_estimate > 0:
            return token_estimate / eff >= loop_config.compact_token_ratio
        return False

    async def _estimate_tokens(
        self, state: LoopState, ctx: LoopContext
    ) -> tuple[int, bool]:
        """增量 token 估算（对齐 miniAgents Reasoner._fetch_base）。

        返回 (token_estimate, has_baseline)。
        """
        guard = state.agent.loop_guard

        if guard.context_tokens > 0:
            try:
                from ctx_weft.protocols import MemoryKind, MemoryScope

                # v2 P3a：TASK 视图 + user/assistant 回合谓词（= 旧 USER_PROMPT+LLM_RESPONSE
                # 口径，与 act._account_tokens 的 context_message_count 对齐）。升序视图：
                # 基线计数之后的即新增记录。
                view = await load_records(ctx.memory,
                    state.scope, MemoryScope.TASK, ctx.provider_ctx)
                convo = [
                    r for r in view
                    if r.kind is MemoryKind.CONVERSATION_TURN and r.role in ("user", "assistant")
                ]
                new_records = convo[guard.context_message_count:]
                # 暂存区里的回合同样会被装配进 prompt（见 `_assemble`），一并计入。
                new_records += [
                    ev for ev in _staged_memory(state, ctx)
                    if ev.kind is MemoryKind.CONVERSATION_TURN and ev.role in ("user", "assistant")
                ]
                count = ctx.llm.tokenizer.count
                delta = sum(_estimate_record_tokens(r, count) for r in new_records)
                return guard.context_tokens + delta, True
            except Exception:
                pass

        return 0, False
