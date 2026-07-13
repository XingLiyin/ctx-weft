import asyncio

from ctx_weft.protocols.context import ProviderContext
from ctx_weft.providers.capability_filesystem.provider import (
    FilesystemConfig,
    FilesystemToolsProvider,
)


def test_fs_config_defaults():
    c = FilesystemConfig()
    assert c.bash_idle_timeout_sec == 30
    assert c.bash_hard_cap_sec == 3600
    assert c.bash_max_output_bytes == 50_000
    assert c.file_read_default_lines == 500
    assert c.file_read_max_bytes == 20_480
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


def _capture_invoke_extra(provider, ctx) -> dict:
    """跑一次 invoke,捕获派发时的 ctx.extra(用 fake _dispatch,不真执行工具)。"""
    captured = {}

    async def fake_dispatch(cap_id, args, ctx):
        captured["extra"] = ctx.extra
        return
        yield  # pragma: no cover — make it an async generator

    provider._dispatch = fake_dispatch  # type: ignore[method-assign]
    gen = provider.invoke("fs:shell", {"command": "echo hi"}, ctx)

    async def drain():
        async for _ in gen:
            pass

    asyncio.run(drain())
    return captured["extra"]


def test_invoke_injects_venv_config(tmp_path):
    p = FilesystemToolsProvider(FilesystemConfig(
        bash_auto_venv=False, bash_venv_dir="venv", bash_venv_python="/x/py"
    ))
    p.register_session("s1", str(tmp_path))
    extra = _capture_invoke_extra(p, ProviderContext(session_id="s1"))
    assert extra["bash_auto_venv"] is False
    assert extra["bash_venv_dir"] == "venv"
    assert extra["bash_venv_python"] == "/x/py"


def test_invoke_fills_limit_defaults_from_config(tmp_path):
    # 调用方未给限额 → 用 fs 配置(此处即默认值)。
    p = FilesystemToolsProvider(FilesystemConfig())
    p.register_session("s1", str(tmp_path))
    extra = _capture_invoke_extra(p, ProviderContext(session_id="s1"))
    assert extra["bash_idle_timeout_sec"] == 30
    assert extra["bash_hard_cap_sec"] == 3600
    assert extra["bash_max_output_bytes"] == 50_000


def test_invoke_lets_caller_override_timeouts(tmp_path):
    p = FilesystemToolsProvider(FilesystemConfig())  # config defaults 30/3600/50000
    p.register_session("s1", str(tmp_path))
    ctx = ProviderContext(session_id="s1", extra={
        "bash_idle_timeout_sec": 11,
        "bash_hard_cap_sec": 999,
        "bash_max_output_bytes": 123,
    })
    extra = _capture_invoke_extra(p, ctx)
    assert extra["bash_idle_timeout_sec"] == 11          # all three overridable
    assert extra["bash_hard_cap_sec"] == 999
    assert extra["bash_max_output_bytes"] == 123
    assert extra["bash_venv_python"] is None             # fs-forced key still set
