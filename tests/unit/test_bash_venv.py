"""Python .venv 自动引导：识别 / 布局 / 懒创建 / env 注入。"""

import os
import platform
import sys

import pytest

from ctx_weft.providers.capability_filesystem import _venv
from ctx_weft.providers.capability_filesystem._venv import (
    PYTHON_COMMANDS,
    VenvError,
    command_is_python,
    ensure_venv,
    venv_env,
    venv_layout,
)

# ── command_is_python ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "python a.py",
    "pip install x",
    "py -3 a.py",
    "PY a.py",
    "echo hi && python a.py",
])
def test_command_is_python_true(cmd):
    assert command_is_python(cmd) is True


@pytest.mark.parametrize("cmd", [
    "node a.js",
    "pytest",
    "git status",
])
def test_command_is_python_false(cmd):
    assert command_is_python(cmd) is False


def test_python_commands_membership():
    assert PYTHON_COMMANDS == {"python", "python3", "py", "pip", "pip3"}


# ── venv_layout ─────────────────────────────────────────────────────────────────


def test_venv_layout_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    venv_path, bindir, python_exe = venv_layout(tmp_path, ".venv")
    assert venv_path == tmp_path / ".venv"
    assert bindir == tmp_path / ".venv" / "Scripts"
    assert python_exe == tmp_path / ".venv" / "Scripts" / "python.exe"


def test_venv_layout_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    _, bindir, python_exe = venv_layout(tmp_path, ".venv")
    assert bindir == tmp_path / ".venv" / "bin"
    assert python_exe == tmp_path / ".venv" / "bin" / "python"


# ── venv_env ────────────────────────────────────────────────────────────────────


def test_venv_env_injects(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    venv_path = tmp_path / ".venv"
    base = {"PATH": "/usr/bin", "PYTHONHOME": "/old", "FOO": "bar"}
    out = venv_env(base, venv_path)

    bindir = str(venv_path / "bin")
    assert out["PATH"].split(os.pathsep)[0] == bindir
    assert "/usr/bin" in out["PATH"]
    assert out["VIRTUAL_ENV"] == str(venv_path)
    assert "PYTHONHOME" not in out
    assert out["FOO"] == "bar"
    # original untouched
    assert "PYTHONHOME" in base


def test_venv_env_no_existing_path(tmp_path):
    out = venv_env({}, tmp_path / ".venv")
    assert "VIRTUAL_ENV" in out
    assert out["PATH"]


# ── ensure_venv ─────────────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode: int, make_exe=None):
        self.returncode = returncode
        self._make_exe = make_exe

    async def communicate(self):
        if self._make_exe:
            self._make_exe()
        return (b"", b"" if self.returncode == 0 else b"boom")


async def test_ensure_venv_first_creates_then_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    _, _, python_exe = venv_layout(tmp_path, ".venv")
    calls = {"n": 0}

    async def fake_exec(*args, **kwargs):
        calls["n"] += 1

        def make_exe():
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text("#!fake")

        return _FakeProc(0, make_exe)

    monkeypatch.setattr(_venv.asyncio, "create_subprocess_exec", fake_exec)

    created = await ensure_venv(tmp_path, ".venv")
    assert created is True
    assert calls["n"] == 1
    assert python_exe.exists()

    again = await ensure_venv(tmp_path, ".venv")
    assert again is False
    assert calls["n"] == 1  # no second subprocess


async def test_ensure_venv_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(1)  # nonzero, no exe produced

    monkeypatch.setattr(_venv.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(VenvError):
        await ensure_venv(tmp_path, ".venv")


async def test_ensure_venv_uses_creator_python(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    _, _, python_exe = venv_layout(tmp_path, ".venv")
    seen = {}

    # Create a real file for creator_python so the existence pre-check passes
    fake_creator = tmp_path / "fake_python"
    fake_creator.write_text("#!/fake/python")
    creator_python_path = str(fake_creator)

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args

        def make_exe():
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text("#!fake")

        return _FakeProc(0, make_exe)

    monkeypatch.setattr(_venv.asyncio, "create_subprocess_exec", fake_exec)

    created = await ensure_venv(tmp_path, ".venv", creator_python=creator_python_path)
    assert created is True
    assert seen["argv"][0] == creator_python_path   # not sys.executable
    assert seen["argv"][1:3] == ("-m", "venv")


async def test_ensure_venv_defaults_to_sys_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    _, _, python_exe = venv_layout(tmp_path, ".venv")
    seen = {}

    async def fake_exec(*args, **kwargs):
        seen["argv"] = args

        def make_exe():
            python_exe.parent.mkdir(parents=True, exist_ok=True)
            python_exe.write_text("#!fake")

        return _FakeProc(0, make_exe)

    monkeypatch.setattr(_venv.asyncio, "create_subprocess_exec", fake_exec)

    await ensure_venv(tmp_path, ".venv")  # creator_python omitted
    assert seen["argv"][0] == sys.executable


async def test_ensure_venv_missing_creator_python_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    with pytest.raises(VenvError, match="creator python not found"):
        await ensure_venv(tmp_path, ".venv", creator_python="/no/such/python")


# ── 集成：bash_exec venv 引导 ───────────────────────────────────────────────────


import asyncio  # noqa: E402

from ctx_weft.protocols.context import ProviderContext  # noqa: E402
from ctx_weft.providers.capability_filesystem import provider as fsprov  # noqa: E402


async def _collect(events):
    return [e async for e in events]


def _spy_shell_env(monkeypatch, captured):
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)


async def test_bash_exec_python_triggers_venv(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_ensure(workspace, venv_dir):
        calls["n"] += 1
        return True

    monkeypatch.setattr(fsprov, "ensure_venv", fake_ensure)
    captured: dict = {}
    _spy_shell_env(monkeypatch, captured)

    ctx = ProviderContext(session_id="s1", extra={
        "workspace": str(tmp_path), "bash_auto_venv": True, "bash_venv_dir": ".venv",
    })
    await _collect(fsprov.bash_exec("python -V", ctx=ctx))

    assert calls["n"] == 1
    assert captured["env"]["VIRTUAL_ENV"] == str(tmp_path / ".venv")


async def test_bash_exec_python_disabled(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_ensure(workspace, venv_dir):
        calls["n"] += 1
        return True

    monkeypatch.setattr(fsprov, "ensure_venv", fake_ensure)
    captured: dict = {}
    _spy_shell_env(monkeypatch, captured)

    ctx = ProviderContext(session_id="s1", extra={
        "workspace": str(tmp_path), "bash_auto_venv": False,
    })
    await _collect(fsprov.bash_exec("python -V", ctx=ctx))

    assert calls["n"] == 0
    # 不注入我们的 venv（继承的 os.environ 可能已带外层 VIRTUAL_ENV，故比对路径而非缺省）
    assert captured["env"].get("VIRTUAL_ENV") != str(tmp_path / ".venv")


async def test_bash_exec_non_python_no_venv(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_ensure(workspace, venv_dir):
        calls["n"] += 1
        return True

    monkeypatch.setattr(fsprov, "ensure_venv", fake_ensure)
    captured: dict = {}
    _spy_shell_env(monkeypatch, captured)

    ctx = ProviderContext(session_id="s1", extra={
        "workspace": str(tmp_path), "bash_auto_venv": True,
    })
    await _collect(fsprov.bash_exec("echo hi", ctx=ctx))

    assert calls["n"] == 0
    assert captured["env"].get("VIRTUAL_ENV") != str(tmp_path / ".venv")


async def test_bash_exec_venv_error_surfaced(tmp_path, monkeypatch):
    async def fake_ensure(workspace, venv_dir):
        raise VenvError("disk full")

    monkeypatch.setattr(fsprov, "ensure_venv", fake_ensure)

    ctx = ProviderContext(session_id="s1", extra={
        "workspace": str(tmp_path), "bash_auto_venv": True,
    })
    events = await _collect(fsprov.bash_exec("python -V", ctx=ctx))
    errors = [e for e in events if e.kind == "error"]
    assert any(e.payload.get("code") == "VENV_ERROR" for e in errors)


async def test_bash_exec_blocks_chained_blacklist(tmp_path):
    ctx = ProviderContext(session_id="s1", extra={"workspace": str(tmp_path)})
    events = await _collect(fsprov.bash_exec("echo a && rm -rf x", ctx=ctx))
    errors = [e for e in events if e.kind == "error"]
    assert any(e.payload.get("code") == "COMMAND_BLACKLISTED" for e in errors)
