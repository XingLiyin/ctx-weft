"""Full skill commands preserve shell syntax and borrow resources for execution."""

import os
import sys
from pathlib import Path

import pytest

from ctx_weft.protocols.capability import CapabilityEvent
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers._script_runner import RunResult
from ctx_weft.providers.capability_filesystem import FilesystemConfig, FilesystemToolsProvider
from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider
from ctx_weft.providers.capability_skill_shell import provider as shellprov


def _make_skill(tmp_path: Path) -> Path:
    skill = tmp_path / "skill with spaces"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: py-skill\ndescription: t\n---\nbody\n", encoding="utf-8"
    )
    return skill


def _patch_capture(monkeypatch) -> dict:
    captured = {}

    async def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return RunResult(
            stdout="ok", stderr="", exit_code=0,
            timed_out=False, timeout_kind=None, terminated_clean=True,
        )

    monkeypatch.setattr(shellprov, "run_with_liveness", fake_run)
    return captured


async def test_direct_command_and_configured_python_environment(tmp_path, monkeypatch):
    root = _make_skill(tmp_path)
    captured = _patch_capture(monkeypatch)
    python_exe = tmp_path / "runtime" / "python.exe"
    provider = LocalSkillCapabilityProvider(tmp_path, python_executable=str(python_exe))
    command = 'python -c "print(42)" | other-tool > output.txt && echo done'
    ctx = ProviderContext(session_id="s1", extra={"extra_env": {"FOO": "bar"}})
    assert await provider.exec_script("py-skill", command, ctx) == "ok"
    assert captured["command"] == command
    assert captured["cwd"] == str(root.resolve())
    assert captured["env"]["SKILL_DIR"] == str(root.resolve())
    assert captured["env"]["PATH"].split(os.pathsep)[0] == str(python_exe.parent)
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["env"]["FOO"] == "bar"


def _result_runner(content="OUT", exit_code=0):
    async def runner(command, ctx):
        runner.command = command
        runner.ctx = ctx
        yield CapabilityEvent(kind="result", payload={
            "content": content, "metadata": {"exit_code": exit_code},
        })
    return runner


async def test_runner_preserves_command_context_and_skill_limits(tmp_path):
    root = _make_skill(tmp_path)
    runner = _result_runner("OUT")
    provider = LocalSkillCapabilityProvider(
        tmp_path, bash_runner=runner, idle_timeout_sec=12, hard_cap_sec=123,
        output_limit_chars=1024,
    )
    ctx = ProviderContext(
        session_id="s1", task_id="t1", invocation_id="i1",
        extra={"workspace": "existing-workspace", "extra_env": {"FOO": "bar", "SKILL_DIR": "wrong"}},
    )
    command = 'node "$SKILL_DIR/scripts/run.js" | formatter > out.txt && echo done'
    assert await provider.exec_script("py-skill", command, ctx) == "OUT"
    assert runner.command == command
    assert runner.ctx.session_id == ctx.session_id
    assert runner.ctx.task_id == ctx.task_id
    assert runner.ctx.invocation_id == ctx.invocation_id
    assert runner.ctx.extra["workspace"] == "existing-workspace"
    assert runner.ctx.extra["extra_env"] == {"FOO": "bar", "SKILL_DIR": str(root.resolve())}
    assert runner.ctx.extra["bash_idle_timeout_sec"] == 12
    assert runner.ctx.extra["bash_hard_cap_sec"] == 123
    assert runner.ctx.extra["bash_max_output_bytes"] == 1024
    assert ctx.extra["extra_env"]["SKILL_DIR"] == "wrong"
    assert "bash_hard_cap_sec" not in ctx.extra
    assert root.exists()  # The local provider never deletes its source.


async def test_runner_error_and_nonzero_exit_remain_failures(tmp_path):
    _make_skill(tmp_path)

    async def error_runner(command, ctx):
        yield CapabilityEvent(kind="error", payload={"code": "TIMEOUT", "message": "boom"})

    provider = LocalSkillCapabilityProvider(tmp_path, bash_runner=error_runner)
    with pytest.raises(RuntimeError, match="boom"):
        await provider.exec_script("py-skill", "echo x", ProviderContext(session_id="s1"))
    provider = LocalSkillCapabilityProvider(tmp_path, bash_runner=_result_runner("err", exit_code=2))
    with pytest.raises(RuntimeError, match="exited with code 2"):
        await provider.exec_script("py-skill", "echo x", ProviderContext(session_id="s1"))


async def test_output_limit_and_empty_command(tmp_path):
    _make_skill(tmp_path)
    runner = _result_runner("123456")
    provider = LocalSkillCapabilityProvider(tmp_path, bash_runner=runner, output_limit_chars=3)
    ctx = ProviderContext(session_id="s1")
    assert await provider.exec_script("py-skill", "echo x", ctx) == "123"
    with pytest.raises(ValueError, match="command is required"):
        await provider.exec_script("missing-skill", " \n", ctx)


@pytest.mark.parametrize("via_runner", [False, True])
async def test_real_resource_command_pipeline_and_workspace(tmp_path, via_runner):
    root = _make_skill(tmp_path)
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "read.py").write_text(
        "from pathlib import Path\nprint(Path('input.txt').read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cwd = workspace if via_runner else root
    (cwd / "input.txt").write_text("skill-resource-42", encoding="utf-8")
    fs = FilesystemToolsProvider(FilesystemConfig(bash_auto_venv=False))
    fs.register_session("s1", str(workspace))

    def runner(command, ctx):
        return fs.invoke("fs:shell", {"command": command}, ctx)

    skill_script = r'%SKILL_DIR%\scripts\read.py' if os.name == "nt" else "$SKILL_DIR/scripts/read.py"
    command = (
        f'"{sys.executable}" "{skill_script}" '
        f'| "{sys.executable}" -c "import sys; print(sys.stdin.read().strip().upper())" '
        '> output.txt && echo complete'
    )
    provider = LocalSkillCapabilityProvider(tmp_path, bash_runner=runner if via_runner else None)
    output = await provider.exec_script("py-skill", command, ProviderContext(session_id="s1"))
    assert "complete" in output
    assert (cwd / "output.txt").read_text().strip() == "SKILL-RESOURCE-42"
    assert root.exists()
    if via_runner:
        assert not (root / "output.txt").exists()
