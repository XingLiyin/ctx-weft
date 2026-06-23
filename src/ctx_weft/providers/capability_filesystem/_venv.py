"""Python .venv 自动引导：识别 python 类命令、布局 venv、懒创建、向 env 注入。

每次 bash_exec 是全新子进程，shell ``activate`` 不跨调用持久，故「激活」只能在子进程
env 层做：把 venv 的 bin 目录插到 PATH 头、设 VIRTUAL_ENV、删 PYTHONHOME，不改写命令、
不依赖 activate 脚本。创建用宿主 ``sys.executable -m venv``（品牌中性、不读 env）。

依赖 _bash_safety 的 split_segments/command_word 做分段解析（两者同为 bash_exec 内部件）。
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from pathlib import Path

from ctx_weft.providers.capability_filesystem._bash_safety import (
    command_word,
    split_segments,
)

PYTHON_COMMANDS: frozenset[str] = frozenset({"python", "python3", "py", "pip", "pip3"})

# 冻结态 GUI 后端建 venv 时会拉起 python.exe；Windows 上会闪黑窗。输出走管道、
# 子进程不需要控制台，故禁建窗口。非 Windows 无此常量 → 0（POSIX 忽略 creationflags）。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 按 workspace 串行化创建，避免并发 python 调用同时建同一个 .venv。
_VENV_LOCKS: dict[str, asyncio.Lock] = {}


class VenvError(RuntimeError):
    """venv 创建失败——视为前置条件，硬错误终止，不静默降级裸跑。"""


def command_is_python(command: str) -> bool:
    """任一段首词 ∈ PYTHON_COMMANDS。"""
    for segment in split_segments(command):
        if command_word(segment) in PYTHON_COMMANDS:
            return True
    return False


def venv_layout(workspace: Path, venv_dir: str) -> tuple[Path, Path, Path]:
    """返回 (venv_path, bindir, python_exe)。

    Windows: bindir=venv/Scripts, python_exe=python.exe
    POSIX:   bindir=venv/bin,     python_exe=python
    """
    venv_path = Path(workspace) / venv_dir
    if platform.system() == "Windows":
        bindir = venv_path / "Scripts"
        python_exe = bindir / "python.exe"
    else:
        bindir = venv_path / "bin"
        python_exe = bindir / "python"
    return venv_path, bindir, python_exe


def venv_env(env: dict[str, str], venv_path: Path) -> dict[str, str]:
    """返回注入了 venv 的 env 副本：PATH 头插 bindir、设 VIRTUAL_ENV、删 PYTHONHOME。"""
    venv_path = Path(venv_path)
    bindir = venv_path / ("Scripts" if platform.system() == "Windows" else "bin")
    new = dict(env)
    existing_path = new.get("PATH", "")
    new["PATH"] = str(bindir) + (os.pathsep + existing_path if existing_path else "")
    new["VIRTUAL_ENV"] = str(venv_path)
    new.pop("PYTHONHOME", None)
    return new


def _lock_for(workspace: Path) -> asyncio.Lock:
    key = os.path.abspath(str(workspace))
    lock = _VENV_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _VENV_LOCKS[key] = lock
    return lock


async def ensure_venv(
    workspace: Path, venv_dir: str, creator_python: str | None = None
) -> bool:
    """懒创建：python_exe 已存在则返回 False（无操作）；否则用 ``creator_python``
    （未给则回退宿主 ``sys.executable``）跑 ``-m venv`` 创建，返回 True。锁内二次检查
    存在性（double-checked）避免并发重复创建。创建失败 raise VenvError。

    creator_python：用于创建 venv 的「真 Python」解释器路径。打包（PyInstaller 冻结）
    形态下 ``sys.executable`` 是 app exe、无法 ``-m venv``，故由 host 注入随包内置的
    Python。给了但文件不存在 → 直接 VenvError（前置条件，不静默回退）。
    """
    venv_path, _, python_exe = venv_layout(workspace, venv_dir)
    if python_exe.exists():
        return False

    if creator_python is not None and not Path(creator_python).exists():
        raise VenvError(f"creator python not found: {creator_python}")
    python = creator_python or sys.executable

    async with _lock_for(workspace):
        if python_exe.exists():  # double-checked：等锁期间别人可能已建好
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                python, "-m", "venv", str(venv_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
            _, stderr = await proc.communicate()
        except Exception as e:  # 统一包成 VenvError 上抛
            raise VenvError(f"failed to spawn venv creation: {e}") from e

        if proc.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise VenvError(
                f"venv creation failed (exit {proc.returncode}) using {python}: {detail}"
            )
        if not python_exe.exists():
            raise VenvError(f"venv creation did not produce {python_exe}")
        return True
