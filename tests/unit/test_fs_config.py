from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.capability_filesystem.provider import (
    FilesystemConfig,
    FilesystemToolsProvider,
)


def test_fs_config_defaults():
    c = FilesystemConfig()
    assert c.bash_idle_timeout_sec == 30
    assert c.bash_hard_cap_sec == 120
    assert c.bash_max_output_bytes == 50_000
    assert c.file_read_default_lines == 2000
    assert c.file_read_max_bytes == 262_144
    assert c.file_read_max_line_bytes == 4096
    assert c.file_read_count_max_bytes == 5_242_880
    assert c.glob_max_results == 500
    assert c.bash_auto_venv is True
    assert c.bash_venv_dir == ".venv"
    assert c.bash_venv_python is None
    assert not hasattr(c, "file_max_read_bytes")


def test_fs_provider_holds_config():
    p = FilesystemToolsProvider(FilesystemConfig(glob_max_results=5))
    assert p._cfg.glob_max_results == 5


def test_invoke_injects_venv_config(tmp_path):
    p = FilesystemToolsProvider(FilesystemConfig(
        bash_auto_venv=False, bash_venv_dir="venv", bash_venv_python="/x/py"
    ))
    p.register_session("s1", str(tmp_path))
    captured = {}

    async def fake_dispatch(cap_id, args, ctx):
        captured["extra"] = ctx.extra
        return
        yield  # pragma: no cover — make it an async generator

    p._dispatch = fake_dispatch  # type: ignore[method-assign]
    ctx = ProviderContext(session_id="s1")
    gen = p.invoke("fs:bash_exec", {"command": "echo hi"}, ctx)

    import asyncio

    async def drain():
        async for _ in gen:
            pass

    asyncio.run(drain())
    assert captured["extra"]["bash_auto_venv"] is False
    assert captured["extra"]["bash_venv_dir"] == "venv"
    assert captured["extra"]["bash_venv_python"] == "/x/py"
