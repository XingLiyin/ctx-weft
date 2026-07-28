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


# 写侧已死的 legacy 词汇：查询它们只该命中**存量旧行**，v2 新行永不匹配
# （否则「不再写 legacy enum」的回归断言 / 计数口径全部失真）。
_DEAD_WRITE_TYPES: frozenset[MemoryEventType] = frozenset({
    MemoryEventType.TASK_DISPATCH,
    MemoryEventType.TASK_DISPATCH_RESULT,
    MemoryEventType.COMPACT_SUMMARY,
    MemoryEventType.OBSERVER_SUMMARY,
})


def matches_legacy_type(
    record_type: MemoryEventType | None,
    record_kind: MemoryKind | None,
    record_layer: MemoryLayer | None,
    record_role: str | None,
    wanted: MemoryEventType,
) -> bool:
    """过渡期桥接：一条记录（新旧词汇皆可）是否命中一个旧 type 请求。

    旧行（record_type 非 None）按 type 精确匹配——保持 v1 行为逐字节不变；
    v2 行（record_type 为 None）按 LEGACY_TRIPLE 三元组匹配，role=None 视为无约束；
    写侧已死的词汇（_DEAD_WRITE_TYPES）只命中存量旧行、永不命中 v2 行。
    """
    if record_type is not None:
        return record_type == wanted
    if wanted in _DEAD_WRITE_TYPES:
        return False
    triple = LEGACY_TRIPLE.get(wanted)
    if triple is None:
        return False
    k, lyr, role = triple
    if record_kind != k or record_layer != lyr:
        return False
    return role is None or record_role == role


# (kind, layer) → 无 role 歧义时的 legacy 等价词汇；CONVERSATION_TURN@TASK 按 role 细分。
_TRIPLE_TO_LEGACY: dict[tuple[MemoryKind, MemoryLayer], MemoryEventType] = {
    (MemoryKind.SUMMARY, MemoryLayer.TASK): MemoryEventType.TASK_COMPACT_SUMMARY,
    (MemoryKind.SUMMARY, MemoryLayer.AGENT): MemoryEventType.AGENT_COMPACT_SUMMARY,
    (MemoryKind.CONVERSATION_TURN, MemoryLayer.AGENT): MemoryEventType.AGENT_CONVERSATION_TURN,
    (MemoryKind.TOOL_AUDIT, MemoryLayer.TASK): MemoryEventType.TOOL_INVOCATION,
    (MemoryKind.PUBLICATION, MemoryLayer.SESSION): MemoryEventType.BLACKBOARD_PUBLISH,
}
_TASK_TURN_BY_ROLE: dict[str, MemoryEventType] = {
    "user": MemoryEventType.USER_PROMPT,
    "assistant": MemoryEventType.LLM_RESPONSE,
    "tool": MemoryEventType.TOOL_RESULT,
}


def legacy_type_of(
    kind: MemoryKind | None, layer: MemoryLayer | None, role: str | None,
) -> MemoryEventType | None:
    """v2 三元组 → legacy 等价词汇（recall wrapper 的 type 回填 / 渲染链 mtype 派生）。

    渲染与旧断言消费的是 legacy 字符串词汇（composer 的 mtype=="user_prompt" 框定位、
    slot_priority 的 mem_type 档位）；过渡期由此函数单点派生，P4 随消费点迁移一并日落。
    """
    if kind is MemoryKind.CONVERSATION_TURN and layer is MemoryLayer.TASK:
        return _TASK_TURN_BY_ROLE.get(role or "")
    if kind is None or layer is None:
        return None
    return _TRIPLE_TO_LEGACY.get((kind, layer))


def normalize_view(records: "list[MemoryRecord]") -> "list[MemoryRecord]":
    """视图返回前的统一归一化（v2 设计 §6）。

    1. legacy dispatch 配对：TASK_DISPATCH/RESULT → AGENT_CONVERSATION_TURN 回合，
       孤立 dispatch 隐去（委托 _legacy_dispatch，全仓唯一配对实现）；
    2. kind/layer 重打：legacy 行按 LEGACY_TRIPLE 补全（v2 行已带，原样）。
    address 回显由 provider 在 record 构造时填（来源即存储行的归档地址）。
    """
    from ctx_weft.protocols._legacy_dispatch import normalize_legacy_dispatch

    out: list[MemoryRecord] = []
    for r in normalize_legacy_dispatch(records):
        if r.kind is None and r.type is not None:
            triple = LEGACY_TRIPLE.get(r.type)
            if triple is not None:
                r.kind, r.layer = triple[0], triple[1]
        out.append(r)
    return out
