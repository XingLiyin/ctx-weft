import asyncio
import os
import platform
import subprocess
import sys

import psutil
import pytest

from ctx_weft.providers._script_runner import (
    LivenessSample,
    collect_tree_metrics,
    made_progress,
    run_with_liveness,
    spawn_contained,
    terminate_tree,
)


# ── liveness primitives ───────────────────────────────────────────────────────


def test_made_progress_on_output_growth():
    a = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=0)
    b = LivenessSample(output_bytes=20, cpu_seconds=1.0, io_bytes=0)
    assert made_progress(a, b) is True


def test_made_progress_on_cpu_growth():
    a = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=0)
    b = LivenessSample(output_bytes=10, cpu_seconds=1.5, io_bytes=0)
    assert made_progress(a, b) is True


def test_made_progress_on_io_growth():
    a = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=0)
    b = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=4096)
    assert made_progress(a, b) is True


def test_no_progress_when_all_flat():
    a = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=4096)
    b = LivenessSample(output_bytes=10, cpu_seconds=1.0, io_bytes=4096)
    assert made_progress(a, b) is False


# ── tree metrics ──────────────────────────────────────────────────────────────


def test_collect_tree_metrics_current_process():
    s = collect_tree_metrics(os.getpid(), output_bytes=123)
    assert s is not None
    assert s.output_bytes == 123
    assert s.cpu_seconds >= 0.0
    assert s.io_bytes >= 0


def test_collect_tree_metrics_dead_pid():
    assert collect_tree_metrics(2_000_000_000, output_bytes=0) is None


# ── containment + termination ─────────────────────────────────────────────────


async def test_spawn_contained_hides_console_window_on_windows(monkeypatch):
    """冻结态 GUI 后端派生 cmd 时不应闪黑窗：Windows 分支须带 CREATE_NO_WINDOW。"""
    if platform.system() != "Windows":
        pytest.skip("console-window flag is Windows-only")
    captured = {}
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["creationflags"] = kw.get("creationflags")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    proc, handle = await spawn_contained("echo hi", cwd=None, env=None)
    await proc.communicate()
    await terminate_tree(proc, handle)
    assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW


async def test_terminate_tree_kills_child_and_grandchild():
    grandchild = f'{sys.executable} -c "import time; time.sleep(60)"'
    proc, handle = await spawn_contained(grandchild, cwd=None, env=None)
    pid = proc.pid
    await asyncio.sleep(1.0)
    result = await terminate_tree(proc, handle)
    assert result.clean is True
    assert result.survivors == []
    assert not psutil.pid_exists(pid)


# ── run_with_liveness orchestration ───────────────────────────────────────────


async def test_clean_exit_collects_output():
    out_lines = []
    result = await run_with_liveness(
        f'{sys.executable} -c "print(\'hello\'); print(\'world\')"',
        cwd=None, env=None,
        idle_timeout_sec=10, hard_cap_sec=30, output_limit_bytes=100_000,
        poll_interval_sec=0.2,
        on_output=lambda stream, text: out_lines.append((stream, text)),
    )
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.timeout_kind is None
    assert result.terminated_clean is True
    assert "hello" in result.stdout
    assert "world" in result.stdout
    assert any("hello" in t for _, t in out_lines)


async def test_idle_timeout_kills_silent_sleeper():
    progress = []
    result = await run_with_liveness(
        f'{sys.executable} -c "import time; time.sleep(60)"',
        cwd=None, env=None,
        idle_timeout_sec=1.5, hard_cap_sec=30, output_limit_bytes=100_000,
        poll_interval_sec=0.3,
        on_progress=lambda p: progress.append(p),
    )
    assert result.timed_out is True
    assert result.timeout_kind == "idle"
    assert result.terminated_clean is True
    assert result.survivors == []
    assert len(progress) >= 1
    assert progress[-1].idle_remaining_sec == 0


async def test_hard_cap_kills_busy_but_overlong():
    # Single shell line (no real newlines); the loop's newline lives inside exec()'s
    # string literal so cmd.exe/sh receives one command.
    busy = (
        sys.executable
        + " -u -c \"import time; exec('while True:\\n print(1); time.sleep(0.05)')\""
    )
    result = await run_with_liveness(
        busy, cwd=None, env=None,
        idle_timeout_sec=30, hard_cap_sec=1.5, output_limit_bytes=100_000,
        poll_interval_sec=0.3,
    )
    assert result.timed_out is True
    assert result.timeout_kind == "hard_cap"
    assert result.terminated_clean is True
