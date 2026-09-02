"""三层各发各的：HITL→task，loop→run，TM→会话级信号（Task 6）。"""

from __future__ import annotations

import pathlib


def test_no_component_emits_session_status_changed_any_more():
    """SessionStatusChanged 进 L 档：只读存量，不得再发射
    （docs/events-v2.md §5.2、§6 不变式 6）。"""
    src = pathlib.Path("src/ctx_weft")
    offenders = [
        str(p) for p in src.rglob("*.py")
        if "SESSION_STATUS_CHANGED" in p.read_text(encoding="utf-8")
        and p.name not in ("reducers.py", "events.py", "_lifecycle.py")
    ]
    assert offenders == []


def test_no_component_dispatches_on_a_task_suspended_reason():
    """判据只能是类型，不能是 payload 里的字符串（Global Constraints）。
    reducers 读存量事件时可以认这两个字符串；别处不许拿它们做路由。"""
    src = pathlib.Path("src/ctx_weft")
    offenders = []
    for p in src.rglob("*.py"):
        if p.name == "reducers.py":
            continue
        text = p.read_text(encoding="utf-8")
        for needle in ('"hitl_park"', '"run_crash"'):
            if needle in text:
                offenders.append(f"{p}:{needle}")
    assert offenders == []
