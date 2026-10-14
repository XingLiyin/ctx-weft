"""Compile structured messages through a Jinja entry; apply request policy at emission."""
from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ctx_weft.core.assembler.loop_adapter import (
    _BOUNDARY_FACTS,
    _OUTPUTS_BEARING_BOUNDARIES,
    _RENDERABLE_KINDS,
    CLOSE_BOUNDARIES,
    LoopPromptAdapter,
    _boundary_fact,
    _finish_result_text,
    _is_blank_content,
)
from ctx_weft.core.assembler.prompt_document import MessageRef
from ctx_weft.core.assembler.request_policy import (
    ActIntent,
    CompactIntent,
    MetadataIntent,
    PromptIntent,
    RequestPolicy,
    ReviewIntent,
    capability_purpose_for_request,
    prompt_intent_for_purpose,
    resolve_prompt_policy,
    select_identity_facet,
)
from ctx_weft.core.utils.content import content_to_text, downgrade_images_to_text, image_tokens
from ctx_weft.protocols import LLMMessage

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssembledPrompt, ContextBlock, ContextRequest

# Compatibility exports; implementation lives in the policy/adapter modules.
__all__ = [
    "ActIntent", "CompactIntent", "MetadataIntent", "PromptIntent", "ReviewIntent",
    "Composer", "DefaultComposer", "PromptPlan", "capability_purpose_for_request",
    "prompt_intent_for_purpose", "select_identity_facet", "CLOSE_BOUNDARIES",
    "_BOUNDARY_FACTS", "_OUTPUTS_BEARING_BOUNDARIES", "_boundary_fact",
    "_finish_result_text", "_is_blank_content",
]

logger = logging.getLogger(__name__)


@runtime_checkable
class Composer(Protocol):
    async def compose(self, blocks: list[ContextBlock], request: ContextRequest) -> AssembledPrompt:
        ...


@dataclasses.dataclass(frozen=True)
class PromptPlan:
    policy: RequestPolicy
    system: str
    messages: list[LLMMessage]
    refs: tuple[MessageRef, ...]
    trace: list[dict]


class DefaultComposer(LoopPromptAdapter):
    """Default loop integration; generic editing itself lives in PromptDocument.

    Inheriting the adapter keeps existing private helper callers compatible. New code
    should use plan/compose/preview, or use PromptDocument independently of the loop.
    """

    async def compose(self, blocks: list[ContextBlock], request: ContextRequest) -> AssembledPrompt:
        return self.emit(await self.plan(blocks, request), request)

    async def plan(self, blocks: list[ContextBlock], request: ContextRequest) -> PromptPlan:
        unsupported = sorted({b.kind for b in blocks} - _RENDERABLE_KINDS)
        if unsupported:
            sources = sorted({b.source for b in blocks if b.kind in unsupported})
            logger.warning(
                "DefaultComposer: no rendering path for block kind(s) %s "
                "(sources: %s) — these blocks are dropped from the prompt", unsupported, sources)
        policy = resolve_prompt_policy(request)
        document, entry, context = self.prepare(blocks, request, policy)
        document.set_system(entry.system(context))
        document.append(LLMMessage(role="user", content=entry.instruction(context)), merge_with_last=True)
        messages = document.messages
        # Image policy is applied before token counting, exactly as on the send path.
        if policy.images == "text_only":
            messages = [dataclasses.replace(m, content=downgrade_images_to_text(m.content)) for m in messages]
        return PromptPlan(policy, document.system, messages, document.refs, document.trace)

    @staticmethod
    def emit(plan: PromptPlan, request: ContextRequest) -> AssembledPrompt:
        from ctx_weft.core.assembler.assembler import AssembledPrompt

        count = request.token_counter(plan.system) + sum(
            request.token_counter(content_to_text(m.content)) + image_tokens(m.content)
            for m in plan.messages)
        # ContextAssembler installs a live tools resolver; never snapshot it here.
        return AssembledPrompt(system=plan.system, messages=plan.messages, tools=[], token_count=count)

    async def preview(self, blocks: list[ContextBlock], request: ContextRequest) -> dict:
        """Exact emitted text/parts plus stable targets and editing trace; no tool lookup."""
        plan = await self.plan(blocks, request)
        return dict(
            template=plan.policy.template, system=plan.system,
            messages=[dict(ref=ref.value, message=dataclasses.asdict(message))
                      for ref, message in zip(plan.refs, plan.messages, strict=True)],
            trace=plan.trace, token_count=self.emit(plan, request).token_count,
        )

