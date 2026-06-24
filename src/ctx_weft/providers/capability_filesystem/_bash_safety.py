"""bash_exec 安全判定：分段扫全部命令词 + 拦命令替换。

把黑名单的「定义、解析、判定」聚于一处，与 provider.py 分离，使判定可独立单测。
判定范围从「只看 tokens[0]」扩到「拆出每一段、各取真实命令词」，从而消解链式/管道/
绝对路径/.exe 后缀/环境前缀几类绕过；命令替换 $(...)/反引号 直接拦截（不扫内部）。
"""

from __future__ import annotations

import re
import shlex

BASH_BLACKLIST: frozenset[str] = frozenset([
    "rm", "rmdir", "del", "format", "mkfs", "dd",
    "shutdown", "reboot", "halt", "poweroff",
    "passwd", "sudo", "su", "chmod", "chown",
    "crontab", "at", "nohup",
    "wget", "curl",
])

# shell 连接符：每个都会令其右侧另起一条命令。长操作符优先（&& 先于 &，|| 先于 |）。
_SEGMENT_SEP = re.compile(r"&&|\|\||;|\||&|\n")

# POSIX 环境赋值前缀：NAME=...（NAME 为标识符）。
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")

# 命令替换：$(...) 或反引号。
_CMD_SUBST = re.compile(r"\$\(|`")

# 可执行后缀（Windows），归一时剥除。
_EXE_SUFFIXES = (".exe", ".bat", ".cmd")


def split_segments(command: str) -> list[str]:
    """按 shell 连接符把命令拆成会各自起一条命令的段。

    连接符：``;  &&  ||  |  &`` 以及换行。段内空白保留（由 command_word 归一）。
    """
    return _SEGMENT_SEP.split(command)


def command_word(segment: str) -> str | None:
    """取一段里真正的命令词并归一化，空段返回 None。

    1. ``shlex.split`` （posix=False 以保留 Windows 反斜杠路径；失败回退 ``str.split``）
    2. 跳过前导 ``VAR=val`` 环境赋值 token
    3. 取首个真实 token → basename（剥 ``/bin``、``C:\\...\\``）→ 去 ``.exe/.bat/.cmd`` → ``lower()``
    """
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        tokens = segment.split()

    word: str | None = None
    for tok in tokens:
        if _ENV_ASSIGN.match(tok):
            continue
        word = tok
        break
    if not word:
        return None

    base = word.replace("\\", "/").split("/")[-1]
    lowered = base.lower()
    for suffix in _EXE_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return lowered or None


def check_command_safety(
    command: str, blacklist: frozenset[str] = BASH_BLACKLIST
) -> str | None:
    """返回错误消息字符串（命中）或 None（放行）。

    - 含命令替换 ``$(...)`` 或反引号 → 返回拦截原因
    - 逐段取命令词，命中 ``blacklist`` → 返回 ``Command '<word>' is not allowed``
    """
    if _CMD_SUBST.search(command):
        return "Command substitution ($(...) or backticks) is not allowed"
    for segment in split_segments(command):
        word = command_word(segment)
        if word and word in blacklist:
            return f"Command '{word}' is not allowed"
    return None
