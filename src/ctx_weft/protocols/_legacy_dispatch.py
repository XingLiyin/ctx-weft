"""存量 dispatch 对 → conversation turn 归一化适配层（spec 2026-06-28 §5.5）。

新数据里 dispatch 对已直接写成 AGENT_CONVERSATION_TURN（gateway 写 delegate assistant 回合、
finalize 写 result tool 回合，与同单元 finish 对同 origin、同命运）。本模块只负责把**存量旧数据**
里残留的 legacy `TASK_DISPATCH` / `TASK_DISPATCH_RESULT` 在**读侧**归一化成等价的 conversation
turn，使召回（agent_recall）与折叠（fold_root_experience）只需面对单一表示。

v2 P2a（2026-07-27）：自 core/loop/steps/legacy_dispatch.py 移入 protocols 层——
统一归一化模块（memory_compat.normalize_view）委托本模块做 dispatch 配对，provider 侧
（含 host postgres）可直接 import；全仓认识旧词汇的地方收敛到 protocols 一处。
当确认线上再无 legacy 记录时，删本模块 + 调用即可（枚举本身按 §5.0 永不物理删除）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ctx_weft.protocols.capability import qualify
from ctx_weft.protocols.memory import MemoryEventType

if TYPE_CHECKING:
    from ctx_weft.protocols.memory import MemoryRecord

_DELEGATE_DEFAULT = qualify("control:delegate_task")


def normalize_legacy_dispatch(records: "list[MemoryRecord]") -> "list[MemoryRecord]":
    """把 records 里的 legacy TASK_DISPATCH/RESULT 就地归一化成 AGENT_CONVERSATION_TURN。

    - TASK_DISPATCH → assistant 回合（tool_calls 承载 delegate 调用），origin_task_id 取写入
      scope 的 task_id（= delegating task）；未配对（无对应 RESULT）的孤立 dispatch 隐去，
      沿用旧 agent_recall「未配对 dispatch 不渲染、避免悬空 tool_call」语义。
    - TASK_DISPATCH_RESULT → tool 回合，origin_task_id 取 parent_task_id（= delegating task），
      与 delegate assistant 回合靠 tool_call_id 配对。
    - 非 legacy 记录原样透传，顺序不变（调用方依赖 recall 的 (timestamp) 序）。
    """
    result_tcids = {
        r.metadata.get("tool_call_id")
        for r in records
        if r.type == MemoryEventType.TASK_DISPATCH_RESULT
    }
    out: list[MemoryRecord] = []
    for r in records:
        if r.type == MemoryEventType.TASK_DISPATCH:
            tcid = r.metadata.get("tool_call_id")
            if tcid not in result_tcids:
                continue  # 孤立 legacy dispatch 隐去（同旧渲染行为）
            out.append(_as_delegate_turn(r, tcid))
        elif r.type == MemoryEventType.TASK_DISPATCH_RESULT:
            out.append(_as_result_turn(r))
        else:
            out.append(r)
    return out


def _as_delegate_turn(r: "MemoryRecord", tcid: str) -> "MemoryRecord":
    md = dict(r.metadata)
    md["origin_task_id"] = md.get("task_id") or None
    md["tool_calls"] = [{
        "id": tcid,
        "name": md.get("tool_name") or _DELEGATE_DEFAULT,
        "input": md.get("arguments", {}),
    }]
    return replace(r, type=MemoryEventType.AGENT_CONVERSATION_TURN, role="assistant",
                   content="", metadata=md)


def _as_result_turn(r: "MemoryRecord") -> "MemoryRecord":
    md = dict(r.metadata)
    md["origin_task_id"] = md.get("parent_task_id") or md.get("task_id") or None
    # result 回合不定义单元的 parent（单元 parent 由 delegate / finish 对权威给出，见 fold 的
    # prefer-non-None）；清掉旧 parent_task_id 以免污染 parent_of。
    md["parent_task_id"] = None
    content = r.content
    if md.get("outcome") == "fail" and isinstance(content, str) \
            and not content.startswith("[outcome=fail]"):
        content = f"[outcome=fail] {content}"
    return replace(r, type=MemoryEventType.AGENT_CONVERSATION_TURN, role="tool", content=content,
                   metadata=md)
