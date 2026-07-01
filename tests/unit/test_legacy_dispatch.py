"""§5.5 集中适配层 normalize_legacy_dispatch：存量 legacy TASK_DISPATCH/RESULT 读侧归一化。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ctx_weft.core.loop.steps.legacy_dispatch import normalize_legacy_dispatch
from ctx_weft.protocols import MemoryEventType, MemoryRecord

T = MemoryEventType
_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _rec(type_, content, t, role=None, **meta) -> MemoryRecord:
    return MemoryRecord(id=f"mev_{t}", type=type_, content=content,
                        timestamp=_BASE + timedelta(seconds=t), role=role, metadata=meta)


def test_paired_legacy_dispatch_becomes_conversation_turns() -> None:
    """配对的 legacy TASK_DISPATCH + RESULT → delegate assistant 回合 + result tool 回合。"""
    records = [
        _rec(T.TASK_DISPATCH, "", 0, role="assistant", tool_call_id="oc1",
             tool_name="control:delegate_task", arguments={"title": "x"}, task_id="D"),
        _rec(T.TASK_DISPATCH_RESULT, "child result", 1, role="tool",
             tool_call_id="oc1", parent_task_id="D", task_id="D"),
    ]
    out = normalize_legacy_dispatch(records)

    assert all(r.type == T.AGENT_CONVERSATION_TURN for r in out)
    delegate = next(r for r in out if r.role == "assistant")
    assert delegate.content == ""
    assert delegate.metadata["origin_task_id"] == "D"
    assert delegate.metadata["tool_calls"] == [
        {"id": "oc1", "name": "control:delegate_task", "input": {"title": "x"}}
    ]
    result = next(r for r in out if r.role == "tool")
    assert result.content == "child result"
    assert result.metadata["origin_task_id"] == "D"
    assert result.metadata["tool_call_id"] == "oc1"


def test_unpaired_legacy_dispatch_is_hidden() -> None:
    """无对应 RESULT 的孤立 legacy TASK_DISPATCH 隐去（避免悬空 tool_call，沿用旧渲染语义）。"""
    records = [
        _rec(T.TASK_DISPATCH, "", 0, role="assistant", tool_call_id="orphan",
             tool_name="control:delegate_task", arguments={}, task_id="D"),
    ]
    assert normalize_legacy_dispatch(records) == []


def test_fail_outcome_gets_prefix() -> None:
    """legacy RESULT 带 outcome=fail → 内容补 [outcome=fail] 前缀（与新数据渲染一致）。"""
    records = [
        _rec(T.TASK_DISPATCH, "", 0, role="assistant", tool_call_id="oc1",
             tool_name="control:delegate_task", arguments={}, task_id="D"),
        _rec(T.TASK_DISPATCH_RESULT, "bad result", 1, role="tool",
             tool_call_id="oc1", parent_task_id="D", outcome="fail"),
    ]
    out = normalize_legacy_dispatch(records)
    result = next(r for r in out if r.role == "tool")
    assert result.content == "[outcome=fail] bad result"


def test_non_legacy_records_pass_through_unchanged() -> None:
    """非 legacy 记录（finish 对 conversation turn、摘要）原样透传、顺序不变。"""
    finish = _rec(T.AGENT_CONVERSATION_TURN, "Process Report: done", 2, role="tool",
                  origin_task_id="D", tool_call_id="finish1")
    summary = _rec(T.AGENT_COMPACT_SUMMARY, "summary", 3, role="user")
    out = normalize_legacy_dispatch([finish, summary])
    assert out == [finish, summary]
