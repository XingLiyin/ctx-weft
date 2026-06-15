import asyncio
from pathlib import Path

from loomex_core.providers.capability_skill_local.provider import LocalSkillCapabilityProvider
from loomex_core.protocols.context import ProviderContext


def _make_skill(tmp_path: Path, script_body: str) -> Path:
    skill = tmp_path / "enc-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: enc-skill\ndescription: t\n---\nbody\n", encoding="utf-8"
    )
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text(script_body, encoding="utf-8")
    return tmp_path


async def test_exec_script_sets_pythonioencoding(tmp_path, monkeypatch):
    skills_dir = _make_skill(
        tmp_path,
        "import os\nprint(os.environ.get('PYTHONIOENCODING', 'UNSET'))\n",
    )
    captured = {}
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)

    prov = LocalSkillCapabilityProvider(skills_dir)
    ctx = ProviderContext(session_id="s1")
    out = await prov.exec_script("enc-skill", "scripts/run.py", "", ctx)

    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert out.strip() == "utf-8"
