"""Run a single child command with liveness-based timeout and reliable kill.

asyncio-native. Liveness = OR of three signals (stdout bytes, summed CPU time,
summed IO bytes) across the whole process tree, so a slow-but-healthy task
(OCR / docx / ffmpeg) is not killed while a genuinely hung one is. Termination
is tree-wide (POSIX process group / Windows Job Object) with kill-then-verify
honest survivor reporting — never claims a clean kill it did not achieve.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import psutil

logger = logging.getLogger(__name__)

# 冻结态 GUI 后端（console=False）派生控制台子进程（cmd/python/rg…）时，Windows 会
# 闪一个黑窗。输出都走管道、子进程不需要控制台，故建进程时禁建窗口。非 Windows 无此
# 常量 → 取 0（subprocess.Popen 各平台都接受 creationflags，POSIX 上忽略）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── liveness primitives ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class LivenessSample:
    output_bytes: int
    cpu_seconds: float
    io_bytes: int


def made_progress(prev: LivenessSample, cur: LivenessSample) -> bool:
    """True if ANY liveness signal grew between two samples."""
    return (
        cur.output_bytes > prev.output_bytes
        or cur.cpu_seconds > prev.cpu_seconds
        or cur.io_bytes > prev.io_bytes
    )


def collect_tree_metrics(pid: int, *, output_bytes: int) -> LivenessSample | None:
    """Sum CPU + IO across pid and all descendants. None if the root is gone.

    Reads only this process tree's own counters (never system-global), so other
    apps' activity cannot mask a hung child. io_counters is unavailable on some
    platforms (macOS) — those procs simply contribute 0 IO.
    """
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None

    cpu = 0.0
    io = 0
    for pr in procs:
        try:
            t = pr.cpu_times()
            cpu += t.user + t.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            ioc = pr.io_counters()
            io += ioc.read_bytes + ioc.write_bytes
        except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError, AttributeError):
            continue
    return LivenessSample(output_bytes=output_bytes, cpu_seconds=cpu, io_bytes=io)


# ── containment + termination ─────────────────────────────────────────────────


@dataclass
class TerminationResult:
    clean: bool
    survivors: list[int] = field(default_factory=list)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _make_windows_job() -> object | None:
    """Create a Job Object with KILL_ON_JOB_CLOSE. None on failure (degrade to psutil)."""
    import ctypes
    from ctypes import wintypes

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            h_job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(h_job)
            return None
        return h_job
    except OSError:
        logger.debug("Job Object creation failed; falling back to psutil kill", exc_info=True)
        return None


def _assign_to_job(h_job: object, pid: int) -> None:
    import ctypes

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if h_proc:
            kernel32.AssignProcessToJobObject(h_job, h_proc)
            kernel32.CloseHandle(h_proc)
    except OSError:
        logger.debug("AssignProcessToJobObject failed", exc_info=True)


async def spawn_contained(command: str, *, cwd: str | None, env: dict | None):
    """Spawn a shell command in an OS container (process group / Job Object).

    Returns (proc, handle) where handle is the Windows job object (or None on POSIX).
    """
    if _is_windows():
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
        )
        h_job = _make_windows_job()
        if h_job is not None:
            _assign_to_job(h_job, proc.pid)
        return proc, h_job

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,  # new process group → killpg targets the whole tree
    )
    return proc, None


async def terminate_tree(proc, handle: object | None) -> TerminationResult:
    """Kill the whole tree, then verify. Reports survivors honestly."""
    if proc.returncode is not None:
        return TerminationResult(clean=True, survivors=[])

    pid = proc.pid
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        tree = []

    # 1) OS-level container kill.
    if _is_windows():
        if handle is not None:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(handle)  # KILL_ON_JOB_CLOSE
    else:
        try:
            os.killpg(os.getpgid(pid), 15)  # SIGTERM
        except (ProcessLookupError, PermissionError):
            pass

    # 2) psutil belt-and-suspenders: terminate then kill survivors.
    for pr in tree:
        try:
            pr.terminate()
        except psutil.NoSuchProcess:
            continue
    _gone, alive = psutil.wait_procs(tree, timeout=3)
    for pr in alive:
        try:
            pr.kill()
        except psutil.NoSuchProcess:
            continue
    if not _is_windows():
        try:
            os.killpg(os.getpgid(pid), 9)  # SIGKILL
        except (ProcessLookupError, PermissionError):
            pass

    # 3) Verify.
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass
    _, still_alive = psutil.wait_procs(tree, timeout=1)
    survivors = [pr.pid for pr in still_alive if pr.is_running()]
    return TerminationResult(clean=not survivors, survivors=survivors)


# ── run_with_liveness ─────────────────────────────────────────────────────────


@dataclass
class LivenessProgress:
    elapsed_sec: float
    output_bytes: int
    idle_remaining_sec: float
    hard_cap_remaining_sec: float
    cpu_seconds: float


@dataclass
class RunResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    timeout_kind: str | None        # "idle" | "hard_cap" | None
    terminated_clean: bool
    survivors: list[int] = field(default_factory=list)


async def run_with_liveness(
    command: str,
    *,
    cwd: str | None,
    env: dict | None,
    idle_timeout_sec: float,
    hard_cap_sec: float,
    output_limit_bytes: int,
    poll_interval_sec: float = 1.0,
    on_output: Callable[[str, str], None] | None = None,
    on_progress: Callable[[LivenessProgress], None] | None = None,
) -> RunResult:
    from ctx_weft.providers._encoding import decode_console

    proc, handle = await spawn_contained(command, cwd=cwd, env=env)
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    counters = {"output_bytes": 0}
    start = time.monotonic()

    async def _reader(stream, name: str, buf: list[str]):
        assert stream is not None
        size = 0
        while True:
            line = await stream.readline()
            if not line:
                break
            counters["output_bytes"] += len(line)
            if size < output_limit_bytes:
                text = decode_console(line)
                buf.append(text)
                size += len(line)
                if on_output is not None:
                    try:
                        on_output(name, text)
                    except Exception:
                        logger.debug("on_output callback raised", exc_info=True)

    readers = [
        asyncio.create_task(_reader(proc.stdout, "stdout", stdout_buf)),
        asyncio.create_task(_reader(proc.stderr, "stderr", stderr_buf)),
    ]

    timeout_kind: str | None = None
    last_alive = time.monotonic()
    prev = LivenessSample(output_bytes=0, cpu_seconds=0.0, io_bytes=0)

    async def _poller():
        nonlocal timeout_kind, last_alive, prev
        while proc.returncode is None:
            await asyncio.sleep(poll_interval_sec)
            now = time.monotonic()
            elapsed = now - start
            sample = collect_tree_metrics(proc.pid, output_bytes=counters["output_bytes"])
            if sample is not None:
                if made_progress(prev, sample):
                    last_alive = now
                prev = sample
            idle_remaining = max(0.0, idle_timeout_sec - (now - last_alive))
            hard_remaining = max(0.0, hard_cap_sec - elapsed)
            if on_progress is not None:
                try:
                    on_progress(LivenessProgress(
                        elapsed_sec=elapsed,
                        output_bytes=counters["output_bytes"],
                        idle_remaining_sec=idle_remaining,
                        hard_cap_remaining_sec=hard_remaining,
                        cpu_seconds=prev.cpu_seconds,
                    ))
                except Exception:
                    logger.debug("on_progress callback raised", exc_info=True)
            if elapsed >= hard_cap_sec:
                timeout_kind = "hard_cap"
                return
            if (now - last_alive) >= idle_timeout_sec:
                timeout_kind = "idle"
                return

    poller = asyncio.create_task(_poller())
    termination = TerminationResult(clean=True, survivors=[])
    try:
        await asyncio.wait(
            {asyncio.create_task(proc.wait()), poller},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timeout_kind is not None:
            termination = await terminate_tree(proc, handle)
        for r in readers:
            try:
                await asyncio.wait_for(r, timeout=2)
            except asyncio.TimeoutError:
                r.cancel()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
    finally:
        poller.cancel()
        if proc.returncode is None:
            termination = await terminate_tree(proc, handle)

    return RunResult(
        stdout="".join(stdout_buf),
        stderr="".join(stderr_buf),
        exit_code=proc.returncode,
        timed_out=timeout_kind is not None,
        timeout_kind=timeout_kind,
        terminated_clean=termination.clean,
        survivors=termination.survivors,
    )
