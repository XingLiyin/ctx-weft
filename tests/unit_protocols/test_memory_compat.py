"""memory_compat：v2 词汇 + 旧词汇归一化（v2 设计 §2/§6）。

全仓唯一认识旧 type 词汇的地方：LEGACY_TRIPLE 把 11 个存活旧类型映射到
(kind, layer, role 约束)；OBSERVER_SUMMARY 刻意不映射（写侧已死，不进视图）。
"""

from __future__ import annotations

import pytest

from ctx_weft.protocols import MemoryAddress, MemoryEventType, MemoryLayer, MemoryAddress
from ctx_weft.protocols.memory_compat import (
    LEGACY_TRIPLE,
    MemoryKind,
    kind_of,
    layer_of,
    matches_legacy_type,
)


def test_memory_address_is_the_scope_dataclass() -> None:
    """过渡别名：MemoryAddress 即原 MemoryAddress 数据类（P4 完成实体互换）。"""
    assert MemoryAddress is MemoryAddress
    addr = MemoryAddress(session_id="s1", task_id="t1")
    assert addr.session_id == "s1" and addr.agent_id is None


def test_legacy_triple_covers_all_live_types() -> None:
    unmapped = {MemoryEventType.OBSERVER_SUMMARY}
    assert set(LEGACY_TRIPLE) == set(MemoryEventType) - unmapped


def test_kind_of_prefers_explicit_kind() -> None:
    assert kind_of(MemoryEventType.USER_PROMPT, None) is MemoryKind.CONVERSATION_TURN
    assert kind_of(None, MemoryKind.SUMMARY) is MemoryKind.SUMMARY
    assert kind_of(MemoryEventType.USER_PROMPT, MemoryKind.SUMMARY) is MemoryKind.SUMMARY


def test_kind_of_rejects_empty_and_dead() -> None:
    with pytest.raises(ValueError):
        kind_of(None, None)
    with pytest.raises(ValueError):
        kind_of(MemoryEventType.OBSERVER_SUMMARY, None)


def test_layer_of_falls_back_to_event_layer() -> None:
    assert layer_of(MemoryEventType.AGENT_COMPACT_SUMMARY, None) is MemoryLayer.AGENT
    assert layer_of(None, MemoryLayer.TASK) is MemoryLayer.TASK
    with pytest.raises(ValueError):
        layer_of(None, None)


def test_matches_legacy_type_bridges_vocabularies() -> None:
    # v2 行（type=None, kind+layer+role）匹配旧类型请求
    assert matches_legacy_type(None, MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "user",
                               MemoryEventType.USER_PROMPT)
    assert not matches_legacy_type(None, MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "assistant",
                                   MemoryEventType.USER_PROMPT)
    # role 无约束的旧类型（AGENT_CONVERSATION_TURN）任意 role 皆匹配
    assert matches_legacy_type(None, MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, "tool",
                               MemoryEventType.AGENT_CONVERSATION_TURN)
    # 旧行按 type 精确匹配（不跨型误配）
    assert matches_legacy_type(MemoryEventType.LLM_RESPONSE, None, None, "assistant",
                               MemoryEventType.LLM_RESPONSE)
    assert not matches_legacy_type(MemoryEventType.LLM_RESPONSE, None, None, "assistant",
                                   MemoryEventType.USER_PROMPT)
    # 死类型请求匹配不到 v2 行
    assert not matches_legacy_type(None, MemoryKind.SUMMARY, MemoryLayer.AGENT, None,
                                   MemoryEventType.OBSERVER_SUMMARY)


def test_legacy_dispatch_shim_moved_but_forwarded() -> None:
    """legacy_dispatch 移入 protocols/_legacy_dispatch；原位置薄转发保兼容。"""
    from ctx_weft.protocols._legacy_dispatch import normalize_legacy_dispatch as new_fn
    from ctx_weft.core.loop.steps.legacy_dispatch import normalize_legacy_dispatch as old_fn
    assert old_fn is new_fn
