import asyncio

from ctx_weft.providers.capability_filesystem import provider as fsprov
from ctx_weft.protocols.context import ProviderContext


async def _collect(events):
    return [e async for e in events]


async def test_bash_exec_passes_pythonioencoding(monkeypatch):
    captured = {}
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    ctx = ProviderContext(session_id="s1")
    await _collect(fsprov.bash_exec("echo hi", ctx=ctx))

    assert captured["env"] is not None
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_description_has_path_guidance():
    desc = fsprov._bash_exec_description()
    # On Windows the description must carry the path/double-escape guidance.
    import platform
    if platform.system() == "Windows":
        assert "double-escape" in desc.lower()


async def test_bash_exec_merges_extra_env(monkeypatch):
    captured = {}
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    ctx = ProviderContext(session_id="s1", extra={"extra_env": {"SKILL_DIR": "X_MARK"}})
    await _collect(fsprov.bash_exec("echo hi", ctx=ctx))

    assert captured["env"]["SKILL_DIR"] == "X_MARK"
