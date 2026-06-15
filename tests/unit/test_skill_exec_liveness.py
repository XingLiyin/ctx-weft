from pathlib import Path

import pytest

from ctx_weft.providers.capability_skill_local.provider import LocalSkillCapabilityProvider
from ctx_weft.protocols.context import ProviderContext


def _make_skill(tmp_path: Path, body: str) -> Path:
    skill = tmp_path / "live-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: live-skill\ndescription: t\n---\nb\n", encoding="utf-8"
    )
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text(body, encoding="utf-8")
    return tmp_path


async def test_idle_timeout_reports_honestly(tmp_path):
    skills_dir = _make_skill(tmp_path, "import time\ntime.sleep(60)\n")
    prov = LocalSkillCapabilityProvider(skills_dir, idle_timeout_sec=1.0, hard_cap_sec=30)
    ctx = ProviderContext(session_id="s1")
    with pytest.raises(RuntimeError) as exc:
        await prov.exec_script("live-skill", "scripts/run.py", "", ctx)
    msg = str(exc.value).lower()
    assert "idle" in msg or "timed out" in msg


async def test_normal_script_returns_stdout(tmp_path):
    skills_dir = _make_skill(tmp_path, "print('done-42')\n")
    prov = LocalSkillCapabilityProvider(skills_dir, idle_timeout_sec=10, hard_cap_sec=30)
    ctx = ProviderContext(session_id="s1")
    out = await prov.exec_script("live-skill", "scripts/run.py", "", ctx)
    assert "done-42" in out
