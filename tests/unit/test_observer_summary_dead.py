"""OBSERVER_SUMMARY 写侧已死（v2 设计 §8 P1）：suspend / tracking flush 不再写入。

枚举成员本身按 §5.0 永不物理删除，保留在 protocols/memory.py（定义 + EVENT_LAYER 兜底）；
除此之外 src 里不允许再出现对该成员的**代码引用**（`MemoryEventType.OBSERVER_SUMMARY`）——
写点已杀、估算清单已剔除。注释/docstring 里提及历史名属于文档，不算违规。
"""

from __future__ import annotations

import pathlib


def test_no_observer_summary_code_references_in_src() -> None:
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "ctx_weft"
    offenders = []
    for p in root.rglob("*.py"):
        if p.name == "memory.py" and p.parent.name == "protocols":
            continue  # 枚举定义 + EVENT_LAYER 兜底映射合法保留
        if "MemoryEventType.OBSERVER_SUMMARY" in p.read_text(encoding="utf-8"):
            offenders.append(str(p))
    assert offenders == []
