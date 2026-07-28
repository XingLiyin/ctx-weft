"""provider 共享助手（v2 §4/§6）：kind_expansion（SQL 查询侧别名展开）+ validate_half_address。

两个 provider（in-memory / host postgres）共用，避免各自维护展开表与半址矩阵。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols import MemoryAddress, MemoryScope
from ctx_weft.protocols.memory_compat import (
    MemoryKind,
    kind_expansion,
    validate_half_address,
)


def test_kind_expansion_task_conversation() -> None:
    got = kind_expansion(MemoryKind.CONVERSATION_TURN, MemoryScope.TASK)
    assert got == frozenset({"conversation_turn", "user_prompt", "llm_response", "tool_result"})


def test_kind_expansion_agent_conversation_includes_legacy_dispatch() -> None:
    got = kind_expansion(MemoryKind.CONVERSATION_TURN, MemoryScope.AGENT)
    assert got == frozenset({
        "conversation_turn", "agent_conversation_turn",
        "task_dispatch", "task_dispatch_result",
    })


def test_kind_expansion_summary_layers() -> None:
    assert kind_expansion(MemoryKind.SUMMARY, MemoryScope.TASK) == frozenset(
        {"summary", "task_compact_summary"})
    # AGENT 层含死类型 compact_summary（读侧兼容存量）；observer_summary 永不出现
    assert kind_expansion(MemoryKind.SUMMARY, MemoryScope.AGENT) == frozenset(
        {"summary", "agent_compact_summary", "compact_summary"})


def test_validate_half_address_matrix() -> None:
    validate_half_address(MemoryAddress(session_id="s", task_id="t"), MemoryScope.TASK)
    validate_half_address(MemoryAddress(session_id="s", agent_id="a"), MemoryScope.TASK)
    validate_half_address(MemoryAddress(session_id="s", agent_id="a"), MemoryScope.AGENT)
    validate_half_address(MemoryAddress(session_id="s"), MemoryScope.SESSION)
    with pytest.raises(ValueError):
        validate_half_address(MemoryAddress(session_id="s"), MemoryScope.TASK)
    with pytest.raises(ValueError):
        validate_half_address(MemoryAddress(session_id="s", task_id="t", agent_id="a"),
                              MemoryScope.AGENT)
    with pytest.raises(ValueError):
        validate_half_address(MemoryAddress(session_id="s", task_id="t"), MemoryScope.SESSION)
