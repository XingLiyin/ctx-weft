"""ULID 前缀 id 生成。

`evt_` / `tsk_` / `ses_` / `agt_` 等前缀是**对外可见的 id 形态**——host 与事件流都按
它认类型，改前缀等于改契约。
"""

from __future__ import annotations

from ulid import ULID

__all__ = ["generate_id"]


def generate_id(prefix: str) -> str:
    """Generate a ULID-based primary key (time-sortable + globally unique).

    Format: {prefix}_{ulid}, e.g. ses_01H8K9XPYJ7DRT2RY3JFXSF7M2
    """
    return f"{prefix}_{ULID()}"


# CJK 表意字 / 假名 / 谚文 / 全角标点等：这些脚本 len//4 会严重低估（真实约 0.6~1 token/字），
# 单列出来按更保守的每字 1.5 token 估。ASCII 起始都 < 0x3000，findall 走 C 级、对大文本仍快。
