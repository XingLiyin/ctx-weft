"""TaskManagerHooks：一次性接线的 7 个回调收成一个不可变载荷。"""

from __future__ import annotations

import dataclasses

import pytest

from ctx_weft.core.orchestrator.hooks import TaskManagerHooks
from ctx_weft.core.orchestrator.task_manager import TaskManager


def _tm() -> TaskManager:
    return TaskManager(session_id="ses_1")


def test_unwired_task_manager_is_usable():
    """未接线的 TM 仍可用：is_current 缺省 → 永远视为 current。"""
    assert _tm().is_alive() is True


def test_set_hooks_installs_is_current():
    tm = _tm()
    tm.set_hooks(TaskManagerHooks(is_current=lambda: False))
    assert tm.is_alive() is False


def test_set_hooks_replaces_wholesale():
    """整体替换语义——不逐字段合并，装不出半接线的中间态。"""
    tm = _tm()
    tm.set_hooks(TaskManagerHooks(is_current=lambda: False))
    tm.set_hooks(TaskManagerHooks())
    assert tm.is_alive() is True


def test_hooks_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        TaskManagerHooks().is_current = lambda: True  # type: ignore[misc]


def test_legacy_setters_are_gone():
    """7 个一次性 setter 已被 set_hooks 取代；set_session_registry 是死代码，已删。"""
    tm = _tm()
    for name in (
        "set_is_current", "set_session_registry", "set_cancel_pending_hitl",
        "set_cancel_inflight", "set_threshold_finalizer", "set_cancel_finalizer",
        "set_session_done_callback", "set_session_idle_callback",
    ):
        assert not hasattr(tm, name), f"{name} 应已被 set_hooks 取代"


def test_runtime_switches_stay_separate_methods():
    """在不同时刻被多次调用的三个，不属于一次性接线，保持独立方法。"""
    tm = _tm()
    for name in ("set_runner", "set_session", "set_pause_abandon"):
        assert hasattr(tm, name)


def test_all_fields_default_to_none():
    """None-tolerant：缺注入时对应副作用整体跳过。"""
    h = TaskManagerHooks()
    assert all(getattr(h, f.name) is None for f in dataclasses.fields(h))
