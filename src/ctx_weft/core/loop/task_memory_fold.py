"""任务执行记录的 memory 折叠与收尾摘要交接。

build_finish_slots / replace_finish_report 构造或替换任务收尾记录。

Finalize 持久化 finish 对和原文保留策略，Observe 只替换完整记录。
进程内通知仅用于唤醒，报告交接不依赖内存槽。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ctx_weft.core.assembler.prompts import runtime_prompt
from ctx_weft.core.media import placeholder_refs
from ctx_weft.core.assembler.legacy_task_memory import (
    capsule_origin as capsule_origin,
)
from ctx_weft.core.assembler.legacy_task_memory import (
    legacy_task_call_name,
    normalize_legacy_tasks,
)
from ctx_weft.core.assembler.legacy_task_memory import (
    task_record_group as legacy_record_group,
)
from ctx_weft.core.assembler.legacy_task_memory import (
    task_record_kind as legacy_record_kind,
)
from ctx_weft.protocols import (
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    load_records,
)
from ctx_weft.protocols.capability import qualify

logger = logging.getLogger(__name__)

_VIEW_KINDS = [MemoryKind.CONVERSATION_TURN, MemoryKind.SUMMARY, MemoryKind.TOOL_AUDIT]


def task_record_kind(record):
    """Read current task markers, accepting historical synthetic records."""
    marker = record.metadata.get("task_memory", {})
    if marker.get("version") == 1:
        scope = getattr(record, "scope", None)
        if scope == MemoryScope.TASK:
            return "final_reply" if marker.get("kind") == "final_reply" else None
        if scope == MemoryScope.AGENT and record.kind == MemoryKind.CONVERSATION_TURN:
            return marker.get("kind")
        return None
    return legacy_record_kind(record)


def task_record_group(record):
    if getattr(record, "scope", None) != MemoryScope.AGENT:
        return None
    marker = record.metadata.get("task_memory", {})
    return marker.get("group_id") or legacy_record_group(record)


def task_record_metadata(kind: str, group_id: str):
    return {"version": 1, "kind": kind, "group_id": group_id}


def build_finish_slots(*, scope, task_id: str, parent_task_id, title: str, outcome: str,
                       act_recap: str, task_summary: str, final_reply: str,
                       base, tool_call_id: str, switched: bool = False,
                       lean: bool = False) -> list[MemoryEvent]:
    """Write ordinary, Jinja-rendered task records with stable closure metadata."""
    parts = [] if lean else [("recap", act_recap)]
    if final_reply.strip() and not lean:
        parts.append(("final_reply", final_reply.strip()))
    parts.append(("finish", task_summary or act_recap))
    events = []
    for index, (kind, body) in enumerate(parts):
        content = runtime_prompt("task_memory_record", record=dict(
            kind=kind, task_id=task_id, parent_task_id=parent_task_id or "",
            title=title, description="", outcome=outcome if kind in {"finish", "final_reply"} else "", body=body)).strip()
        events.append(MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
            content=content, timestamp=base + timedelta(microseconds=index),
            role="assistant" if kind == "final_reply" else "user",
            metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id,
                      "task_memory": task_record_metadata(kind, tool_call_id),
                      **({"final_reply": True} if kind == "final_reply" else {})},
            blob_refs=placeholder_refs(content),
        ))
    return events


async def find_finish_target(memory, provider_ctx, scope, task_id, *, legacy_outcome="success"):
    """Read a complete persisted finish pair; partial writes are not ready."""
    from ctx_weft.protocols import MemoryKind, MemoryScope
    turns = await load_records(memory,
        MemoryAddress(session_id=scope.session_id, agent_id=scope.agent_id),
        MemoryScope.AGENT, provider_ctx, kinds=[MemoryKind.CONVERSATION_TURN])
    own = [r for r in turns if r.metadata.get("origin_task_id") == task_id]
    for record in reversed(own):
        if task_record_kind(record) != "finish" or not record.metadata.get("task_memory"):
            continue
        group = task_record_group(record)
        members = [r for r in own if task_record_group(r) == group]
        policy = record.metadata.get("finish_policy") or {
            "preserve_raw": True, "outcome": legacy_outcome, "slot_count": 2}
        if len(members) >= policy["slot_count"]:
            return record.address, group, dict(policy)
    for assistant in reversed(own):
        if assistant.role != "assistant":
            continue
        for call in assistant.metadata.get("tool_calls") or []:
            if legacy_task_call_name(call) != qualify("control:finish_task"):
                continue
            tools = [r for r in own if r.role == "tool" and
                     r.metadata.get("tool_call_id") == call.get("id")]
            if not tools:
                continue
            policy = assistant.metadata.get("finish_policy")
            # Historical pairs have no policy: retain raw rather than invent a fold.
            if policy is None:
                policy = {"preserve_raw": True, "outcome": legacy_outcome, "slot_count": 2}
            if len(own) < policy["slot_count"]:
                continue
            return assistant.address, call["id"], dict(policy)
    return None


async def replace_finish_report(memory, provider_ctx, scope, task_id: str,
                                 tool_call_id: str, act_recap: str, task_summary: str,
                                 outcome: str, title: str, *, final_reply: str = "", operation_id: str = "") -> None:
    """Atomically replace a complete legacy or tagged closure with labeled records.

    Preserve an existing delivered answer if the new report supplies none. Stable
    operation IDs make repeated recovery applications idempotent."""
    from datetime import timedelta

    from ctx_weft.protocols import MemoryEvent, MemoryKind, MemoryScope

    turns = await load_records(memory,
        MemoryAddress(session_id=scope.session_id, agent_id=scope.agent_id),
        MemoryScope.AGENT, provider_ctx, kinds=[MemoryKind.CONVERSATION_TURN],
    )
    tagged = [r for r in turns if r.metadata.get("origin_task_id") == task_id
              and r.metadata.get("task_memory") and task_record_group(r) == tool_call_id]
    asst = [r for r in turns
            if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
            and any(tc.get("id") == tool_call_id for tc in (r.metadata.get("tool_calls") or []))]
    tool = [r for r in turns
            if r.role == "tool" and r.metadata.get("origin_task_id") == task_id
            and r.metadata.get("tool_call_id") == tool_call_id]
    recap_slot = [r for r in turns
                  if r.role == "assistant" and r.metadata.get("origin_task_id") == task_id
                  and not r.metadata.get("tool_calls") and not r.metadata.get("task_memory")]
    if tagged:
        asst = [r for r in tagged if task_record_kind(r) in {"recap", "final_reply"}]
        tool = [r for r in tagged if task_record_kind(r) == "finish"]
        recap_slot = []
    lean = bool(tool and tool[0].metadata.get("finish_policy", {}).get("capsule_state") == "lean")
    if (not asst and not lean) or not tool:
        raise RuntimeError(f"Incomplete finish pair for task={task_id} call={tool_call_id}")

    first = (recap_slot or asst or tool)[0]
    ts = first.timestamp
    parent_task_id = first.metadata.get("parent_task_id")
    # 已是三槽 → 沿用已有锚点正文（它已含前后两条注解，直接透传会被再包一层）。
    existing_anchor = next((r for r in asst if r.metadata.get("final_reply")), None)

    # 槽位形态与 close 合成同源（`build_finish_slots`）：答复在则三槽、不在则两槽。
    # v2 P3d：占位遗忘 + 新槽写入一次原子 fold（关旧「占位已删而真报告未写」窗口）。
    events = build_finish_slots(
        scope=scope, task_id=task_id, parent_task_id=parent_task_id, title=title,
        outcome=outcome, act_recap=act_recap, task_summary=task_summary,
        final_reply=final_reply, base=ts, tool_call_id=tool_call_id,
        lean=lean,
    )
    if existing_anchor is not None and not (final_reply or "").strip() and not lean:
        # 占位已是三槽而本次没带答复：锚点正文原样保留，只换 recap 与报告两槽。
        events = [e for e in events if not e.metadata.get("final_reply")]
        events.insert(1, MemoryEvent(
            kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.AGENT, address=scope,
            content=(runtime_prompt("task_memory_record", record=normalize_legacy_tasks([existing_anchor])[0].metadata["legacy_task_record"]).strip()
                     if not existing_anchor.metadata.get("task_memory") else existing_anchor.content),
            timestamp=ts + timedelta(microseconds=1),
            blob_refs=list(dict.fromkeys([*existing_anchor.blob_refs,
                                          *placeholder_refs(existing_anchor.content)])),
            role="assistant",
            metadata={"origin_task_id": task_id, "parent_task_id": parent_task_id,
                      "task_memory": task_record_metadata("final_reply", tool_call_id),
                      "final_reply": True},
        ))
    policy = first.metadata.get("finish_policy")
    if policy is not None:
        for event in events:
            event.metadata["finish_policy"] = {
                **policy, "slot_count": len(events), "outcome": outcome,
                "has_summary": bool((task_summary or act_recap).strip()),
            }
    if operation_id:
        from ctx_weft.core.utils.ids import record_id
        for index, event in enumerate(events):
            event.id = record_id(operation_id, "replace_finish_v1", scope, task_id, index)
        if any(r.id == events[0].id for r in (*recap_slot, *asst, *tool)):
            return
    await memory.fold([r.id for r in (*recap_slot, *asst, *tool)], events, provider_ctx)
