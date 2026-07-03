"""Task 7：各 Source 改调 slot_priority() + _history 补 origin_task_id。

真实断言（非 test_slot_priority.py 的重复）：
1. record_to_history_block() 返回的 block.priority 确实等于 slot_priority("history", mem_type)
   （而不是硬编码常量）——用两种代表性 record.type 验证。
2. record_to_history_block() 的 block.metadata["origin_task_id"] 确实从
   record.metadata["origin_task_id"] 透传（budget 层据此判 agent 层回合归属哪个 task）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextRequest
from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.core.assembler.sources.blackboard import BlackboardSource
from ctx_weft.core.state.models import Agent, Session, Task
from ctx_weft.providers.memory_blackboard.in_memory import InMemoryMemoryProvider
from ctx_weft.protocols import (
    MemoryEvent,
    MemoryEventType,
    MemoryRecord,
    MemoryScope,
    ProviderContext,
)

T = MemoryEventType


def _ctx() -> ProviderContext:
    return ProviderContext(session_id="s1", tenant_id="default")


def _request_for(task_id: str, m: InMemoryMemoryProvider) -> tuple[ContextRequest, AssemblerDeps]:
    task = Task(id=task_id, session_id="s1", status="ACTIVE")
    agent = Agent(id="a", session_id="s1", template_id="t", template_version="1", status="IDLE")
    session = Session(id="s1", user_prompt="go", status="RUNNING")
    req = ContextRequest(
        purpose="observe", scope=MemoryScope(session_id="s1", task_id=task_id, agent_id="a"),
        task=task, agent=agent, session=session, template=None, bound_capabilities=[],
    )
    deps = AssemblerDeps(memory=m, knowledge_providers=[], provider_ctx=_ctx())
    return req, deps


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


async def test_blackboard_source_tiers_long_term_background_at_priority_1() -> None:
    # 回归：blackboard.py 曾对所有 yield 硬编码 slot_priority("blackboard")（=3），
    # 导致 long_term_background（kind="background"，进系统提示）被降级保护。
    # 现在改传实际 kind：background→1，其余 blackboard→3。
    m = InMemoryMemoryProvider()
    await m.ingest(
        MemoryEvent(
            type=MemoryEventType.BLACKBOARD_PUBLISH,
            scope=MemoryScope(session_id="s1", task_id="G", agent_id="a"),
            content="project background info",
            timestamp=datetime.now(timezone.utc),
            topic="G",
            metadata={"title": "Background", "outcome": "success"},
        ),
        _ctx(),
    )
    await m.subscribe_topic("s1", topic="G", intent="long_term_background", ctx=_ctx(), task_id="")
    await m.subscribe_topic("s1", topic="A", intent="subtask", ctx=_ctx(), task_id="B")
    await m.ingest(
        MemoryEvent(
            type=MemoryEventType.BLACKBOARD_PUBLISH,
            scope=MemoryScope(session_id="s1", task_id="A", agent_id="a"),
            content="subtask result",
            timestamp=datetime.now(timezone.utc),
            topic="A",
            metadata={"title": "Subtask", "outcome": "success"},
        ),
        _ctx(),
    )

    req, deps = _request_for("B", m)
    blocks = {blk.kind: blk for blk in [b async for b in BlackboardSource().fetch(req, deps)]}

    assert blocks["background"].priority == 1 == slot_priority("background")
    assert blocks["blackboard"].priority == 3 == slot_priority("blackboard")


def test_tier_mapping_contract():
    # 锁定 slot_priority 语义（Task 2 已实现）；下面 grep 步骤确保 source 确实改调它。
    assert slot_priority("history", "agent_compact_summary") == 2
    assert slot_priority("history", "agent_conversation_turn") == 5
    assert slot_priority("history", "llm_response") == 6
    assert slot_priority("history", "task_compact_summary") == 6
    assert slot_priority("capabilities") == 1
    assert slot_priority("background") == 1
    assert slot_priority("blackboard") == 3
    assert slot_priority("reference") == 7
    assert slot_priority("summary") == 7
    assert slot_priority("identity") == 0
    assert slot_priority("task_spec") == 0
