"""Decode raw subprocess output bytes into text.

Subprocess stdout/stderr on Windows consoles is frequently GBK (the default
OEM codepage for zh-CN), while modern toolchains emit UTF-8. We try UTF-8
first (strict), fall back to GBK (strict), and finally UTF-8 with replacement
so the call can never raise. Applied on every OS: on POSIX the UTF-8 strict
path wins for well-formed output, and the GBK fallback only triggers for bytes
that are already not valid UTF-8.
"""

from __future__ import annotations


def decode_console(data: bytes) -> str:
    """Decode subprocess bytes via UTF-8 → GBK → UTF-8(replace)."""
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")
