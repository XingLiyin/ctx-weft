"""Loop request policies, independent of text and message editing."""
from __future__ import annotations

import dataclasses
from typing import Literal, TypeAlias

CompactScope: TypeAlias = Literal["task", "agent"]
PromptFacet: TypeAlias = Literal["act", "observe", "compact", "metadata"]
ImagePolicy: TypeAlias = Literal["preserve", "text_only"]


@dataclasses.dataclass(frozen=True)
class ActIntent:
    """执行当前任务。"""


@dataclasses.dataclass(frozen=True)
class ReviewIntent:
    """Observe always uses one purpose; only task judgement is optional."""

    judge_task: bool = True


@dataclasses.dataclass(frozen=True)
class CompactIntent:
    """压缩 task 或 agent 记忆。"""

    scope: CompactScope = "task"

    def __post_init__(self) -> None:
        if self.scope not in ("task", "agent"):
            raise ValueError(f"unsupported compact scope: {self.scope!r}")


@dataclasses.dataclass(frozen=True)
class MetadataIntent:
    """识别并填写 task metadata。"""


PromptIntent: TypeAlias = ActIntent | ReviewIntent | CompactIntent | MetadataIntent


def prompt_intent_for_purpose(purpose: str, extra: dict | None = None) -> PromptIntent:
    """Select compilation options from the business purpose."""
    if purpose == "act":
        return ActIntent()
    if purpose == "observe":
        return ReviewIntent(judge_task=(extra or {}).get("judge_task", True))
    if purpose == "compact":
        scope = "agent" if (extra or {}).get("compact_scope") == "agent" else "task"
        return CompactIntent(scope=scope)
    if purpose == "recognize_intent":
        return MetadataIntent()
    raise ValueError(f"unsupported prompt purpose: {purpose!r}")



@dataclasses.dataclass(frozen=True)
class RequestPolicy:
    template: str
    facet: PromptFacet
    capability_purpose: str
    images: ImagePolicy
    scope: CompactScope = "task"
    judge_task: bool = False


def compile_prompt_policy(intent: PromptIntent) -> RequestPolicy:
    if isinstance(intent, ActIntent):
        return RequestPolicy("act", "act", "act", "preserve")
    if isinstance(intent, ReviewIntent):
        facet = "observe" if intent.judge_task else "compact"
        return RequestPolicy("observe", facet, "observe", "text_only", judge_task=intent.judge_task)
    if isinstance(intent, CompactIntent):
        return RequestPolicy("compact", "compact", "compact", "text_only", intent.scope)
    if isinstance(intent, MetadataIntent):
        return RequestPolicy("metadata", "metadata", "recognize_intent", "text_only")
    raise TypeError(f"unsupported prompt intent: {type(intent).__name__}")


def resolve_prompt_policy(request) -> RequestPolicy:
    intent = getattr(request, "intent", None)
    if intent is None:
        intent = prompt_intent_for_purpose(getattr(request, "purpose", "act"), getattr(request, "extra", None))
    policy = compile_prompt_policy(intent)
    purpose = getattr(request, "purpose", policy.capability_purpose)
    if purpose != policy.capability_purpose:
        raise ValueError(f"prompt intent does not match purpose: {purpose!r}")
    return policy


def capability_purpose_for_request(request) -> str:
    return resolve_prompt_policy(request).capability_purpose


_TEMPLATE_KEY_BY_FACET: dict[PromptFacet, str] = {
    "act": "act",
    "observe": "observe",
    "compact": "compact",
    # 本地模板文件名是 METADATA.md，但存量 AgentTemplate key 是 recognize_intent。
    "metadata": "recognize_intent",
}


def select_identity_facet(template, facet_name: PromptFacet):
    """Select the host facet; only metadata may fall back to the actor identity."""
    if template is None:
        return None
    facet = template.identity.get(_TEMPLATE_KEY_BY_FACET[facet_name])
    if facet is None and facet_name == "metadata":
        facet = template.identity.get("act")
    return facet
