"""Persist assembler inputs, not rendered prompts.

Memory blocks keep immutable record IDs and their collection-time placement.
Other sources have explicit fields: capabilities, skill definitions, guidance and
knowledge. Identity/purpose templates are deliberately loaded again on inspection.
No providers, retrieval queries or live task state are consulted during compilation.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

from ctx_weft.core.assembler.assembler import (
    ActIntent,
    CompactIntent,
    ContextAssembler,
    ContextBlock,
    ContextRequest,
    MetadataIntent,
    ReviewIntent,
)
from ctx_weft.core.assembler.builder import PromptBuilder
from ctx_weft.core.assembler.context import PromptContext
from ctx_weft.core.utils.content import (
    content_to_event_jsonable,
    content_to_jsonable,
    redact_content_for_event,
)
from ctx_weft.core.utils.estimate import estimate_tokens
from ctx_weft.protocols import LLMMessage, LLMTool, MemoryAddress, MemoryRecord
from ctx_weft.protocols.capability import (
    AgentCapability,
    SkillCapability,
    SkillDefinition,
    ToolCapability,
)
from ctx_weft.protocols.knowledge import KnowledgeDoc
from ctx_weft.protocols.memory import MemoryEventType, MemoryKind, MemoryScope


def recording_counter(counter, counts):
    """Preserve a custom/calibrated counter's results without saving its text.

    Restoration reuses these counts for unchanged inputs. Newly edited template
    text falls back to the supplied current counter. No tokenizer state or code
    has to be serialized.
    """
    def count(text):
        value = counter(text)
        counts[sha256(text.encode("utf-8", "surrogatepass")).hexdigest()] = value
        return value
    return count


def _json(value):
    """Detach JSON-compatible input values at collection time."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {f.name: _json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported prompt input: {type(value).__name__}")


def capture_prompt(context: PromptContext) -> dict[str, Any]:
    """Capture exactly the collected sources; never fetch or render again here."""
    request = context.request
    task_block = context.first("task_spec")
    task = task_block.value if task_block else request.task
    extra = request.extra or {}
    intent = request.intent
    snapshot = {
        "version": 1,
        "purpose": request.purpose,
        "template_id": getattr(request.template, "id", "") or "",
        "scope": {name: getattr(request.scope, name, None)
                  for name in ("session_id", "task_id", "agent_id")},
        "task": {
            name: _json(getattr(task, name, default))
            for name, default in (
                ("id", ""), ("title", ""), ("description", ""), ("user_prompt", None),
                ("user_prompt_in_memory", False), ("outputs", None), ("switch_reason", ""),
            )
        },
        "skill_name": getattr(getattr(task, "settings", None), "skill_name", ""),
        "context_limit": request.session.context_limit,
        "reserved_output_tokens": request.session.reserved_output_tokens,
        "judge_task": getattr(intent, "judge_task", extra.get("judge_task", True)),
        "terminal_only": bool(getattr(intent, "terminal_only", False)),
        "compact_scope": getattr(intent, "scope", extra.get("compact_scope", "task")),
        "observe_boundary": extra.get("observe_boundary", "normal"),
        "subtasks": _json(extra.get("subtasks", [])),
        "memory_blocks": [],
        "capabilities": [],
        "skill_definitions": [],
        "guidance": [],
        "knowledge": [],
        "references": [],
    }
    staged_ids = {event.id for event in extra.get("staged_memory", [])}
    for position, block in enumerate(context.blocks):
        if block.kind in {"identity", "task_spec"}:
            continue
        item = {
            "position": position, "id": block.id, "source": block.source,
            "kind": block.kind, "metadata": _json(block.metadata),
        }
        value = block.value
        if isinstance(value, MemoryRecord):
            item["memory_id"] = value.id
            # These records may still belong to an uncommitted human round.
            # Do not commit the round early just to make a debug reference readable.
            if value.id in staged_ids or not value.id:
                item["record"] = _json(value)
            snapshot["memory_blocks"].append(item)
        elif block.kind == "capabilities":
            item["definition"] = {
                key: _json(getattr(value, key))
                for key in ("id", "name", "kind", "description", "purposes", "input_schema")
                if hasattr(value, key)
            }
            # input_schema already lives in definition; do not serialize it twice.
            item["metadata"].pop("input_schema", None)
            snapshot["capabilities"].append(item)
        elif block.kind == "directive" and isinstance(value, SkillDefinition):
            item["definition"] = _json(value)
            snapshot["skill_definitions"].append(item)
        elif block.kind == "guidance":
            item["state"] = _json(value)
            snapshot["guidance"].append(item)
        elif block.kind == "reference" and isinstance(value, KnowledgeDoc):
            item["document"] = _json(value)
            snapshot["knowledge"].append(item)
        elif block.kind in {"background", "blackboard", "summary", "reference"}:
            # Custom Sources may supply reference content without a memory row.
            item["content"] = _json(getattr(value, "content", value))
            item["value_metadata"] = _json(getattr(value, "metadata", {}))
            item["timestamp"] = _json(getattr(value, "timestamp", None))
            snapshot["references"].append(item)
        else:
            raise TypeError(f"unsupported prompt source {block.source!r} ({block.kind})")
    # Task state sometimes repeats a large user input/output already in memory.
    # Reference the identical collected value instead of serializing it twice.
    for name in ("user_prompt", "outputs"):
        value = getattr(task, name, None)
        if not value:
            continue
        match = next((block.value for block in context.blocks
                      if isinstance(block.value, MemoryRecord) and block.value.id
                      and block.value.content == value), None)
        if match is not None:
            snapshot["task"].pop(name)
            snapshot["task"][f"{name}_memory_id"] = match.id
    return snapshot


def request_snapshot(prompt, messages: list[LLMMessage]) -> dict | None:
    """Add only the messages appended after assembly; no compiled prefix."""
    snapshot = getattr(prompt, "input_snapshot", None)
    if snapshot is None:
        return None
    appended = []
    for message in messages[len(prompt.messages):]:
        item = {"role": message.role}
        if message.memory_id:
            item["memory_id"] = message.memory_id
        else:
            item["content"] = content_to_jsonable(message.content)
        # Persisted assistant records can omit silent/dispatch calls. Keep the
        # actual request's call list, including an explicit empty list.
        item["tool_calls"] = _json(message.tool_calls)
        item["tool_call_id"] = message.tool_call_id
        appended.append(item)
    return {**snapshot, "appended_messages": appended}


async def externalize_snapshot_images(snapshot, *, event_blob_store, ctx):
    """Never introduce inline image bytes into a prompt event.

    Memory references stay references. Only unpersisted input content can still
    contain inline images; send those through the existing event blob boundary.
    """
    from ctx_weft.protocols.context import normalize_content_parts

    async def content(value):
        if not isinstance(value, list):
            return value
        result = []
        for part in value:
            if isinstance(part, dict) and part.get("type") == "image" and part.get("source_type", "base64") != "ref":
                parts = normalize_content_parts([part])
                if event_blob_store is not None and event_blob_store.can_externalize:
                    result.extend(await content_to_event_jsonable(
                        parts, event_blob_store=event_blob_store, ctx=ctx))
                else:
                    result.append({"type": "text", "text": redact_content_for_event(parts)})
            else:
                result.append(part)
        return result

    result = {**snapshot, "task": dict(snapshot["task"])}
    for key in ("user_prompt", "outputs"):
        if key in result["task"]:
            result["task"][key] = await content(result["task"][key])
    for group, key in (("appended_messages", "content"), ("references", "content")):
        result[group] = [{**item, key: await content(item[key])} if key in item else item
                         for item in snapshot.get(group, [])]
    result["memory_blocks"] = [
        {**item, "record": {**item["record"], "content": await content(item["record"]["content"])}}
        if "record" in item else item for item in snapshot["memory_blocks"]
    ]
    return result


def _record(data: dict) -> MemoryRecord:
    data = dict(data)
    data["timestamp"] = datetime.fromisoformat(data["timestamp"])
    for name, klass in (("type", MemoryEventType), ("kind", MemoryKind), ("scope", MemoryScope)):
        if data.get(name) is not None:
            data[name] = klass(data[name])
    if data.get("address") is not None:
        data["address"] = MemoryAddress(**data["address"])
    return MemoryRecord(**data)


async def restore_prompt(snapshot, *, memory, ctx, template, token_counter=estimate_tokens):
    """Recompile saved inputs with current templates, without running Sources."""
    if snapshot.get("version") != 1:
        raise ValueError("unsupported prompt snapshot version")
    scope = MemoryAddress(**snapshot["scope"])
    if scope.session_id != ctx.session_id:
        raise ValueError("prompt snapshot belongs to a different session")
    counts = snapshot.get("token_counts", {})
    fallback_counter = token_counter

    def saved_counter(text):
        saved = counts.get(sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
        return saved if saved is not None else fallback_counter(text)

    token_counter = saved_counter
    items = snapshot["memory_blocks"]
    appended = snapshot.get("appended_messages", [])
    inline = {item["memory_id"]: _record(item["record"])
              for item in items if "record" in item}
    task_ids = [snapshot["task"][key] for key in ("user_prompt_memory_id", "outputs_memory_id")
                if key in snapshot["task"]]
    ids = list(dict.fromkeys(
        [item["memory_id"] for item in items if "record" not in item]
        + [item["memory_id"] for item in appended if item.get("memory_id")]
        + [record_id for record_id in task_ids if record_id not in inline]
    ))
    records = {record.id: record for record in await memory.read_records(ids, ctx)} if ids else {}
    missing = set(ids) - records.keys()
    if missing:
        raise ValueError(f"prompt memory records missing: {sorted(missing)}")
    records.update(inline)
    blocks = []

    def add(item, value):
        blocks.append((item["position"], ContextBlock(
            id=item["id"], source=item["source"], kind=item["kind"],
            metadata=item["metadata"], value=value,
        )))

    for item in items:
        add(item, records[item["memory_id"]])
    classes = {"tool": ToolCapability, "skill": SkillCapability, "agent": AgentCapability}
    for item in snapshot["capabilities"]:
        definition = item["definition"]
        add(item, classes[definition["kind"]](**definition))
    for item in snapshot["skill_definitions"]:
        add(item, SkillDefinition(**item["definition"]))
    for item in snapshot["guidance"]:
        add(item, item["state"])
    for item in snapshot["knowledge"]:
        add(item, KnowledgeDoc(**item["document"]))
    for item in snapshot["references"]:
        timestamp = item["timestamp"]
        from ctx_weft.protocols.context import normalize_content_parts

        add(item, SimpleNamespace(content=normalize_content_parts(item["content"]), metadata=item["value_metadata"],
                                  timestamp=datetime.fromisoformat(timestamp) if timestamp else None))
    task_state = dict(snapshot["task"])
    for name in ("user_prompt", "outputs"):
        record_id = task_state.pop(f"{name}_memory_id", None)
        if record_id:
            task_state[name] = records[record_id].content
    task = SimpleNamespace(**task_state, settings=SimpleNamespace(skill_name=snapshot["skill_name"]))
    if isinstance(task.user_prompt, list):
        from ctx_weft.protocols.context import normalize_content_parts
        task.user_prompt = normalize_content_parts(task.user_prompt)
    if isinstance(task.outputs, list):
        from ctx_weft.protocols.context import normalize_content_parts
        task.outputs = normalize_content_parts(task.outputs)
    purpose = snapshot["purpose"]
    intent = {
        "act": lambda: ActIntent(),
        "observe": lambda: ReviewIntent(snapshot["judge_task"], snapshot["terminal_only"]),
        "compact": lambda: CompactIntent(snapshot["compact_scope"]),
        "recognize_intent": lambda: MetadataIntent(),
    }[purpose]()
    request = ContextRequest(
        purpose=purpose, scope=scope, task=task,
        agent=SimpleNamespace(id=scope.agent_id),
        session=SimpleNamespace(context_limit=snapshot["context_limit"],
                                reserved_output_tokens=snapshot["reserved_output_tokens"]),
        template=template, bound_capabilities=[], token_counter=token_counter, intent=intent,
        extra={"observe_boundary": snapshot["observe_boundary"], "subtasks": snapshot["subtasks"]},
    )
    context = PromptContext(request, tuple(block for _, block in sorted(blocks, key=lambda pair: pair[0])))
    # Budget the compiled prefix against assembly-time tools. Runtime pins only
    # change the eventual request tools, not the already compiled message prefix.
    prompt = PromptBuilder(token_counter).compile(context, tools=ContextAssembler._tools_snapshot(context))
    if "tools" in snapshot:
        prompt.tools = [LLMTool(**tool) for tool in snapshot["tools"]]
    for item in appended:
        content = records[item["memory_id"]].content if item.get("memory_id") else item["content"]
        prompt.messages.append(LLMMessage(
            role=item["role"], content=content, tool_calls=item["tool_calls"],
            tool_call_id=item["tool_call_id"],
        ))
    return prompt


def prompt_body(prompt) -> dict:
    """Keep the existing debug-card response format and image redaction."""
    return {
        "system_prompt": prompt.system,
        "messages": [{"role": message.role, "content": redact_content_for_event(message.content)}
                     for message in prompt.messages],
        "tool_names": [tool.name for tool in prompt.tools],
    }
