from ctx_weft.providers.capability_filesystem.provider import (
    FilesystemConfig, FilesystemToolsProvider,
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
    assert not hasattr(c, "file_max_read_bytes")


def test_fs_provider_holds_config():
    p = FilesystemToolsProvider(FilesystemConfig(glob_max_results=5))
    assert p._cfg.glob_max_results == 5
