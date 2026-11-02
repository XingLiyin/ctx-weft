"""Task-memory facts and presentation; persisted records are never changed on read."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, TypeVar

from ctx_weft.core.assembler.prompts import runtime_prompt
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


def capsule_origin(record) -> str | None:
    """Inherited snapshot rows belong to the receiving task's capsule."""
    address = getattr(record, "address", None)
    return record.metadata.get("origin_task_id") or (
        address.task_id if address and record.metadata.get("inherited_from_agent_id") else None
    )


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
        if kind in {"start", "result"} and task_id:
            metadata["child_task_id"] = task_id
        projected.append(
            replace(
                record,
                content=content,
                metadata=metadata,
                role="assistant" if kind == "final_reply" else "user",
            )
        )
    return projected


@dataclass(frozen=True)
class TaskCapsule:
    """Mutually exclusive prompt representations of one closed task subtree."""

    group: str
    full_ids: frozenset[str]
    lean: tuple[MemoryEvent | MemoryRecord, ...]


def _owner(record):
    address = record.address
    return (address.session_id, address.agent_id) if address else (None, None)


def _order(record):
    return (
        record.timestamp.isoformat() if record.timestamp else "",
        record.metadata.get("seq_no", 0),
    )


def final_reply_source(records, outputs: str) -> str | None:
    """Locate the actual retained actor answer, including the old body+summary form."""
    if not outputs.strip():
        return None
    candidates = []
    for record in records:
        md = record.metadata
        if (
            record.scope != MemoryScope.TASK
            or record.role != "assistant"
            or md.get("tool_calls")
            or md.get("tool_call_count")
            or md.get("interrupted")
            or md.get("cancelled")
            or not isinstance(record.content, str)
        ):
            continue
        body = record.content.strip()
        if body and (outputs.strip() == body or outputs.strip().startswith(body + "\n\n")):
            candidates.append(record)
    return max(candidates, key=_order).id if candidates else None


def capsule_history(records: Iterable[_Record]) -> tuple[list[_Record], list[TaskCapsule]]:
    """Select full or lean history without changing persisted rows or IDs.

    A finish is a durable compression source. While a task has its body or final
    answer, that source is hidden. An atomic prompt group also supplies the lean
    alternative, so budget trimming cannot strand a recap without its answer.
    """
    records = list(records)
    if not any(getattr(r, "scope", None) == MemoryScope.AGENT for r in records):
        return records, []
    projected = project_task_history(records)
    by_id = {r.id: r for r in projected}
    closures = {}
    for record in sorted(projected, key=_order):
        if task_record_kind(record) != "finish":
            continue
        md = record.metadata
        if not md.get("origin_task_id"):
            continue
        policy = md.get("finish_policy", {})
        group = task_record_group(record)
        members = [
            r
            for r in projected
            if _owner(r) == _owner(record)
            and r.metadata.get("origin_task_id") == md["origin_task_id"]
            and task_record_group(r) == group
        ]
        if policy.get("capsule_state") != "lean" and len(members) < policy.get("slot_count", 1):
            continue  # An incomplete close is not a selectable capsule.
        closures[(*_owner(record), md.get("origin_task_id"))] = record

    # Historical terminal results share the dispatch-start timestamp because
    # they used to be synthetic tool results. Labels have no adjacency rule:
    # project a known child's result after its closure without rewriting storage.
    for record in projected:
        if task_record_kind(record) != "result":
            continue
        child = record.metadata.get("child_task_id")
        closing = closures.get((*_owner(record), child)) if child else None
        if closing is not None and record.timestamp <= closing.timestamp:
            by_id[record.id] = replace(
                record, timestamp=closing.timestamp + timedelta(microseconds=1)
            )

    suppressed = set()
    full_tasks = set()
    task_members = {}
    for key, closing in closures.items():
        owner, task_id = key[:2], key[2]
        members = [
            r
            for r in projected
            if _owner(r) == owner
            and (
                r.scope == MemoryScope.TASK
                and r.address.task_id == task_id
                or r.scope == MemoryScope.AGENT
                and capsule_origin(r) == task_id
            )
        ]
        task_members[key] = members
        policy = closing.metadata.get("finish_policy", {})
        if policy.get("has_summary") is False:
            suppressed.update(r.id for r in members if task_record_kind(r) == "recap")
        if policy.get("capsule_state") == "lean":
            suppressed.update(r.id for r in members if r.id != closing.id)
            continue
        anchors = [r for r in members if task_record_kind(r) == "final_reply"]
        source_id = policy.get("final_reply_record_id")
        source = by_id.get(source_id)
        if source is not None and not (
            source.scope == MemoryScope.TASK
            and _owner(source) == owner
            and source.address.task_id == task_id
            and source.role == "assistant"
            and isinstance(source.content, str)
            and not source.metadata.get("tool_calls")
            and not source.metadata.get("tool_call_count")
            and not source.metadata.get("interrupted")
            and not source.metadata.get("cancelled")
        ):
            source = None
        if not anchors and source is None:
            # Older short capsules retained the final prose turn as ordinary raw.
            raw = [
                r
                for r in members
                if r.scope == MemoryScope.TASK
                and r.role == "assistant"
                and _order(r) <= _order(closing)
            ]
            candidate = max(raw, key=_order) if raw else None
            if (
                candidate is not None
                and isinstance(candidate.content, str)
                and candidate.content
                and not candidate.metadata.get("tool_calls")
                and not candidate.metadata.get("tool_call_count")
                and not candidate.metadata.get("interrupted")
                and not candidate.metadata.get("cancelled")
                and policy.get("outcome") != "cancelled"
            ):
                source = candidate
        if anchors and source is not None:
            suppressed.add(source.id)  # Close may have persisted its anchor before raw GC.
        elif not anchors and source is not None:
            updated = replace(
                source,
                content=render_task_record(
                    "final_reply",
                    task_id=task_id,
                    parent_task_id=closing.metadata.get("parent_task_id") or "",
                    outcome=policy.get("outcome", ""),
                    body=source.content,
                ),
                metadata={
                    **source.metadata,
                    "task_memory": task_record_metadata("final_reply", task_record_group(closing)),
                    "final_reply": True,
                },
            )
            by_id[source.id] = updated
        if anchors or source is not None or any(r.scope == MemoryScope.TASK for r in members):
            full_tasks.add(key)
            suppressed.add(closing.id)
        else:
            suppressed.update(r.id for r in members if r.id != closing.id)

    def root(key):
        seen = set()
        while key not in seen:
            seen.add(key)
            parent = closures[key].metadata.get("parent_task_id")
            parent_key = (*key[:2], parent)
            if parent_key not in closures:
                break
            key = parent_key
        return key

    trees = {}
    for key in closures:
        trees.setdefault(root(key), []).append(key)
    capsules = []
    for top, descendants in trees.items():
        closing = closures[top]
        ids = {r.id for key in descendants for r in task_members[key]}
        if not any(key in full_tasks for key in descendants):
            # The entire subtree is already a single minimal task capsule.
            suppressed.update(ids - {closing.id})
            suppressed.discard(closing.id)
            continue
        full_ids = frozenset(ids - suppressed)
        capsules.append(
            TaskCapsule(
                group="capsule:" + ":".join(str(part) for part in top),
                full_ids=full_ids,
                lean=()
                if closing.metadata.get("finish_policy", {}).get("has_summary") is False
                else (closing,),
            )
        )
    return [by_id[r.id] for r in projected if r.id not in suppressed], capsules
