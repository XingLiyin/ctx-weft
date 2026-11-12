"""Legacy synthetic task-call normalization; no current presentation policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta
from typing import Any, TypeVar

from ctx_weft.protocols import MemoryEvent, MemoryKind, MemoryRecord, MemoryScope

_Record = TypeVar("_Record", MemoryEvent, MemoryRecord)

LEGACY_FINISH = "control__finish_task"
LEGACY_START = frozenset({"control__start_task", "control__delegate_task"})


def legacy_task_call_name(call) -> str:
    """Normalize only historical control names; never rewrite business tools."""
    name = str(call.get("name") or "")
    if name in {"start_task", "delegate_task", "finish_task"}:
        return "control__" + name
    return name.replace("control:", "control__", 1) if name.startswith("control:") else name


def task_record_kind(record: MemoryEvent | MemoryRecord) -> str | None:
    tagged = (record.metadata or {}).get("task_memory", {})
    if (
        getattr(record, "scope", None) == MemoryScope.TASK
        and tagged.get("version") == 1
        and tagged.get("kind") == "final_reply"
    ):
        return "final_reply"
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
        if legacy_task_call_name(call) == LEGACY_FINISH:
            return "final_reply" if md.get("final_reply") else "recap"
        if legacy_task_call_name(call) in LEGACY_START:
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
        if legacy_task_call_name(call) in LEGACY_START | {LEGACY_FINISH}:
            return call.get("id")
    return None


def task_record_metadata(kind: str, group_id: str) -> dict[str, Any]:
    return {"version": 1, "kind": kind, "group_id": group_id}


def normalize_legacy_tasks(records: Iterable[_Record]) -> list[_Record]:
    """Return raw body and structured label facts, retaining every source ID."""
    records = list(records)
    if not any(getattr(r, "scope", None) == MemoryScope.AGENT for r in records):
        return records
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
            name = legacy_task_call_name(call)
            if name == LEGACY_FINISH or name in LEGACY_START:
                calls[call.get("id")] = (record, {**call, "name": name})
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
                if legacy_task_call_name(c) in LEGACY_START | {LEGACY_FINISH}
            ),
            {},
        )
        inputs = call.get("input") or call.get("arguments") or {}
        if paired and kind == "result":
            inputs = paired[1].get("input") or paired[1].get("arguments") or {}
        body = record.content or ""
        if legacy_task_call_name(call) == LEGACY_FINISH and inputs.get("result"):
            kind = "final_reply"
            body = body or inputs["result"]
        group = (
            task_record_group(record)
            or switch_groups.get(record.id)
            or finish_groups.get(md.get("origin_task_id"))
            or record.id
        )
        task_id = (
            inputs.get("task_id") or md.get("child_task_id") or ""
            if kind in {"start", "result"}
            else md.get("origin_task_id") or ""
        )
        policy = md.get("finish_policy") or (
            paired[0].metadata.get("finish_policy", {}) if paired else {}
        )
        facts = dict(
            kind=kind,
            task_id=task_id,
            parent_task_id=md.get("parent_task_id") or "",
            title=inputs.get("title") or "",
            description=inputs.get("description") or "",
            outcome=policy.get("outcome", "unknown") if kind == "finish" else "",
            body=body,
        )
        metadata = {k: v for k, v in md.items() if k not in {"tool_calls", "tool_call_id"}}
        metadata["task_memory"] = task_record_metadata(kind, group)
        metadata["legacy_task_record"] = facts
        if kind in {"start", "result"} and task_id:
            metadata["child_task_id"] = task_id
        projected.append(
            replace(
                record,
                content=body,
                metadata=metadata,
                role="assistant" if kind == "final_reply" else "user",
            )
        )
    return projected


def capsule_origin(record) -> str | None:
    """Inherited agent rows belong to the receiving task's capsule."""
    address = getattr(record, "address", None)
    return record.metadata.get("origin_task_id") or (
        address.task_id if address and record.metadata.get("inherited_from_agent_id") else None)
