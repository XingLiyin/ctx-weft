from datetime import UTC, datetime

import pytest
from types import SimpleNamespace
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.budget import PriorityBudgetStrategy
from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.core.errors import ContextOverflowError
from ctx_weft.protocols import ImagePart, MemoryEventType, MemoryRecord, TextPart


def _req(task_id="cur"):
    return SimpleNamespace(
        task=SimpleNamespace(id=task_id),
        session=SimpleNamespace(context_limit=180_000, reserved_output_tokens=8192),
    )


def _blk(bid, priority, tokens, *, ts="", role="user", mtype="user_prompt",
         task_id="", origin_task_id="", tool_calls=None, tool_call_id=""):
    md = {"timestamp": ts, "role": role, "type": mtype, "task_id": task_id}
    if origin_task_id:
        md["origin_task_id"] = origin_task_id
    if tool_calls is not None:
        md["tool_calls"] = tool_calls
    if tool_call_id:
        md["tool_call_id"] = tool_call_id
    return ContextBlock(id=bid, source="agent_recall", kind="history", target="messages",
                        content="x", priority=priority, token_estimate=tokens, metadata=md)


@pytest.mark.asyncio
async def test_drops_oldest_completed_first():
    """完成 task 层胶囊(6) 同档超预算：丢最老。"""
    old = _blk("old", 6, 100, ts="2026-01-01T00:00:00", task_id="p1",
               mtype="llm_response", role="assistant")
    new = _blk("new", 6, 100, ts="2026-01-02T00:00:00", task_id="p2",
               mtype="llm_response", role="assistant")
    kept = await PriorityBudgetStrategy().apply([old, new], token_limit=100, request=_req())
    assert {b.id for b in kept} == {"new"}


@pytest.mark.asyncio
async def test_current_task_more_protected_than_completed():
    """当前 task 内容(提级 4) 比已完成 task(6) 更保：即便更老也留。"""
    done = _blk("done", 6, 100, ts="2026-01-03T00:00:00", task_id="past",
                mtype="llm_response", role="assistant")           # 更新但已完成 → eff 6
    cur = _blk("cur", 6, 100, ts="2026-01-01T00:00:00", task_id="cur",
               mtype="llm_response", role="assistant")            # 更老但当前 → 提级 eff 4
    kept = await PriorityBudgetStrategy().apply([done, cur], token_limit=100, request=_req("cur"))
    assert {b.id for b in kept} == {"cur"}


@pytest.mark.asyncio
async def test_completed_task_layer_dropped_before_agent_layer():
    """完成 task 层胶囊(6) 先于完成 task 的 agent 层回合(5) 丢，尽管 agent 回合更老。"""
    tl = _blk("tl", 6, 100, ts="2026-01-02T00:00:00", task_id="past",
              mtype="tool_result", role="tool")                    # task 层
    al = _blk("al", 5, 100, ts="2026-01-01T00:00:00", origin_task_id="past",
              mtype="agent_conversation_turn", role="assistant")   # agent 层，更老
    kept = await PriorityBudgetStrategy().apply([tl, al], token_limit=100, request=_req("cur"))
    assert {b.id for b in kept} == {"al"}


@pytest.mark.asyncio
async def test_agent_summary_protected_over_raw():
    """AGENT_COMPACT_SUMMARY(2) 比 raw(6) 更保：raw 先丢。"""
    summ = _blk("s", 2, 100, ts="2026-01-01T00:00:00", mtype="agent_compact_summary", role="user")
    raw = _blk("r", 6, 100, ts="2026-01-02T00:00:00", mtype="llm_response",
               role="assistant", task_id="past")
    kept = await PriorityBudgetStrategy().apply([summ, raw], token_limit=100, request=_req())
    assert {b.id for b in kept} == {"s"}


@pytest.mark.asyncio
async def test_tool_pair_dropped_atomically():
    """丢含 tool_call 的 assistant → 其 tool_result 同批丢，无 orphan/dangling。"""
    call = _blk("call", 6, 100, ts="2026-01-01T00:00:00", role="assistant",
                mtype="llm_response", task_id="past", tool_calls=[{"id": "A"}])
    result = _blk("res", 6, 100, ts="2026-01-01T00:00:01", role="tool",
                  mtype="tool_result", task_id="past", tool_call_id="A")
    newer = _blk("keep", 6, 50, ts="2026-01-03T00:00:00", task_id="p2",
                 mtype="llm_response", role="assistant")
    kept = await PriorityBudgetStrategy().apply([call, result, newer], token_limit=50, request=_req())
    assert {b.id for b in kept} == {"keep"}  # call 与 res 同生共死，不留半对


@pytest.mark.asyncio
async def test_current_message_pinned():
    """task_id == request.task.id 的 user_prompt 即便最老也不丢（pin→0）。"""
    cur = _blk("cur", 6, 100, ts="2026-01-01T00:00:00", mtype="user_prompt", task_id="cur")
    other = _blk("oth", 6, 100, ts="2026-01-02T00:00:00", mtype="user_prompt", task_id="past")
    kept = await PriorityBudgetStrategy().apply([cur, other], token_limit=100, request=_req("cur"))
    assert "cur" in {b.id for b in kept}
    assert "oth" not in {b.id for b in kept}


@pytest.mark.asyncio
async def test_overflow_when_floor_exceeds_limit():
    """仅 priority-0（含 pin）超 effective_limit → 抛富信息 ContextOverflowError。"""
    floor = ContextBlock(id="soul", source="identity", kind="identity", target="system",
                         content="x", priority=0, token_estimate=200, metadata={})
    with pytest.raises(ContextOverflowError) as ei:
        await PriorityBudgetStrategy().apply([floor], token_limit=100, request=_req("cur"))
    assert ei.value.required >= 200
    assert ei.value.effective_limit == 100
    assert ei.value.context_limit == 180_000
    assert ei.value.reserved_output_tokens == 8192


@pytest.mark.asyncio
async def test_overflow_reports_image_count_from_pinned_content():
    """priority-0（pin 住的当前消息）里含图片时，ContextOverflowError.image_count
    须精确等于其中 ImagePart 的个数（不靠 image_tokens 整除反推）。"""
    content = [TextPart(text="hi"), ImagePart(data="ZGF0YQ==", media_type="image/png"),
               ImagePart(data="ZGF0YQ==", media_type="image/png")]
    pinned = ContextBlock(
        id="cur", source="agent_recall", kind="history", target="messages",
        content=content, priority=6, token_estimate=200_000,
        metadata={"timestamp": "", "role": "user", "type": "user_prompt", "task_id": "cur"},
    )
    with pytest.raises(ContextOverflowError) as ei:
        await PriorityBudgetStrategy().apply([pinned], token_limit=100, request=_req("cur"))
    assert ei.value.image_count == 2


@pytest.mark.asyncio
async def test_overflow_image_count_from_real_assembled_history_block():
    """Phase 0 遗留义务 2（spec §6.5）：budget.py:89 的 image_part_count(b.content) 曾因
    ContextBlock.content 恒为 _history.py 拍扁后的字符串而按构造恒 0。这里不手工构造
    ContextBlock——而是用 record_to_history_block（Task 2 之后的真实装配产物）喂给
    PriorityBudgetStrategy.apply，证明装配链真的把 parts 送到了 budget，而不只是
    budget 自己会数。"""
    record = MemoryRecord(
        id="mem_pin",
        type=MemoryEventType.USER_PROMPT,
        content=[
            TextPart(text="hi"),
            ImagePart(data="ZGF0YQ==", media_type="image/png"),
            ImagePart(data="ZGF0YQ==", media_type="image/png"),
            ImagePart(data="ZGF0YQ==", media_type="image/png"),
        ],
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        role="user",
        metadata={"task_id": "cur"},
    )
    req_for_block = SimpleNamespace(task=SimpleNamespace(id="cur"), token_counter=len)
    blk = record_to_history_block(
        record, "agent_recall", 0, request=req_for_block, current_task_id="cur",
    )
    # 未手工构造：content 直接来自 record_to_history_block 的真实产出。
    assert isinstance(blk.content, list), "本条断言的前提是装配产出的是 part 列表，而非拍扁字符串"

    with pytest.raises(ContextOverflowError) as ei:
        await PriorityBudgetStrategy().apply([blk], token_limit=1, request=_req("cur"))
    assert ei.value.image_count == 3
