"""LoopConfig 短任务阈值字段（spec 2026-06-23）。"""

from __future__ import annotations

from ctx_weft.protocols.template import LoopConfig


def test_short_task_defaults() -> None:
    cfg = LoopConfig()
    assert cfg.short_task_token_threshold == 1000
    assert cfg.short_task_turn_cap == 2


def test_short_task_overridable() -> None:
    cfg = LoopConfig(short_task_token_threshold=500, short_task_turn_cap=1)
    assert cfg.short_task_token_threshold == 500
    assert cfg.short_task_turn_cap == 1
