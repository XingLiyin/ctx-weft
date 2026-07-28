"""v2 词汇 + 旧词汇读侧归一化（v2 设计 §2/§6）。

全仓唯一认识旧 type 词汇的地方：
- MemoryKind：v2 内容种类（封死，永不为新机制扩——新机制 = 新 metadata 约定）
- LEGACY_TRIPLE：旧 type → (kind, layer, role 约束)，读侧归一化 / 过渡期双词汇匹配的唯一映射
- kind_of / layer_of：事件的 kind/layer 归一（显式字段优先，旧 type 兜底）
- matches_legacy_type：过渡期桥接——一条记录（新旧词汇皆可）是否命中一个旧 type 请求
- normalize_view：视图返回前的统一归一化（重打 kind/layer + legacy dispatch 配对）

OBSERVER_SUMMARY 刻意不映射：写侧已死（P1），存量行不进任何视图。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from ctx_weft.protocols.memory import EVENT_LAYER, MemoryEventType, MemoryLayer

if TYPE_CHECKING:
    from ctx_weft.protocols.memory import MemoryRecord


class MemoryKind(StrEnum):
    """v2 内容种类（设计 §2）。判据：不同 kind = provider 可施加不同存储/索引/保留策略。"""

    CONVERSATION_TURN = "conversation_turn"  # 对话回合（user/assistant/tool），各 scope 通用
    SUMMARY = "summary"                      # 遗忘补偿：段摘要 / 经验摘要（fold 的 replacement）
    TOOL_AUDIT = "tool_audit"                # 真实能力调用审计；默认不进视图装配
    PUBLICATION = "publication"              # topic 发布；按流读取（recall_topic）


# 旧 type → (kind, layer, role 约束)。role=None 表示该词汇不含 role 约束。
LEGACY_TRIPLE: dict[MemoryEventType, tuple[MemoryKind, MemoryLayer, str | None]] = {
    MemoryEventType.USER_PROMPT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "user"),
    MemoryEventType.LLM_RESPONSE: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "assistant"),
    MemoryEventType.TOOL_RESULT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.TASK, "tool"),
    MemoryEventType.TOOL_INVOCATION: (MemoryKind.TOOL_AUDIT, MemoryLayer.TASK, None),
    MemoryEventType.TASK_COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.TASK, None),
    MemoryEventType.AGENT_COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.AGENT, None),
    MemoryEventType.AGENT_CONVERSATION_TURN: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, None),
    MemoryEventType.BLACKBOARD_PUBLISH: (MemoryKind.PUBLICATION, MemoryLayer.SESSION, None),
    MemoryEventType.TASK_DISPATCH: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, "assistant"),
    MemoryEventType.TASK_DISPATCH_RESULT: (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT, "tool"),
    MemoryEventType.COMPACT_SUMMARY: (MemoryKind.SUMMARY, MemoryLayer.AGENT, None),
}


def kind_of(type_: MemoryEventType | None, kind: MemoryKind | None) -> MemoryKind:
    """事件 kind 归一：显式 kind 优先；旧 type 查 LEGACY_TRIPLE；死类型/双空抛 ValueError。"""
    if kind is not None:
        return kind
    if type_ is None:
        raise ValueError("memory event carries neither type nor kind")
    triple = LEGACY_TRIPLE.get(type_)
    if triple is None:
        raise ValueError(f"legacy type {type_} has no v2 mapping (dead type)")
    return triple[0]


def layer_of(type_: MemoryEventType | None, layer: MemoryLayer | None) -> MemoryLayer:
    """事件 layer 归一：显式 layer 优先；旧 type 走 EVENT_LAYER 兜底；双空抛 ValueError。"""
    if layer is not None:
        return layer
    if type_ is None:
        raise ValueError("memory event carries neither type nor layer")
    return EVENT_LAYER[type_]


def matches_legacy_type(
    record_type: MemoryEventType | None,
    record_kind: MemoryKind | None,
    record_layer: MemoryLayer | None,
    record_role: str | None,
    wanted: MemoryEventType,
) -> bool:
    """过渡期桥接：一条记录（新旧词汇皆可）是否命中一个旧 type 请求。

    旧行（record_type 非 None）按 type 精确匹配——保持 v1 行为逐字节不变；
    v2 行（record_type 为 None）按 LEGACY_TRIPLE 三元组匹配，role=None 视为无约束。
    """
    if record_type is not None:
        return record_type == wanted
    triple = LEGACY_TRIPLE.get(wanted)
    if triple is None:
        return False  # 死类型请求永不命中 v2 行
    k, lyr, role = triple
    if record_kind != k or record_layer != lyr:
        return False
    return role is None or record_role == role


def normalize_view(records: "list[MemoryRecord]") -> "list[MemoryRecord]":
    """视图返回前的统一归一化。

    Task 4（load_view 落地）实装：legacy dispatch 配对（委托 _legacy_dispatch）+
    kind/layer/address 重打。本阶段直通。
    """
    return records
