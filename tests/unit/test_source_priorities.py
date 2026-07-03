"""Task 7：各 Source 改调 slot_priority() + _history 补 origin_task_id。

真实断言（非 test_slot_priority.py 的重复）：
1. record_to_history_block() 返回的 block.priority 确实等于 slot_priority("history", mem_type)
   （而不是硬编码常量）——用两种代表性 record.type 验证。
2. record_to_history_block() 的 block.metadata["origin_task_id"] 确实从
   record.metadata["origin_task_id"] 透传（budget 层据此判 agent 层回合归属哪个 task）。
"""
from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import MemoryEventType, MemoryRecord

T = MemoryEventType


def _rec(type_, content, role="user", metadata=None):
    return MemoryRecord(
        id="m1",
        type=type_,
        content=content,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        role=role,
        topic=None,
        metadata=metadata if metadata is not None else {"seq_no": 1},
    )


def test_history_block_priority_wired_to_slot_priority_llm_response():
    rec = _rec(T.LLM_RESPONSE, "hello", role="assistant")
    blk = record_to_history_block(rec, "task_conversation", 0)
    assert blk.priority == slot_priority("history", str(rec.type))
    assert blk.priority == 6


def test_history_block_priority_wired_to_slot_priority_agent_compact_summary():
    rec = _rec(T.AGENT_COMPACT_SUMMARY, "summary text", role="assistant")
    blk = record_to_history_block(rec, "agent_recall", 0)
    assert blk.priority == slot_priority("history", str(rec.type))
    assert blk.priority == 2


def test_history_block_carries_origin_task_id_from_record_metadata():
    rec = _rec(
        T.USER_PROMPT,
        "do the thing",
        role="user",
        metadata={"seq_no": 1, "task_id": "T1", "origin_task_id": "T9"},
    )
    blk = record_to_history_block(rec, "task_conversation", 0)
    assert blk.metadata["origin_task_id"] == "T9"


def test_history_block_origin_task_id_defaults_empty_when_absent():
    rec = _rec(T.USER_PROMPT, "do the thing", role="user", metadata={"seq_no": 1})
    blk = record_to_history_block(rec, "task_conversation", 0)
    assert blk.metadata["origin_task_id"] == ""


def test_tier_mapping_contract():
    # 锁定 slot_priority 语义（Task 2 已实现）；下面 grep 步骤确保 source 确实改调它。
    assert slot_priority("history", "agent_compact_summary") == 2
    assert slot_priority("history", "agent_conversation_turn") == 5
    assert slot_priority("history", "llm_response") == 6
    assert slot_priority("history", "task_compact_summary") == 6
    assert slot_priority("capabilities") == 1
    assert slot_priority("blackboard") == 3
    assert slot_priority("reference") == 7
    assert slot_priority("summary") == 7
    assert slot_priority("identity") == 0
    assert slot_priority("task_spec") == 0
