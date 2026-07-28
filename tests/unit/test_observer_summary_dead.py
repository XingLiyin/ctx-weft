"""OBSERVER_SUMMARY 写侧已死（v2 设计 §8 P1）：suspend / tracking flush 不再写入。

枚举成员本身按 §5.0 永不物理删除，保留在 protocols/memory.py（定义 + EVENT_LAYER 兜底）；
除此之外 src 里不允许再出现对该成员的**代码引用**（`MemoryEventType.OBSERVER_SUMMARY`）——
写点已杀、估算清单已剔除。注释/docstring 里提及历史名属于文档，不算违规。
"""

from __future__ import annotations

import pathlib


def test_no_observer_summary_code_references_in_src() -> None:
    root = pathlib.Path(__file__).resolve().parents[2] / "src" / "ctx_weft"
    # 豁免：枚举定义 + EVENT_LAYER 兜底（memory.py）；归一化模块（memory_compat.py）是
    # v2 设计钦定的「全仓唯一认识旧词汇的地方」，其 _DEAD_WRITE_TYPES 合法引用死类型。
    exempt = {("protocols", "memory.py"), ("protocols", "memory_compat.py")}
    offenders = []
    for p in root.rglob("*.py"):
        if (p.parent.name, p.name) in exempt:
            continue
        if "MemoryEventType.OBSERVER_SUMMARY" in p.read_text(encoding="utf-8"):
            offenders.append(str(p))
    assert offenders == []
