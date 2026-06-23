"""skill exec_script 的 .py 解释器选择：配置注入 > 裸 python；非 .py 不受影响。"""

from pathlib import Path

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers._script_runner import RunResult
from ctx_weft.providers.capability_skill_local import provider as skillprov
from ctx_weft.providers.capability_skill_local.provider import (
    LocalSkillCapabilityProvider,
)


def _make_skill(tmp_path: Path, rel: str, body: str) -> Path:
    skill = tmp_path / "py-skill"
    if not skill.exists():
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: py-skill\ndescription: t\n---\nbody\n", encoding="utf-8"
        )
    p = skill / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return tmp_path


def _patch_capture(monkeypatch) -> dict:
    """拦截 run_with_liveness，捕获拼好的命令字符串，不真起进程。"""
    captured: dict = {}

    async def fake_run(command, **kw):
        captured["command"] = command
        return RunResult(
            stdout="ok", stderr="", exit_code=0,
            timed_out=False, timeout_kind=None, terminated_clean=True,
        )

    monkeypatch.setattr(skillprov, "run_with_liveness", fake_run)
    return captured


async def test_exec_script_uses_configured_python(tmp_path, monkeypatch):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "print('hi')\n")
    captured = _patch_capture(monkeypatch)
    prov = LocalSkillCapabilityProvider(skills_dir, python_executable=r"C:\rt\python.exe")
    await prov.exec_script("py-skill", "scripts/run.py", "", ProviderContext(session_id="s1"))
    assert captured["command"].startswith('"C:\\rt\\python.exe" "')
    assert captured["command"].endswith('run.py"')


async def test_exec_script_defaults_to_bare_python(tmp_path, monkeypatch):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "print('hi')\n")
    captured = _patch_capture(monkeypatch)
    prov = LocalSkillCapabilityProvider(skills_dir)  # 未配置 → 裸 python
    await prov.exec_script("py-skill", "scripts/run.py", "", ProviderContext(session_id="s1"))
    assert captured["command"].startswith('"python" "')


async def test_exec_script_non_py_ignores_python(tmp_path, monkeypatch):
    skills_dir = _make_skill(tmp_path, "scripts/run.sh", "echo hi\n")
    captured = _patch_capture(monkeypatch)
    prov = LocalSkillCapabilityProvider(skills_dir, python_executable=r"C:\rt\python.exe")
    await prov.exec_script("py-skill", "scripts/run.sh", "", ProviderContext(session_id="s1"))
    assert "python.exe" not in captured["command"]
    assert captured["command"].endswith('run.sh"')


async def test_exec_script_appends_args(tmp_path, monkeypatch):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "print('hi')\n")
    captured = _patch_capture(monkeypatch)
    prov = LocalSkillCapabilityProvider(skills_dir, python_executable=r"C:\rt\python.exe")
    await prov.exec_script("py-skill", "scripts/run.py", "--flag x", ProviderContext(session_id="s1"))
    assert captured["command"].endswith('run.py" --flag x')
