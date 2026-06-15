from ctx_weft.providers.capability_filesystem.provider import (
    FilesystemConfig, FilesystemToolsProvider,
)


def test_fs_config_defaults():
    c = FilesystemConfig()
    assert c.bash_idle_timeout_sec == 30
    assert c.bash_hard_cap_sec == 120
    assert c.bash_max_output_bytes == 50_000
    assert c.file_max_read_bytes == 500_000
    assert c.glob_max_results == 500


def test_fs_provider_holds_config():
    p = FilesystemToolsProvider(FilesystemConfig(glob_max_results=5))
    assert p._cfg.glob_max_results == 5
