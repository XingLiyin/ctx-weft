"""Task-memory facts and presentation; persisted records are never changed on read."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta
from typing import Any, TypeVar

from ctx_weft.core.assembler.prompts import runtime_prompt
from ctx_weft.protocols import MemoryEvent, MemoryKind, MemoryRecord, MemoryScope

_Record = TypeVar("_Record", MemoryEvent, MemoryRecord)

LEGACY_FINISH = "control__finish_task"
LEGACY_START = frozenset({"control__start_task", "control__delegate_task"})


def task_record_kind(record: MemoryEvent | MemoryRecord) -> str | None:
    if (
        getattr(record, "scope", None) != MemoryScope.AGENT
        or record.kind != MemoryKind.CONVERSATION_TURN
    ):
        return None
    md = record.metadata or {}
    tagged = md.get("task_memory", {})
    if tagged.get("version") == 1:
        return tagged.get("kind")
    for call in md.get("tool_calls") or []:
        if call.get("name") == LEGACY_FINISH:
            return "final_reply" if md.get("final_reply") else "recap"
        if call.get("name") in LEGACY_START:
            return "start"
    if md.get("finish_policy"):
        return "finish" if record.role == "tool" else "recap"
    if record.role == "tool" and md.get("child_task_id"):
        return "result"
    return None


def task_record_group(record: MemoryEvent | MemoryRecord) -> str | None:
    if getattr(record, "scope", None) != MemoryScope.AGENT:
        return None
    md = record.metadata or {}
    group = md.get("task_memory", {}).get("group_id")
    if group:
        return group
    if md.get("tool_call_id"):
        return md["tool_call_id"]
    for call in md.get("tool_calls") or []:
        if call.get("name") in LEGACY_START | {LEGACY_FINISH}:
            return call.get("id")
    return None


def task_record_metadata(kind: str, group_id: str) -> dict[str, Any]:
    return {"version": 1, "kind": kind, "group_id": group_id}


def render_task_record(kind: str, *, locale: str = "en", **facts: Any) -> str:
    values = dict(
        kind=kind, task_id="", parent_task_id="", title="", description="", outcome="", body=""
    )
    values.update(facts)
    return runtime_prompt("task_memory_record", locale=locale, record=values).strip()


def project_task_history(records: Iterable[_Record], *, locale: str = "en") -> list[_Record]:
    """Project legacy synthetic pairs one-for-one, retaining IDs and source order."""
    records = list(records)
    calls = {}
    finish_groups = {}
    finish_times = {}
    policy_closures = {}
    for record in records:
        if getattr(record, "scope", None) != MemoryScope.AGENT:
            continue
        md = record.metadata or {}
        if md.get("finish_policy") and not md.get("task_memory"):
            key = (md.get("origin_task_id"), md["finish_policy"].get("observe_id"))
            policy_closures.setdefault(key, []).append(record)
        for call in md.get("tool_calls") or []:
            name = call.get("name")
            if name == LEGACY_FINISH or name in LEGACY_START:
                calls[call.get("id")] = (record, call)
                if name == LEGACY_FINISH:
                    finish_groups[md.get("origin_task_id")] = call.get("id")
                    finish_times[md.get("origin_task_id")] = record.timestamp
    # Old task-switch closures used two assistant records without tool calls.
    switch_finishes = set()
    switch_groups = {}
    for key, members in policy_closures.items():
        if len(members) == 2 and all(
            r.role == "assistant" and not r.metadata.get("tool_calls") for r in members
        ):
            closing = max(members, key=lambda r: (r.timestamp, r.metadata.get("seq_no", 0)))
            switch_finishes.add(closing.id)
            switch_groups.update({r.id: key[1] or closing.id for r in members})
    projected = []
    for record in records:
        md = record.metadata or {}
        if md.get("task_memory", {}).get("version") == 1:
            projected.append(record)
            continue
        kind = task_record_kind(record)
        if record.id in switch_finishes:
            kind = "finish"
        elif (
            not kind
            and record.scope == MemoryScope.AGENT
            and record.role == "assistant"
            and not md.get("tool_calls")
            and md.get("origin_task_id") in finish_times
            and record.timestamp
            == finish_times[md.get("origin_task_id")] - timedelta(microseconds=1)
        ):
            kind = "recap"
        paired = (
            calls.get(md.get("tool_call_id"))
            if record.scope == MemoryScope.AGENT and record.role == "tool"
            else None
        )
        if paired:
            kind = "finish" if paired[1]["name"] == LEGACY_FINISH else "result"
        if not kind:
            projected.append(record)
            continue
        call = next(
            (
                c
                for c in md.get("tool_calls") or []
                if c.get("name") in LEGACY_START | {LEGACY_FINISH}
            ),
            {},
        )
        inputs = call.get("input") or call.get("arguments") or {}
        if paired and kind == "result":
            inputs = paired[1].get("input") or paired[1].get("arguments") or {}
        body = record.content or ""
        if call.get("name") == LEGACY_FINISH and inputs.get("result"):
            kind = "final_reply"
            body = body or inputs["result"]
        group = (
            task_record_group(record)
            or switch_groups.get(record.id)
            or finish_groups.get(md.get("origin_task_id"))
            or record.id
        )
        task_id = inputs.get("task_id") or md.get("child_task_id") or md.get("origin_task_id") or ""
        policy = md.get("finish_policy") or (
            paired[0].metadata.get("finish_policy", {}) if paired else {}
        )
        content = render_task_record(
            kind,
            locale=locale,
            task_id=task_id,
            parent_task_id=md.get("parent_task_id") or "",
            title=inputs.get("title") or "",
            description=inputs.get("description") or "",
            outcome=policy.get("outcome", "unknown") if kind == "finish" else "",
            body=body,
        )
        metadata = {k: v for k, v in md.items() if k not in {"tool_calls", "tool_call_id"}}
        metadata["task_memory"] = task_record_metadata(kind, group)
        projected.append(
            replace(
                record,
                content=content,
                metadata=metadata,
                role="assistant" if kind == "final_reply" else "user",
            )
        )
    return projected
