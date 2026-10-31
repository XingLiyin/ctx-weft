"""Default skill command execution; resource ownership belongs to subclasses.

The shell runner keeps control of workspace selection and runtime setup. Without
one, commands run directly in the borrowed skill directory, as before.
"""

from __future__ import annotations

import dataclasses
import os
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from ctx_weft.protocols.capability import CapabilityEvent, SkillCapabilityProvider
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers._script_runner import run_with_liveness


class ShellSkillCapabilityProvider(SkillCapabilityProvider):
    """Execute full shell commands with SKILL_DIR pointing to borrowed resources.

    Subclasses implement the content APIs and borrow_skill_root(). The resource
    lease stays open until execution completes, fails, or is cancelled.
    """

    def __init__(
        self,
        *,
        script_timeout_sec: int = 60,
        idle_timeout_sec: float = 90,
        hard_cap_sec: float = 600,
        output_limit_chars: int = 65536,
        python_executable: str | None = None,
        bash_runner: Callable[[str, ProviderContext], AsyncIterator[CapabilityEvent]] | None = None,
    ) -> None:
        # Retained for constructor compatibility; liveness limits govern execution.
        self._script_timeout_sec = script_timeout_sec
        self._idle_timeout_sec = idle_timeout_sec
        self._hard_cap_sec = hard_cap_sec
        self._output_limit_chars = output_limit_chars
        self._python_executable = python_executable
        self._bash_runner = bash_runner

    @property
    def execution_instructions(self) -> str:
        """Resource-access guidance for definitions served by this shell provider.

        Override when an injected runner uses different shell syntax.
        """
        script = "%SKILL_DIR%/scripts/example.py" if os.name == "nt" else "$SKILL_DIR/scripts/example.py"
        return (
            "## Resource access\n\n"
            "SKILL_DIR points to this skill's resource directory during each command. "
            f'For example: `python "{script}" --input input.csv`. '
            "Quote resource paths and use SKILL_DIR again on each call. "
            "Save files you need to keep in the workspace."
        )

    @abstractmethod
    def borrow_skill_root(
        self, skill_name: str, ctx: ProviderContext,
    ) -> AbstractAsyncContextManager[Path]:
        """Borrow a local skill root; release temporary resources on context exit."""
        ...

    async def exec_script(
        self, skill_name: str, command: str, ctx: ProviderContext,
    ) -> str:
        if not command.strip():
            raise ValueError("command is required")
        async with self.borrow_skill_root(skill_name, ctx) as root:
            skill_root = root.resolve()
            if self._bash_runner is not None:
                return await self._exec_via_bash(skill_root, command, ctx)
            return await self._exec_direct(skill_root, command, ctx)

    async def _exec_via_bash(
        self, skill_root: Path, command: str, ctx: ProviderContext,
    ) -> str:
        assert self._bash_runner is not None
        extra = {
            **ctx.extra,
            "extra_env": {**(ctx.extra.get("extra_env") or {}), "SKILL_DIR": str(skill_root)},
            "bash_idle_timeout_sec": self._idle_timeout_sec,
            "bash_hard_cap_sec": self._hard_cap_sec,
            "bash_max_output_bytes": self._output_limit_chars,
        }
        execution_ctx = dataclasses.replace(ctx, extra=extra)
        content = ""
        exit_code = 0
        async for event in self._bash_runner(command, execution_ctx):
            if event.kind == "error":
                message = event.payload.get("message") or event.payload.get("code") or "skill exec failed"
                raise RuntimeError(message)
            if event.kind == "result":
                content = event.payload.get("content", "")
                exit_code = (event.payload.get("metadata") or {}).get("exit_code", 0)
        if exit_code != 0:
            raise RuntimeError(f"script exited with code {exit_code}\n{content[:self._output_limit_chars]}")
        return content[:self._output_limit_chars]

    async def _exec_direct(
        self, skill_root: Path, command: str, ctx: ProviderContext,
    ) -> str:
        env = {
            **os.environ,
            **(ctx.extra.get("extra_env") or {}),
            "SKILL_DIR": str(skill_root),
            "PYTHONIOENCODING": "utf-8",
        }
        # Configure executable lookup without rewriting the caller's shell command.
        if self._python_executable:
            python_dir = str(Path(self._python_executable).parent)
            env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
        result = await run_with_liveness(
            command,
            cwd=str(skill_root),
            env=env,
            idle_timeout_sec=self._idle_timeout_sec,
            hard_cap_sec=self._hard_cap_sec,
            output_limit_bytes=self._output_limit_chars,
        )
        stdout = result.stdout[:self._output_limit_chars]
        stderr = result.stderr[:self._output_limit_chars]
        if result.timed_out:
            survivor_note = (
                "" if result.terminated_clean
                else f" WARNING: {len(result.survivors)} process(es) may still be "
                     "running; outputs may be partial."
            )
            raise RuntimeError(
                f"exec_script timed out ({result.timeout_kind}).{survivor_note}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        if result.exit_code != 0:
            raise RuntimeError(
                f"script exited with code {result.exit_code}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        return stdout
