"""Prompt construction and budget enforcement used by Jinja templates.

Templates declare content, order, placement and discard priority through PromptBuilder.
The builder owns the mechanical work: exact token accounting, atomic-group trimming,
message decoration and protocol-preserving emission.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from ctx_weft.core.models.errors import ContextOverflowError
from ctx_weft.core.utils.content import (
    content_to_text,
    content_with_prefix,
    content_with_suffix,
    downgrade_images_to_text,
    image_part_count,
)
from ctx_weft.core.utils.estimate import (
    estimate_content_tokens,
    estimate_tool_calls_tokens,
    estimate_tools_tokens,
)
from ctx_weft.protocols import LLMMessage, LLMTool

OperationKind = Literal["system", "message", "before", "after", "user"]


@dataclass
class PromptOperation:
    kind: OperationKind
    content: str | LLMMessage
    priority: int
    required: bool
    sequence: int
    ref: str | None = None
    group: str | None = None
    timestamp: datetime | str | None = None
    token_count: int = 0
    merge: bool = False


def _intent_values(context) -> tuple[str, bool, str]:
    """Return template name, judge flag and compact scope after validation."""
    if context.purpose == "observe":
        return "observe", context.judge_task, "task"
    if context.purpose == "compact":
        return "compact", False, context.compact_scope
    if context.purpose == "recognize_intent":
        return "metadata", False, "task"
    return "act", False, "task"


def _identity_facet(template, name: str):
    if template is None:
        return None
    key = "recognize_intent" if name == "metadata" else name
    facet = template.identity.get(key)
    if facet is None and name == "metadata":
        facet = template.identity.get("act")
    return facet


def _is_blank(content: object) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content
    for part in content:
        if not hasattr(part, "text") or part.text:
            return False
    return True


def _memory_type(record) -> str:
    from ctx_weft.protocols.memory_compat import legacy_type_of

    return str(record.type or legacy_type_of(record.kind, record.scope, record.role))


def _history_facts(context, records=None) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    from ctx_weft.protocols import MemoryEventType, MemoryKind, MemoryScope

    records = context._records if records is None else records
    task_id = context.task.get("id", "")
    base = max((r.metadata.get("seq_no", 0) for r in records.values()
                if r.scope is MemoryScope.TASK), default=0) + 1
    rows = []
    for block in context.memory_blocks:
        if block["kind"] != "history":
            continue
        record = records[block["memory_id"]]
        if record.kind is not None and record.kind not in (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY):
            continue
        metadata = block["metadata"]
        sequence = metadata.get("sequence", record.metadata.get("seq_no", 0))
        if "staged_order" in metadata and "seq_no" not in record.metadata:
            sequence = base + metadata["staged_order"]
        rows.append((
            (
                record.timestamp.isoformat() if record.timestamp else "",
                sequence,
            ),
            block,
            record,
        ))
    rows.sort(key=lambda item: item[0])

    prepared: list[dict[str, Any]] = []
    for _sort, _block, record in rows:
        role = record.role or "user"
        if record.scope is MemoryScope.AGENT and record.kind is MemoryKind.SUMMARY:
            role = "user"
        content = record.content
        tool_calls = record.metadata.get("tool_calls", []) if role == "assistant" else []
        if _is_blank(content) and not tool_calls:
            continue
        if role == "assistant":
            message = LLMMessage(
                role="assistant",
                content=content,
                tool_calls=[
                    {"id": call.get("id", ""), "name": call.get("name", ""),
                     "input": call.get("input", {})}
                    for call in tool_calls
                ],
            )
        elif role == "tool":
            message = LLMMessage(
                role="tool",
                content=content,
                tool_call_id=record.metadata.get("tool_call_id", ""),
            )
        else:
            message = LLMMessage(role="user", content=content)
        event_type = _memory_type(record)
        is_agent_summary = (
            record.scope is MemoryScope.AGENT and record.kind is MemoryKind.SUMMARY)
        is_task_summary = event_type == str(MemoryEventType.TASK_COMPACT_SUMMARY)
        summary_kind = None
        if is_agent_summary or is_task_summary:
            if is_agent_summary or role == "user":
                summary_kind = "user"
            elif record.metadata.get("task_id") == task_id:
                summary_kind = "assistant_current"
            else:
                summary_kind = "assistant"
        prepared.append(dict(
            ref=f"history:{len(prepared)}",
            message=message,
            memory_type=_memory_type(record),
            timestamp=record.timestamp.isoformat() if record.timestamp else "",
            task_id=record.metadata.get("task_id", ""),
            origin_task_id=record.metadata.get("origin_task_id", ""),
            summary_kind=summary_kind,
        ))

    candidates = [
        i for i, item in enumerate(prepared)
        if item["message"].role == "user" and item["memory_type"] == "user_prompt"
    ]
    matching = [i for i in candidates if prepared[i]["task_id"] == task_id]
    anchor = matching[0] if matching else (candidates[-1] if candidates else None)
    latest = matching[-1] if matching else anchor
    for index, item in enumerate(prepared):
        item["current_prompt"] = index == anchor or (
            item["task_id"] == task_id and item["memory_type"] == "user_prompt")
        item["current_task"] = (
            item["task_id"] == task_id or item["origin_task_id"] == task_id)
        item["priority"] = (
            0 if item["current_prompt"] else
            4 if item["current_task"] else
            2 if item["memory_type"] == "agent_compact_summary" else
            5 if item["memory_type"] == "agent_conversation_turn" else 6
        )
    user_indexes = [i for i, item in enumerate(prepared) if item["message"].role == "user"]
    refs = dict(
        anchor_ref=f"history:{anchor}" if anchor is not None else None,
        latest_ref=f"history:{latest}" if latest is not None else None,
        first_user_ref=f"history:{user_indexes[0]}" if user_indexes else None,
        last_user_ref=f"history:{user_indexes[-1]}" if user_indexes else None,
        last_ref=f"history:{len(prepared) - 1}" if prepared else None,
        last_role=prepared[-1]["message"].role if prepared else None,
    )
    return prepared, refs


_JSON_TYPE_ABBR = {
    "string": "str", "integer": "int", "number": "num", "boolean": "bool",
    "array": "list", "object": "obj", "null": "null",
}


def _compact_signature(schema: object) -> str:
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return ""
    props = schema["properties"]
    required = set(schema.get("required") or ())
    result = []
    for name, spec in list(props.items())[:8]:
        raw_type = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(raw_type, list):
            raw_type = raw_type[0] if raw_type else None
        result.append(
            f"{name}{'' if name in required else '?'}: "
            f"{_JSON_TYPE_ABBR.get(raw_type, raw_type or 'any')}")
    if len(props) > 8:
        result.append("…")
    return ", ".join(result)


def _shift_headings(markdown: str, base_level: int = 2) -> str:
    import re

    pattern = re.compile(r"^(#{1,6})\s")
    lines = markdown.split("\n")
    levels, fenced = [], False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith((chr(96) * 3, "~~~")):
            fenced = not fenced
        elif not fenced and (match := pattern.match(line)):
            levels.append(len(match.group(1)))
    shift = (base_level + 1 - min(levels)) if levels else 0
    if shift <= 0:
        return markdown
    output, fenced = [], False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith((chr(96) * 3, "~~~")):
            fenced = not fenced
        elif not fenced and (match := pattern.match(line)):
            line = "#" * min(6, len(match.group(1)) + shift) + line[len(match.group(1)):]
        output.append(line)
    return "\n".join(output)


def _capability_facts(context, purpose: str) -> list[dict[str, Any]]:
    from ctx_weft.protocols.capability import qualify

    current_skill = context.skill_name
    blocks = [
        block for block in context.capabilities
        if purpose in block["definition"].get("purposes", ())
    ]
    sections = []
    for kind in ("skill", "tool", "agent"):
        candidates = [block for block in blocks if block["definition"]["kind"] == kind]
        if kind == "skill" and current_skill:
            candidates = [
                block for block in candidates
                if qualify(block["definition"]["id"]) != current_skill
            ]
        if not candidates:
            continue
        groups: dict[str, dict[str, Any]] = {}
        ungrouped = []
        for block in candidates:
            cap = block["definition"]
            item = dict(
                name=qualify(cap["id"]),
                signature=_compact_signature(cap.get("input_schema")),
                description=cap["description"],
            )
            provider = block["metadata"].get("provider_name", "")
            if not provider:
                ungrouped.append(item)
                continue
            group = groups.setdefault(provider, dict(
                name=provider,
                prefix=qualify(provider) + "__",
                description=(block["metadata"].get("provider_description") or "").strip(),
                entries=[],
            ))
            group["entries"].append(item)
        sections.append(dict(kind=kind, ungrouped=ungrouped, groups=list(groups.values())))
    return sections


def _prompt_facts(context, records=None, template=None) -> dict[str, Any]:
    from ctx_weft.core.utils.task_ref import task_label_parts, task_ref_parts
    from ctx_weft.protocols.context import normalize_content_parts

    records = context._records if records is None else records
    template_name, judge_task, scope = _intent_values(context)
    task = context.task
    title = task.get("title") or ""
    description = task.get("description") or ""

    def task_content(name):
        record_id = task.get(f"{name}_memory_id")
        return records[record_id].content if record_id else normalize_content_parts(task.get(name))

    raw_prompt = task_content("user_prompt")
    user_prompt = raw_prompt if isinstance(raw_prompt, str) else (
        content_to_text(raw_prompt) if raw_prompt else "")

    identity = template
    actor = _identity_facet(identity, "act")
    selected_name = (
        "observe" if template_name == "observe" and judge_task
        else "compact" if template_name in {"observe", "compact"}
        else template_name
    )
    selected = _identity_facet(identity, selected_name)
    compact = _identity_facet(identity, "compact")
    soul = getattr(selected, "text", "") if template_name == "act" else getattr(actor, "text", "")
    role = getattr(selected, "text", "")

    background_blocks = [item for item in context.memory_blocks + context.references if item["kind"] == "background"]
    background_blocks.sort(key=lambda item: item["position"])
    background = ""
    if background_blocks:
        item = background_blocks[0]
        background = content_to_text(records[item["memory_id"]].content if "memory_id" in item
                                     else normalize_content_parts(item["content"]))
    directive = None
    if context.skill_definitions:
        definition = context.skill_definitions[0]["definition"]
        directive = dict(
            skill_name=definition.get("skill_name", ""),
            body=_shift_headings(definition.get("instructions", "")),
        )

    capability_sections = _capability_facts(context, context.purpose)
    guidance = None
    resume_snapshot = None
    if context.guidance:
        guidance = dict(context.guidance[0]["state"]["guidance"])
        resume_snapshot = context.guidance[0]["state"].get("resume")
    ctx = dict(
        title=title,
        description=description,
        user_prompt=user_prompt,
        task_ref=task_ref_parts(task.get("id", "") or "", title),
        soul=soul,
        project_background=background,
        directive=directive,
        capability_sections=capability_sections,
        guidance=guidance,
        resume_snapshot=resume_snapshot,
        needs_pointer=False,
        scope=scope,
        judge_task=judge_task,
    )
    if template_name == "observe":
        ctx.update(
            switch_reason=task.get("switch_reason", ""),
            terminal_only=context.terminal_only,
            evaluation_role=role if judge_task else "",
            summary_principles=getattr(compact, "text", "") if compact else "",
        )
        boundary = context.observe_boundary or "normal"
        supported_boundaries = {
            "actor_done", "finish", "finish_park", "normal", "plain_text",
            "max_turns", "context_limit", "interrupt", "dispatch", "mechanical", "task_switch",
        }
        ref = task_label_parts(task.get("id", ""), title, raw_prompt) if task.get("id") else ""
        outputs = task_content("outputs")
        output_text = outputs if isinstance(outputs, str) else (
            content_to_text(outputs) if outputs else "")
        inject = boundary in {"actor_done", "finish", "finish_park"}
        ctx.update(
            task_label=ref,
            boundary_kind=(boundary if boundary in supported_boundaries else "normal"),
            final_output=output_text.strip() if inject else "",
        )
        if judge_task:
            subtasks = [
                dict(
                    ref=task_ref_parts(row.get("task_id", "") or "", row.get("title", "") or ""),
                    outcome=row.get("outcome", ""),
                    note=row.get("note"),
                )
                for row in context.subtasks
            ]
            ctx.update(
                plain_text_yielded=boundary == "plain_text",
                finish_park_yielded=boundary == "finish_park",
                subtasks=subtasks,
            )
        else:
            ctx["closed"] = boundary in {"finish", "normal"}
    else:
        ctx["role"] = role

    history, refs = _history_facts(context, records)
    sources = []
    for block in sorted(context.memory_blocks + context.knowledge + context.references,
                        key=lambda item: item["position"]):
        if block["kind"] not in {"blackboard", "summary", "reference"}:
            continue
        metadata = dict(block["metadata"])
        if "memory_id" in block:
            value = records[block["memory_id"]]
            content = value.content
            metadata = {**value.metadata, **metadata}
            timestamp = value.timestamp.isoformat() if value.timestamp else metadata.get("timestamp")
        else:
            value = block.get("document", block)
            content = normalize_content_parts(value["content"])
            metadata = {**value.get("metadata", value.get("value_metadata", {})), **metadata}
            timestamp = value.get("timestamp", metadata.get("timestamp"))
        sources.append(dict(
            kind=block["kind"],
            content=content_to_text(content),
            metadata=metadata,
            timestamp=timestamp,
            group=f"source:{block['id']}",
        ))
    return dict(
        template_name=template_name,
        ctx=ctx,
        history=history,
        source_blocks=sources,
        user_prompt_in_memory=task.get("user_prompt_in_memory", False),
        capabilities_priority=1,
        directive_priority=1,
        **refs,
    )


@dataclass
class PromptBuilder:
    """Mutable output surface exposed to a purpose Jinja template.

    Priority follows the existing convention: zero is pinned; larger numbers are
    discarded first. A group is atomic. If any member is required, the whole group is
    protected.
    """

    token_counter: Callable[[str], int]
    message_transform: Callable[[LLMMessage], LLMMessage] | None = None
    _operations: list[PromptOperation] = field(default_factory=list, init=False)
    _tools: list[LLMTool] = field(default_factory=list, init=False)
    _serial: int = field(default=0, init=False)
    _tool_groups: dict[str, str] = field(default_factory=dict, init=False)
    _last_refs: tuple[str, ...] = field(default=(), init=False)
    _last_trace: list[dict] = field(default_factory=list, init=False)

    def compile(self, context, *, tools: list[LLMTool] | None = None, records=None, template=None):
        """Compile one PromptContext through its executable purpose template."""
        from ctx_weft.core.assembler.prompts import render_purpose
        from ctx_weft.core.utils.estimate import effective_limit, tools_signature

        self._operations.clear()
        self._serial = 0
        self._tool_groups.clear()
        self.tools(tools or [])
        facts = _prompt_facts(context, records, template)
        purpose = facts.pop("template_name")
        self.message_transform = None
        if purpose != "act":
            self.message_transform = lambda message: replace(
                message, content=downgrade_images_to_text(message.content))
        render_purpose(
            purpose,
            prompt=self,
            purpose=purpose,
            context=context,
            **facts,
        )
        assembled = self.build(
            effective_limit(context.context_limit, context.reserved_output_tokens),
            context_limit=context.context_limit,
            reserved_output_tokens=context.reserved_output_tokens,
        )
        assembled.metadata["tools_signature"] = tools_signature(assembled.tools)
        assembled.metadata["purpose"] = context.purpose
        return assembled

    def _add(
        self,
        kind: OperationKind,
        content: str | LLMMessage,
        *,
        priority: int,
        required: bool = False,
        ref: str | None = None,
        group: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> str | None:
        if priority < 0:
            raise ValueError("prompt operation priority must be non-negative")
        if kind in {"before", "after"} and not ref:
            raise ValueError(f"{kind} operation requires a message ref")
        if kind == "message" and not ref:
            ref = f"message:{self._serial}"
        if kind == "user" and ref is None:
            ref = f"generated:{self._serial}"

        if isinstance(content, LLMMessage):
            counted = self.message_transform(content) if self.message_transform else content
            tokens = estimate_content_tokens(counted.content, count=self.token_counter)
            tokens += estimate_tool_calls_tokens(counted.tool_calls, count=self.token_counter)
            if counted.reasoning_content:
                tokens += self.token_counter(counted.reasoning_content)
        else:
            tokens = self.token_counter(content)

        operation = PromptOperation(
            kind=kind,
            content=deepcopy(content),
            priority=priority,
            required=required,
            sequence=self._serial,
            ref=ref,
            group=group,
            timestamp=timestamp,
            token_count=tokens,
        )
        self._serial += 1
        self._operations.append(operation)
        return ref

    def system(
        self, content: str, *, priority: int, required: bool = False,
        group: str | None = None,
    ) -> None:
        if content:
            self._add("system", content, priority=priority, required=required, group=group)

    def message(
        self,
        message: LLMMessage,
        *,
        ref: str | None = None,
        priority: int,
        required: bool = False,
        group: str | None = None,
        timestamp: datetime | str | None = None,
    ) -> str:
        # Derive an atomic group for tool exchanges when the template does not provide one.
        if group is None and message.role == "assistant" and message.tool_calls:
            group = f"tool-exchange:{ref or self._serial}"
            for call in message.tool_calls:
                call_id = str(call.get("id", ""))
                if call_id:
                    self._tool_groups[call_id] = group
        elif group is None and message.role == "tool" and message.tool_call_id:
            group = self._tool_groups.get(message.tool_call_id)

        result = self._add(
            "message", message, ref=ref, priority=priority, required=required,
            group=group, timestamp=timestamp,
        )
        assert result is not None
        return result

    def before(
        self, ref: str, content: str, *, priority: int, required: bool = False,
        group: str | None = None, timestamp: datetime | str | None = None,
    ) -> None:
        if content:
            self._add(
                "before", content, ref=ref, priority=priority,
                required=required, group=group, timestamp=timestamp,
            )

    def after(
        self, ref: str, content: str, *, priority: int, required: bool = False,
        group: str | None = None, timestamp: datetime | str | None = None,
    ) -> None:
        if content:
            self._add(
                "after", content, ref=ref, priority=priority,
                required=required, group=group, timestamp=timestamp,
            )

    def user(
        self,
        content: str,
        *,
        priority: int,
        required: bool = False,
        merge: bool = True,
        group: str | None = None,
    ) -> str | None:
        if not content:
            return None
        # merge is kept on the message as builder-only metadata, rather than leaking into
        # the provider protocol. The operation kind already distinguishes generated users.
        ref = self._add(
            "user", LLMMessage(role="user", content=content), priority=priority,
            required=required, group=group,
        )
        assert ref is not None
        self._operations[-1].merge = merge
        return ref

    def tools(self, tools: list[LLMTool]) -> None:
        self._tools = deepcopy(tools)

    def build(
        self,
        token_limit: int,
        *,
        context_limit: int | None = None,
        reserved_output_tokens: int = 0,
    ):
        """Trim declared operations and materialize an AssembledPrompt."""
        from ctx_weft.core.assembler.assembler import AssembledPrompt

        operations = list(self._operations)
        tools_tokens = estimate_tools_tokens(self._tools, self.token_counter)
        content_limit = max(0, token_limit - tools_tokens)

        def group_key(op: PromptOperation) -> str:
            return op.group or f"operation:{op.sequence}"

        grouped: dict[str, list[PromptOperation]] = defaultdict(list)
        for operation in operations:
            grouped[group_key(operation)].append(operation)

        kept_groups = set(grouped)
        total = sum(op.token_count for op in operations)
        owner_group = {
            operation.ref: group_key(operation)
            for operation in operations
            if operation.kind in {"message", "user"} and operation.ref is not None
        }
        decoration_groups: dict[str, set[str]] = defaultdict(set)
        for operation in operations:
            if operation.kind in {"before", "after"} and operation.ref in owner_group:
                decoration_groups[owner_group[operation.ref]].add(group_key(operation))

        def discard_group(key: str) -> None:
            """Drop a group and decorations whose target disappears with it."""
            nonlocal total
            pending = [key]
            while pending:
                current = pending.pop()
                if current not in kept_groups:
                    continue
                kept_groups.discard(current)
                total -= sum(member.token_count for member in grouped[current])
                pending.extend(decoration_groups.get(current, ()))

        candidates = []
        for key, members in grouped.items():
            required = any(member.required or member.priority == 0 for member in members)
            priority = max(member.priority for member in members)
            timestamp = min(
                (member.timestamp.isoformat()
                 if isinstance(member.timestamp, datetime) else str(member.timestamp)
                 for member in members
                 if member.timestamp is not None),
                default="",
            )
            tokens = sum(member.token_count for member in members)
            candidates.append((required, -priority, timestamp, -tokens, key))

        for required, _negative_priority, _timestamp, _negative_tokens, key in sorted(candidates):
            if total <= content_limit:
                break
            if required:
                continue
            discard_group(key)

        if total > content_limit:
            floor = [
                op for op in operations
                if group_key(op) in kept_groups and (op.required or op.priority == 0)
            ]
            images = sum(
                image_part_count(op.content.content)
                for op in floor
                if isinstance(op.content, LLMMessage)
            )
            raise ContextOverflowError(
                required=sum(op.token_count for op in floor) + tools_tokens,
                effective_limit=token_limit,
                context_limit=context_limit if context_limit is not None else token_limit,
                reserved_output_tokens=reserved_output_tokens,
                image_count=images,
            )

        kept = [op for op in operations if group_key(op) in kept_groups]
        kept_refs = {op.ref for op in kept if op.kind in {"message", "user"}}
        kept = [
            op for op in kept
            if op.kind not in {"before", "after"} or op.ref in kept_refs
        ]

        messages: list[LLMMessage] = []
        message_refs: list[str] = []
        trace: list[dict[str, Any]] = []
        ref_map: dict[str, int] = {}
        pending: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: {"before": [], "after": []})

        def decorate(index: int, ref: str, *, before: str = "", after: str = "") -> None:
            message = messages[index]
            content = message.content
            if before:
                content = content_with_prefix(content, before)
            if after:
                content = content_with_suffix(content, after)
            messages[index] = replace(message, content=content)
            if before or after:
                trace.append(
                    dict(operation="decorate", ref=ref, before=before, after=after))

        def append_message(
            message: LLMMessage, ref: str, *, merge: bool = False,
        ) -> int | None:
            if (
                message.content == ""
                and not message.tool_calls
                and not message.tool_call_id
                and message.reasoning_content is None
            ):
                return None
            last = messages[-1] if messages else None
            if (
                merge
                and last is not None
                and last.role == message.role == "user"
                and not last.tool_calls
                and not message.tool_calls
                and not last.tool_call_id
                and not message.tool_call_id
                and message.reasoning_content is None
            ):
                if not isinstance(message.content, str):
                    raise TypeError("merged append requires text")
                decorate(
                    len(messages) - 1,
                    message_refs[-1],
                    after="\n\n" + message.content,
                )
                trace.append(dict(operation="merge", ref=message_refs[-1]))
                return len(messages) - 1
            messages.append(deepcopy(message))
            message_refs.append(ref)
            trace.append(dict(operation="insert", ref=ref, index=len(messages) - 1))
            return len(messages) - 1

        def flush_decorations() -> None:
            for ref, values in list(pending.items()):
                if ref in ref_map:
                    decorate(
                        ref_map[ref],
                        ref,
                        before="".join(values["before"]),
                        after="".join(values["after"]),
                    )
                del pending[ref]

        for operation in sorted(kept, key=lambda op: op.sequence):
            if operation.kind == "system":
                continue
            if operation.kind == "message":
                flush_decorations()
                assert isinstance(operation.content, LLMMessage)
                assert operation.ref is not None
                index = append_message(operation.content, operation.ref)
                if index is not None:
                    ref_map[operation.ref] = index
            elif operation.kind == "user":
                flush_decorations()
                assert isinstance(operation.content, LLMMessage)
                assert operation.ref is not None
                index = append_message(
                    operation.content, operation.ref, merge=operation.merge)
                if index is not None:
                    ref_map[operation.ref] = index
            elif operation.kind in {"before", "after"} and operation.ref is not None:
                assert isinstance(operation.content, str)
                pending[operation.ref][operation.kind].append(operation.content)
        flush_decorations()

        system = "".join(
            op.content for op in sorted(kept, key=lambda op: op.sequence)
            if op.kind == "system" and isinstance(op.content, str)
        )
        if self.message_transform is not None:
            messages = [self.message_transform(message) for message in messages]
        self._last_refs = tuple(message_refs)
        self._last_trace = trace
        final_count = self.token_counter(system) + sum(
            estimate_content_tokens(message.content, count=self.token_counter)
            + estimate_tool_calls_tokens(message.tool_calls, count=self.token_counter)
            + (self.token_counter(message.reasoning_content) if message.reasoning_content else 0)
            for message in messages
        ) + tools_tokens
        return AssembledPrompt(
            system=system,
            messages=messages,
            tools=deepcopy(self._tools),
            token_count=final_count,
            metadata={"tools_reserved_tokens": tools_tokens},
        )

    @property
    def refs(self) -> tuple[str, ...]:
        """References from the most recent build, after trimming and merges."""
        return self._last_refs

    @property
    def trace(self) -> list[dict]:
        """Editing trace from the most recent build."""
        return list(self._last_trace)
