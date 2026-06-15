from pathlib import Path
from loomex_core.providers.capability_skill_local.provider import LocalSkillCapabilityProvider


def test_skill_exec_param_defaults(tmp_path: Path):
    p = LocalSkillCapabilityProvider(tmp_path)
    assert p._script_timeout_sec == 60
    assert p._output_limit_chars == 65536


def test_skill_exec_param_override(tmp_path: Path):
    p = LocalSkillCapabilityProvider(tmp_path, script_timeout_sec=10, output_limit_chars=100)
    assert p._script_timeout_sec == 10
    assert p._output_limit_chars == 100
