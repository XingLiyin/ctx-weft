"""FilesystemToolsProvider：文件系统操作工具集 + per-session workspace 管理。

工具（bash_exec / read_file / write_file / glob）都在 session 的工作目录下运作：
  - bash_exec 以 workspace 为 cwd；
  - read_file / write_file / glob 把相对路径锚定到 workspace。

workspace 是本 provider 的内部概念，core 不知道它的存在：host 在 session 启动前调用
register_session(session_id, 绝对路径) 登记，本 provider 维护 session_id → workspace 映射。
对 core 而言本 provider 只实现两个协议：
  - SpillSink（spill）：CapabilityGateway 截断超长输出时落盘到对应 workspace；
  - SessionScopedCapabilityProvider（deregister_session）：session 结束时释放映射。
register_session / workspace_for 是 provider 自有方法（host 接线用），不在任何协议内。
"""

from __future__ import annotations

import asyncio
import dataclasses
import glob as _glob
import logging
import os
import shlex
import platform
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from loomex_core.core.utils import generate_id
from loomex_core.protocols.capability import (
    Capability,
    CapabilityEvent,
    CapabilityProviderInfo,
    SessionScopedCapabilityProvider,
    ToolCapabilityProvider,
)
from loomex_core.protocols.context import ProviderContext
from loomex_core.protocols.filesystem import FS_PROVIDER_NAME, SpillSink
from loomex_core.providers._encoding import decode_console
from loomex_core.providers._script_runner import run_with_liveness
from loomex_core.providers._tooldecl import make_tool_registry

logger = logging.getLogger(__name__)

tool, _FS_TOOLS, _FS_IMPLS = make_tool_registry(FS_PROVIDER_NAME)

# ── 共享常量 ──────────────────────────────────────────────────────────────────

_BASH_BLACKLIST = frozenset([
    "rm", "rmdir", "del", "format", "mkfs", "dd",
    "shutdown", "reboot", "halt", "poweroff",
    "passwd", "sudo", "su", "chmod", "chown",
    "crontab", "at", "nohup",
    "wget", "curl",
])
_BASH_IDLE_TIMEOUT_SEC_DEFAULT = 30
_BASH_HARD_CAP_SEC_DEFAULT = 120
_BASH_MAX_OUTPUT_BYTES_DEFAULT = 50_000
_FILE_MAX_READ_BYTES_DEFAULT = 500_000
_GLOB_MAX_RESULTS_DEFAULT = 500


def _bash_exec_description() -> str:
    """Build a platform-aware description so the LLM picks the right shell syntax.

    The description states the concrete host OS, which shell the command runs in,
    which command family is available vs unavailable for that OS, and the list of
    commands that are blocked regardless of OS (``_BASH_BLACKLIST``).
    """
    system = platform.system()  # 'Windows' | 'Linux' | 'Darwin'
    detail = platform.platform()
    blocked = ", ".join(sorted(_BASH_BLACKLIST))
    blocked_note = (
        f"The following commands are blocked on every OS and will be rejected: {blocked}."
    )
    if system == "Windows":
        return (
            f"Execute a shell command and return stdout/stderr. "
            f"Host OS is Windows ({detail}); commands run via cmd.exe. "
            f"Use Windows commands (e.g. dir, type, copy, findstr, where); "
            f"Linux/Unix commands such as ls, cat, grep are NOT available. "
            f"Paths: write 'D:\\foo\\bar' (single backslash) or 'D:/foo/bar' "
            f"(forward slashes) — do NOT double-escape backslashes. "
            f"When you need PowerShell, call "
            f"powershell -NoProfile -Command \"...\"; on Windows PowerShell 5.1 "
            f"prepend [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); so "
            f"Chinese output is not garbled (PowerShell 7 already uses UTF-8). "
            f"{blocked_note}"
        )
    shell_kind = "macOS" if system == "Darwin" else "Linux"
    return (
        f"Execute a shell command and return stdout/stderr. "
        f"Host OS is {shell_kind} ({detail}); commands run via /bin/sh. "
        f"Use POSIX shell commands (e.g. ls, cat, grep, find); "
        f"Windows-only commands such as dir, type, findstr are NOT available. "
        f"{blocked_note}"
    )


def _allowed_dirs(ctx: ProviderContext | None) -> list[Path]:
    if ctx is None:
        return []
    raw = ctx.extra.get("allowed_dirs") or []
    return [d if isinstance(d, Path) else Path(d) for d in raw]


def _workspace(ctx: ProviderContext | None) -> Path | None:
    """该次调用的 session 工作目录（provider.invoke 注入到 ctx.extra）。"""
    if ctx is None:
        return None
    ws = ctx.extra.get("workspace")
    return Path(ws) if ws else None


def _check_path(path: Path, allowed_dirs: list[Path]) -> bool:
    """Return True if path is within at least one allowed directory, or no restriction."""
    if not allowed_dirs:
        return True
    resolved = path.resolve()
    return any(str(resolved).startswith(str(d.resolve())) for d in allowed_dirs)


def _resolve(path: str, ctx: ProviderContext | None) -> Path:
    """相对路径锚定到 session workspace；绝对路径原样。"""
    p = Path(path)
    if p.is_absolute():
        return p
    ws = _workspace(ctx)
    return (ws / p) if ws else p


# ── 工具实现 ──────────────────────────────────────────────────────────────────


@tool(purposes=["act"], side_effects=True, description=_bash_exec_description())
async def bash_exec(
    command: Annotated[str, "Shell command to execute"],
    *,
    ctx: ProviderContext | None = None,
) -> AsyncIterator[CapabilityEvent]:
    """Execute a shell command and return stdout/stderr."""
    if not command.strip():
        yield CapabilityEvent(kind="error", payload={"code": "EMPTY_COMMAND", "message": "command is required"})
        return

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if tokens and tokens[0].lower() in _BASH_BLACKLIST:
        yield CapabilityEvent(
            kind="error",
            payload={"code": "COMMAND_BLACKLISTED", "message": f"Command '{tokens[0]}' is not allowed"},
        )
        return

    yield CapabilityEvent(kind="progress", payload={"status": "starting", "command": command})

    ws = _workspace(ctx)
    cwd = str(ws) if ws else None
    idle = (ctx.extra.get("bash_idle_timeout_sec") if ctx else None) or _BASH_IDLE_TIMEOUT_SEC_DEFAULT
    hard = (ctx.extra.get("bash_hard_cap_sec") if ctx else None) or _BASH_HARD_CAP_SEC_DEFAULT
    max_out = (ctx.extra.get("bash_max_output_bytes") if ctx else None) or _BASH_MAX_OUTPUT_BYTES_DEFAULT

    logger.info("bash_exec command (repr): %r", command)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    # The runner streams decoded output via on_output; bridge those callbacks
    # through a queue so this async generator can yield stdout events live.
    queue: asyncio.Queue = asyncio.Queue()

    def _on_output(_stream: str, text: str) -> None:
        queue.put_nowait(("out", text))

    async def _run() -> None:
        try:
            result = await run_with_liveness(
                command, cwd=cwd, env=env,
                idle_timeout_sec=idle, hard_cap_sec=hard, output_limit_bytes=max_out,
                on_output=_on_output,
            )
            queue.put_nowait(("done", result))
        except Exception as e:  # noqa: BLE001 — surfaced as an error event below
            queue.put_nowait(("exc", e))

    runner = asyncio.create_task(_run())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "out":
                yield CapabilityEvent(kind="stdout", payload={"data": payload})
            elif kind == "exc":
                logger.exception("bash_exec failed: %s", command)
                yield CapabilityEvent(kind="error", payload={"code": "EXEC_ERROR", "message": str(payload)})
                return
            elif kind == "done":
                result = payload
                if result.timed_out:
                    survivor = "" if result.terminated_clean else (
                        f" WARNING: {len(result.survivors)} process(es) may still be running."
                    )
                    yield CapabilityEvent(kind="error", payload={
                        "code": "TIMEOUT",
                        "message": f"Command timed out ({result.timeout_kind}).{survivor}",
                    })
                    return
                exit_code = result.exit_code or 0
                # Fold stderr into content to preserve the old stderr=STDOUT merge.
                content = result.stdout + result.stderr
                yield CapabilityEvent(kind="result", payload={
                    "content": content,
                    "metadata": {"exit_code": exit_code, "is_error": exit_code != 0},
                })
                return
    finally:
        if not runner.done():
            runner.cancel()


@tool(purposes=["act", "compact"], side_effects=False)
async def read_file(
    path: Annotated[str, "File path; relative paths are resolved against the workspace"],
    *,
    ctx: ProviderContext | None = None,
) -> AsyncIterator[CapabilityEvent]:
    """Read the contents of a file and return as text."""
    if not path:
        yield CapabilityEvent(kind="error", payload={"code": "MISSING_PATH", "message": "path is required"})
        return

    file_path = _resolve(path, ctx)
    if not _check_path(file_path, _allowed_dirs(ctx)):
        yield CapabilityEvent(
            kind="error",
            payload={"code": "PATH_NOT_ALLOWED", "message": "Path is outside allowed directories"},
        )
        return

    try:
        if not file_path.exists():
            yield CapabilityEvent(
                kind="error",
                payload={"code": "FILE_NOT_FOUND", "message": f"File not found: {file_path}"},
            )
            return

        _max_read = (ctx.extra.get("file_max_read_bytes") if ctx else None) or _FILE_MAX_READ_BYTES_DEFAULT
        content = file_path.read_bytes()[:_max_read]
        yield CapabilityEvent(
            kind="result",
            payload={
                "content": content.decode("utf-8", errors="replace"),
                "metadata": {
                    "path": str(file_path),
                    "size_bytes": file_path.stat().st_size,
                    "truncated": len(content) == _max_read,
                },
            },
        )
    except Exception as e:
        logger.exception("read_file failed: %s", file_path)
        yield CapabilityEvent(kind="error", payload={"code": "READ_ERROR", "message": str(e)})


@tool(purposes=["act"], side_effects=True)
async def write_file(
    path: Annotated[str, "File path (relative resolved against workspace; parents auto-created)"],
    content: Annotated[str, "Text content to write"],
    *,
    ctx: ProviderContext | None = None,
) -> AsyncIterator[CapabilityEvent]:
    """Write content to a file, overwriting if it already exists."""
    if not path:
        yield CapabilityEvent(kind="error", payload={"code": "MISSING_PATH", "message": "path is required"})
        return

    file_path = _resolve(path, ctx)
    if not _check_path(file_path, _allowed_dirs(ctx)):
        yield CapabilityEvent(
            kind="error",
            payload={"code": "PATH_NOT_ALLOWED", "message": "Path is outside allowed directories"},
        )
        return

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        yield CapabilityEvent(
            kind="result",
            payload={
                "content": f"Written {len(content)} chars to {file_path}",
                "metadata": {"path": str(file_path), "size_chars": len(content)},
            },
        )
    except Exception as e:
        logger.exception("write_file failed: %s", file_path)
        yield CapabilityEvent(kind="error", payload={"code": "WRITE_ERROR", "message": str(e)})


@tool(purposes=["act", "compact"], side_effects=False)
async def glob(
    pattern: Annotated[str, "Glob pattern relative to base_dir, e.g. '**/*.py'"],
    base_dir: Annotated[str, "Base directory (relative resolved against workspace)"] = ".",
    *,
    ctx: ProviderContext | None = None,
) -> AsyncIterator[CapabilityEvent]:
    """List files matching a glob pattern."""
    if not pattern:
        yield CapabilityEvent(kind="error", payload={"code": "MISSING_PATTERN", "message": "pattern is required"})
        return

    try:
        _max_glob = (ctx.extra.get("glob_max_results") if ctx else None) or _GLOB_MAX_RESULTS_DEFAULT
        root = _resolve(base_dir, ctx)
        matches = _glob.glob(str(root / pattern), recursive=True)[:_max_glob]
        yield CapabilityEvent(
            kind="result",
            payload={
                "content": "\n".join(matches) if matches else "(no matches)",
                "metadata": {
                    "pattern": pattern,
                    "base_dir": str(root),
                    "count": len(matches),
                    "truncated": len(matches) == _max_glob,
                },
            },
        )
    except Exception as e:
        logger.exception("glob failed: %s", pattern)
        yield CapabilityEvent(kind="error", payload={"code": "GLOB_ERROR", "message": str(e)})


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass
class FilesystemConfig:
    """Provider 运行时配置。权限控制由 auth 层负责，不在此处理。"""
    allowed_dirs: list[Path] = field(default_factory=list)
    bash_idle_timeout_sec: int = 30
    bash_hard_cap_sec: int = 120
    bash_max_output_bytes: int = 50_000
    file_max_read_bytes: int = 500_000
    glob_max_results: int = 500


# ── Provider ──────────────────────────────────────────────────────────────────


class FilesystemToolsProvider(ToolCapabilityProvider, SpillSink, SessionScopedCapabilityProvider):
    """文件系统工具 provider：bash_exec, read_file, write_file, glob + per-session workspace。

    实现三个面向 core 的契约：ToolCapabilityProvider（invoke 四个工具）、SpillSink（spill 落盘）、
    SessionScopedCapabilityProvider（deregister_session 清理）。register_session / workspace_for
    是 host 接线用的自有方法，不属于任何协议——workspace 对 core 不可见。
    """

    name = FS_PROVIDER_NAME

    def __init__(self, config: FilesystemConfig | None = None) -> None:
        self._cfg = config or FilesystemConfig()
        self._invokers = self._build_invokers()
        self._workspaces: dict[str, str] = {}  # session_id → 绝对路径

    def _build_invokers(self) -> dict[str, Callable]:
        return {
            name: (lambda f: lambda args, ctx: f(**args, ctx=ctx))(fn)
            for name, fn in _FS_IMPLS.items()
        }

    # ── workspace 生命周期 ────────────────────────────────────────────────────

    def register_session(self, session_id: str, workspace: str) -> None:
        """登记某 session 的工作目录（必须绝对路径）。host 接线用的自有方法，不在协议内。"""
        if not workspace or not os.path.isabs(workspace):
            raise ValueError(
                f"workspace must be an absolute path, got: {workspace!r}"
            )
        path = os.path.normpath(workspace)
        os.makedirs(path, exist_ok=True)
        self._workspaces[session_id] = path
        logger.info("Filesystem: registered workspace for session %s → %s", session_id, path)

    def deregister_session(self, session_id: str) -> None:
        """SessionScopedCapabilityProvider：session 结束时由 core 调用，释放 workspace 映射。"""
        self._workspaces.pop(session_id, None)

    def workspace_for(self, ctx: ProviderContext) -> str | None:
        """该 session 已登记的工作目录绝对路径；未登记返回 None。host 接线用的自有方法。"""
        return self._workspaces.get(ctx.session_id)

    async def spill(self, content: str, ctx: ProviderContext, *, name_hint: str = "") -> str:
        """SpillSink：把超长内容落盘到该 session 的 workspace，返回落盘路径；未登记则 raise。"""
        ws = self.workspace_for(ctx)
        if ws is None:
            raise RuntimeError(f"no workspace registered for session {ctx.session_id!r}")
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (name_hint or "out"))
        file_path = Path(ws) / "tool_outputs" / f"{safe}_{generate_id('spill')}.txt"
        await asyncio.to_thread(self._write_text, file_path, content)
        return str(file_path)

    @staticmethod
    def _write_text(file_path: Path, content: str) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    # ── CapabilityProvider 接口 ───────────────────────────────────────────────

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return list(_FS_TOOLS.values())

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        # 注入本次 session 的 workspace + allowed_dirs + runtime limits，供工具读取
        extra = dict(ctx.extra)
        ws = self._workspaces.get(ctx.session_id)
        if ws:
            extra["workspace"] = ws
        if self._cfg.allowed_dirs:
            extra["allowed_dirs"] = self._cfg.allowed_dirs
        extra["bash_idle_timeout_sec"] = self._cfg.bash_idle_timeout_sec
        extra["bash_hard_cap_sec"] = self._cfg.bash_hard_cap_sec
        extra["bash_max_output_bytes"] = self._cfg.bash_max_output_bytes
        extra["file_max_read_bytes"] = self._cfg.file_max_read_bytes
        extra["glob_max_results"] = self._cfg.glob_max_results
        ctx = dataclasses.replace(ctx, extra=extra)
        return self._dispatch(capability_id, arguments, ctx)

    async def _dispatch(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        name = capability_id.split(":")[-1]
        invoker = self._invokers.get(name)
        if invoker is None:
            yield CapabilityEvent(
                kind="error",
                payload={"code": "UNKNOWN_CAPABILITY", "message": f"Unknown: {capability_id}"},
            )
            return
        async for ev in invoker(arguments, ctx):
            yield ev

    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        pass

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(_FS_TOOLS),
            supports_streaming=True,
            supports_cancel=False,
            description=self.description,
        )
