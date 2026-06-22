"""bash_exec 安全判定：分段扫全部命令词 + 拦命令替换。"""

import pytest

from ctx_weft.providers.capability_filesystem._bash_safety import (
    BASH_BLACKLIST,
    check_command_safety,
    command_word,
    split_segments,
)

# ── split_segments ─────────────────────────────────────────────────────────────


def test_split_segments_single():
    assert split_segments("echo ok") == ["echo ok"]


def test_split_segments_connectors():
    assert split_segments("a && b || c") == ["a ", " b ", " c"]
    assert split_segments("a; b | c & d") == ["a", " b ", " c ", " d"]


def test_split_segments_newline():
    assert split_segments("a\nb") == ["a", "b"]


# ── command_word 归一化 ─────────────────────────────────────────────────────────


def test_command_word_basic():
    assert command_word("echo ok") == "echo"


def test_command_word_case_insensitive():
    assert command_word("DEL x") == "del"


def test_command_word_strips_exe_suffix():
    assert command_word("del.exe y") == "del"
    assert command_word("foo.bat") == "foo"
    assert command_word("foo.cmd") == "foo"


def test_command_word_strips_dir_prefix_posix():
    assert command_word("/bin/rm x") == "rm"


def test_command_word_strips_dir_prefix_windows():
    assert command_word(r"C:\Windows\System32\del.exe y") == "del"


def test_command_word_skips_env_assignment():
    assert command_word("FOO=1 rm x") == "rm"
    assert command_word("FOO=1 BAR=2 rm x") == "rm"


def test_command_word_empty_segment():
    assert command_word("") is None
    assert command_word("   ") is None


# ── check_command_safety：放行 ──────────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "echo ok",
    "git status && echo done",
    "ls | head",
])
def test_check_allows_safe(cmd):
    assert check_command_safety(cmd) is None


# ── check_command_safety：拦截黑名单（任一段）────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "git status && rm -rf x",
    "echo a; del b",
    "cat f | sudo tee g",
    "/bin/rm x",
    r"C:\Windows\System32\del.exe y",
    "FOO=1 rm x",
])
def test_check_blocks_blacklisted_anywhere(cmd):
    msg = check_command_safety(cmd)
    assert msg is not None
    assert "not allowed" in msg


# ── check_command_safety：拦命令替换 ────────────────────────────────────────────


@pytest.mark.parametrize("cmd", [
    "echo $(rm x)",
    "echo `rm x`",
])
def test_check_blocks_command_substitution(cmd):
    msg = check_command_safety(cmd)
    assert msg is not None


def test_blacklist_is_frozenset():
    assert isinstance(BASH_BLACKLIST, frozenset)
    assert "rm" in BASH_BLACKLIST
