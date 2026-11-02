"""Shared conversation-record → history block mapping (spec/06 §4.1).

Used by AgentRecallSource (task-layer body + agent-layer AGENT_CONVERSATION_TURN
records) so a memory record renders
identically wherever it is recalled from. Tool fidelity is keyed off role:
assistant→tool_calls, tool→tool_call_id (matches how LLM_RESPONSE/TOOL_RESULT
are ingested).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from datetime import timedelta

from ctx_weft.core.assembler.legacy_task_memory import normalize_legacy_tasks
from ctx_weft.protocols import LLMMessage, MemoryKind, MemoryScope

from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import ContextBlock
    from ctx_weft.protocols import MemoryRecord


def record_to_history_block(
    record: "MemoryRecord",
    source: str,
    idx: int,
) -> "ContextBlock":
    """Wrap one raw MemoryRecord; prompt-specific rendering happens later."""
    from ctx_weft.core.assembler.assembler import ContextBlock

    return ContextBlock(
        id=generate_id("blk"),
        source=source,
        kind="history",
        value=record,
        metadata={"sequence": record.metadata.get("seq_no", idx)},
    )


def history_facts(records, *, task_id="", sequences=None):
    """Describe every candidate and its task relationships, without selecting a view.

    Slot completeness, source validity and ancestry are data facts. Visibility,
    reply wrapping, deduplication and alternatives belong to the history template.
    """
    from ctx_weft.protocols.memory_compat import legacy_type_of

    rows = []
    for index, record in enumerate(normalize_legacy_tasks(records)):
        md = record.metadata or {}
        scope = getattr(record, "scope", None)
        kind = getattr(record, "kind", None)
        if kind is not None and kind not in (MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY):
            continue
        address = getattr(record, "address", None)
        owner = (getattr(address, "session_id", None), getattr(address, "agent_id", None))
        origin = md.get("origin_task_id") or (
            getattr(address, "task_id", None) if md.get("inherited_from_agent_id") else None
        )
        own_task = getattr(address, "task_id", None) if scope == MemoryScope.TASK else origin
        role = getattr(record, "role", None) or "user"
        if scope == MemoryScope.AGENT and kind == MemoryKind.SUMMARY:
            role = "user"
        marker = md.get("task_memory", {})
        timestamp = getattr(record, "timestamp", None)
        row = dict(
            record=record,
            record_id=record.id,
            ref=f"history:{index}",
            owner=owner,
            scope=scope,
            task_key=(*owner, own_task),
            task_id=md.get("task_id", ""),
            origin_task_id=origin or "",
            parent_id=md.get("parent_task_id"),
            child_id=md.get("child_task_id"),
            kind=marker.get("kind"),
            marker=marker,
            policy=md.get("finish_policy", {}),
            legacy=md.get("legacy_task_record"),
            group=("task-memory:" + str(marker["group_id"]) if marker.get("group_id") else None),
            timestamp=timestamp,
            sequence=(sequences or {}).get(record.id, md.get("seq_no", index)),
            message=LLMMessage(
                role=role,
                content=record.content,
                tool_calls=md.get("tool_calls", []) if role == "assistant" else [],
                tool_call_id=md.get("tool_call_id") if role == "tool" else None,
            ),
        )
        row["memory_type"] = str(getattr(record, "type", None) or legacy_type_of(kind, scope, role))
        row["summary_kind"] = (
            (
                "user"
                if role == "user"
                else "assistant_current"
                if md.get("task_id") == task_id
                else "assistant"
            )
            if kind == MemoryKind.SUMMARY
            else None
        )
        rows.append(row)

    def order(row):
        return (row["timestamp"].isoformat() if row["timestamp"] else "", row["sequence"])

    rows.sort(key=order)
    tasks = {}
    for row in rows:
        if (
            row["scope"] != MemoryScope.AGENT
            or row["kind"] != "finish"
            or not row["origin_task_id"]
        ):
            continue
        group_members = [
            r
            for r in rows
            if r["owner"] == row["owner"]
            and r["origin_task_id"] == row["origin_task_id"]
            and r["marker"].get("group_id") == row["marker"].get("group_id")
        ]
        policy = row["policy"]
        if policy.get("capsule_state") != "lean" and len(group_members) < policy.get(
            "slot_count", 1
        ):
            continue
        key = (*row["owner"], row["origin_task_id"])
        tasks[key] = dict(
            key=key,
            id=row["origin_task_id"],
            parent_id=row["parent_id"],
            closing=row,
            policy=policy,
            tree_key=key,
        )

    def prose(row):
        md = row["record"].metadata
        return (
            row["scope"] == MemoryScope.TASK
            and row["message"].role == "assistant"
            and isinstance(row["message"].content, str)
            and bool(row["message"].content)
            and not any(
                md.get(k) for k in ("tool_calls", "tool_call_count", "interrupted", "cancelled")
            )
        )

    for key, task in tasks.items():
        members = [r for r in rows if r["task_key"] == key]
        task["members"] = members
        task["body"] = [r for r in members if r["scope"] == MemoryScope.TASK]
        task["anchors"] = [r for r in members if r["kind"] == "final_reply"]
        task["source"] = next(
            (
                r
                for r in task["body"]
                if r["record_id"] == task["policy"].get("final_reply_record_id") and prose(r)
            ),
            None,
        )
        raw = [
            r
            for r in task["body"]
            if r["message"].role == "assistant" and order(r) <= order(task["closing"])
        ]
        task["last_prose"] = raw[-1] if raw and prose(raw[-1]) else None
        seen = {key}
        parent = (*key[:2], task["parent_id"])
        while parent in tasks and parent not in seen:
            seen.add(parent)
            task["tree_key"] = parent
            parent = (*key[:2], tasks[parent]["parent_id"])

    trees = {}
    for task in tasks.values():
        top = task["tree_key"]
        tree = trees.setdefault(
            top,
            dict(
                key=top,
                root=tasks[top],
                tasks=[],
                group="capsule:" + ":".join(str(part) for part in top),
            ),
        )
        tree["tasks"].append(task)
    for row in rows:
        task = tasks.get(row["task_key"])
        row["task"] = task
        row["tree"] = trees.get(task["tree_key"]) if task else None
        # Old synthetic results were stored at start. Supply their known logical
        # completion position as a fact, preserving persisted timestamps.
        child = tasks.get((*row["owner"], row["child_id"])) if row["kind"] == "result" else None
        if child and row["timestamp"] <= child["closing"]["timestamp"]:
            row["timestamp"] = child["closing"]["timestamp"] + timedelta(microseconds=1)
        row["current_task"] = row["task_id"] == task_id or row["origin_task_id"] == task_id
        row["current_prompt"] = row["memory_type"] == "user_prompt" and row["task_id"] == task_id
        row["priority"] = (
            0
            if row["current_prompt"]
            else 4
            if row["current_task"]
            else 2
            if row["memory_type"] == "agent_compact_summary"
            else 5
            if row["memory_type"] == "agent_conversation_turn"
            else 6
        )
    rows.sort(key=order)
    return rows, dict(task_history=tasks, history_trees=list(trees.values()))
