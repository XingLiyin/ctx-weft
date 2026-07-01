"""PrepareStep（资源装配）：能力解析/绑定 + token 估算 + compact 触发 + prompt 装配。

token 估算对齐 miniAgents Reasoner._fetch_base：
  - 有真实基线（loop_guard.context_tokens > 0）→ 增量估算：基线 + 新增消息估算
  - 无基线 → 用装配后的 prompt.token_count（全量估算）

compact 触发（纯预算，spec 2026-07-01 §3.6）：
  - token_estimate / context_limit >= compact_token_ratio
  命中后调 escalating_compact 升级式压缩，再完整重装配一次 prompt（Q4=c 校正）。
"""

from __future__ import annotations

import logging

from ctx_weft.core.assembler import ContextRequest
from ctx_weft.core.events import EventType
from ctx_weft.core.loop.driver import LoopContext, LoopState, Step, StepOutcome, make_event
from ctx_weft.core.loop.steps._capabilities import resolve_and_bind
from ctx_weft.core.orchestrator.skill_executor_capability import (
    EXEC_SCRIPT_NAME,
    LIST_FILES_NAME,
    READ_FILE_NAME,
)
from ctx_weft.core.loop.steps.recognize_intent import (
    launch_recognize_intent,
    should_recognize_intent,
)
from ctx_weft.core.state.models import NormalTaskSettings
from ctx_weft.core.utils import estimate_tokens
from ctx_weft.protocols import MemoryEventType
from ctx_weft.protocols.capability import SkillCapability

logger = logging.getLogger(__name__)


_SKILL_SCRIPT_RUNTIME_NOTE = (
    "---\n"
    "**Runtime note (overrides any 'run python ...' wording above):** "
    "This skill's files are NOT in your working directory. To run ANY script the "
    f"instructions reference, you MUST call the `{EXEC_SCRIPT_NAME}` tool — e.g. "
    f"`{EXEC_SCRIPT_NAME}(script_path='scripts/foo.py', args='...')` — never run "
    "`python scripts/foo.py` yourself (the path will not resolve). To read a skill "
    f"file use `{READ_FILE_NAME}`; to list skill files use `{LIST_FILES_NAME}`. "
    "Do not build absolute paths by hand. Applies to local and remote skills alike."
)


def wrap_skill_instructions(instructions: str) -> str:
    """Append the exec_script runtime note after a non-empty SKILL.md body."""
    if not instructions:
        return ""
    return f"{instructions}\n\n{_SKILL_SCRIPT_RUNTIME_NOTE}"


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

        # ── 3. skill instructions ─────────────────────────────────────────────
        settings = state.task.settings
        skill_name = settings.skill_name if isinstance(settings, NormalTaskSettings) else ""
        skill_instructions = await self._load_skill_instructions(state, ctx, skill_name)

        # ── 4. 装配 prompt ────────────────────────────────────────────────────
        purpose = settings.purpose if isinstance(settings, NormalTaskSettings) else "act"

        def _assemble():
            return ctx.assembler.assemble(ContextRequest(
                purpose=purpose,
                scope=state.scope,
                task=state.task,
                agent=agent,
                session=session,
                template=template,
                bound_capabilities=bound_capabilities,
                extra={"skill_instructions": skill_instructions, "skill_name": skill_name},
            ))

        prompt = await _assemble()
        if token_estimate == 0:
            token_estimate = prompt.token_count

        # ── 5. compact 触发：命中则跑升级式 compact，再在压缩后 memory 上重装配一次（Q4=c 校正）──
        if await self._should_compact(state, ctx, token_estimate):
            from ctx_weft.core.loop.steps.compact import escalating_compact
            for ev in await escalating_compact(state, ctx, token_estimate=token_estimate, trigger="compact"):
                await ctx.event_bus.emit(ev)
            prompt = await _assemble()

        # ── 6. 会话意图识别：root task 首轮（无标题）时旁路快照运行，与后续 act 并发（不阻塞）──
        if should_recognize_intent(state.task) and bound_capabilities:
            launch_recognize_intent(state, ctx)

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

    async def _load_skill_instructions(
        self, state: LoopState, ctx: LoopContext, skill_name: str
    ) -> str:
        if not skill_name or not ctx.skill_provider_index:
            return ""
        skill_cap = (
            ctx.capability_cache.get_by_qualified_name(state.agent.id, skill_name)
            if ctx.capability_cache else None
        )
        if not isinstance(skill_cap, SkillCapability):
            return ""
        provider_name = skill_cap.id.rsplit(":", 1)[0]
        skill_provider = ctx.skill_provider_index.get(provider_name)
        if skill_provider is None:
            return ""
        try:
            defn = await skill_provider.load_definition(skill_cap.name, ctx.provider_ctx)
            if defn:
                # 原始 SKILL.md 正文 + exec_script 运行时说明（追加在正文之后，
                # recency 最强）；skill 名 + 标题层级由 composer 装配进
                # "## Instructions for the current task" 段。
                return wrap_skill_instructions(defn.instructions)
        except Exception:
            logger.exception("PrepareStep: failed to load skill definition for '%s'", skill_name)
        return ""

    async def _should_compact(
        self, state: LoopState, ctx: LoopContext, token_estimate: int
    ) -> bool:
        """纯预算触发（spec 2026-07-01 §3.6）：token 估算 / context_limit ≥ compact_token_ratio。
        消息条数门控（compact_message_delta）已废。"""
        loop_config = state.agent.loop_config
        context_limit = state.agent.loop_guard.context_limit
        if context_limit > 0 and token_estimate > 0:
            return token_estimate / context_limit >= loop_config.compact_token_ratio
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
                recent = await ctx.memory.recall_recent(
                    scope=state.scope,
                    types=[
                        MemoryEventType.USER_PROMPT,
                        MemoryEventType.LLM_RESPONSE,
                        MemoryEventType.OBSERVER_SUMMARY,
                        MemoryEventType.COMPACT_SUMMARY,
                    ],
                    limit=10000,
                    ctx=ctx.provider_ctx,
                )
                new_records = recent[:max(0, len(recent) - guard.context_message_count)]
                new_text = " ".join(
                    r.content if isinstance(r.content, str) else ""
                    for r in new_records
                )
                return guard.context_tokens + estimate_tokens(new_text), True
            except Exception:
                pass

        return 0, False
