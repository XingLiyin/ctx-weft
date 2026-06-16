"""grep 的纯搜索逻辑：ripgrep 优先，回退纯 Python。

不依赖 ProviderContext / CapabilityEvent —— 只接受 (pattern, root, params, config)，
返回 GrepResult。与 provider.py 分离，使两种后端的输出格式可独立单测。

两种后端把命中归一成同一组「记录」，再交给同一个 formatter 渲染，从而保证输出格式
完全一致；文件筛选差异（ripgrep 默认跳过 .gitignore/二进制；Python 回退只跳过非
UTF-8 文件）写进 metadata.backend，不隐藏。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_NUL = b"\x00"
_BINARY_SNIFF_BYTES = 8192


@dataclass
class GrepConfig:
    max_results: int = 1000


@dataclass
class GrepResult:
    content: str
    metadata: dict


@dataclass
class _Line:
    """content 模式的一条记录。"""
    path: str
    lineno: int
    text: str
    is_match: bool


def _ripgrep_path() -> str | None:
    """ripgrep 可执行文件路径；未安装返回 None。测试通过 monkeypatch 此函数强制走 Python 后端。"""
    return shutil.which("rg")


# ── 公共入口 ──────────────────────────────────────────────────────────────────


def run_grep(
    pattern: str,
    root: Path,
    *,
    file_glob: str | None,
    output_mode: str,
    ignore_case: bool,
    before: int,
    after: int,
    cfg: GrepConfig,
) -> GrepResult:
    """在 root（文件或目录）下搜索 pattern，返回归一后的 GrepResult。

    output_mode: 'files_with_matches' | 'content'。content 模式下 before/after 为上下文行数。
    正则无法编译时（Python 后端）抛 ValueError，由调用方映射成 INVALID_ARGS。
    """
    rg = _ripgrep_path()
    backend = "ripgrep" if rg else "python"
    if output_mode == "files_with_matches":
        paths, truncated = (
            _rg_files(rg, pattern, root, file_glob, ignore_case, cfg.max_results)
            if rg
            else _py_files(pattern, root, file_glob, ignore_case, cfg.max_results)
        )
        content = "\n".join(paths) if paths else "(no matches)"
        return GrepResult(content=content, metadata={
            "mode": "files_with_matches", "backend": backend,
            "count": len(paths), "truncated": truncated,
        })

    lines, truncated, match_count = (
        _rg_content(rg, pattern, root, file_glob, ignore_case, before, after, cfg.max_results)
        if rg
        else _py_content(pattern, root, file_glob, ignore_case, before, after, cfg.max_results)
    )
    content = _format_content(lines) if lines else "(no matches)"
    return GrepResult(content=content, metadata={
        "mode": "content", "backend": backend,
        "count": match_count, "truncated": truncated,
    })


def _format_content(lines: list[_Line]) -> str:
    # 命中行 path:line:text；上下文行 path-line-text（对齐 ripgrep 约定）。
    return "\n".join(
        f"{ln.path}:{ln.lineno}:{ln.text}" if ln.is_match else f"{ln.path}-{ln.lineno}-{ln.text}"
        for ln in lines
    )


# ── ripgrep 后端 ──────────────────────────────────────────────────────────────


def _rg_base_args(pattern: str, root: Path, file_glob: str | None, ignore_case: bool) -> list[str]:
    args: list[str] = []
    if ignore_case:
        args.append("-i")
    if file_glob:
        args += ["--glob", file_glob]
    args += ["-e", pattern, "--", str(root)]
    return args


def _run_rg(rg: str, args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [rg, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode >= 2:  # 0=有命中 1=无命中 2+=错误
        raise RuntimeError(proc.stderr.strip() or f"ripgrep exited {proc.returncode}")
    return proc


def _rg_files(rg, pattern, root, file_glob, ignore_case, max_results) -> tuple[list[str], bool]:
    proc = _run_rg(rg, ["-l", *_rg_base_args(pattern, root, file_glob, ignore_case)])
    paths = sorted(p for p in proc.stdout.splitlines() if p)
    truncated = len(paths) > max_results
    return paths[:max_results], truncated


def _rg_content(rg, pattern, root, file_glob, ignore_case, before, after, max_results):
    args = ["--json"]
    if before:
        args += ["-B", str(before)]
    if after:
        args += ["-A", str(after)]
    proc = _run_rg(rg, [*args, *_rg_base_args(pattern, root, file_glob, ignore_case)])
    lines: list[_Line] = []
    match_count = 0
    truncated = False
    for raw in proc.stdout.splitlines():
        if not raw:
            continue
        evt = json.loads(raw)
        kind = evt.get("type")
        if kind not in ("match", "context"):
            continue
        data = evt["data"]
        is_match = kind == "match"
        if is_match and match_count >= max_results:
            truncated = True
            break
        lines.append(_Line(
            path=data["path"]["text"],
            lineno=data["line_number"],
            text=data["lines"]["text"].rstrip("\n"),
            is_match=is_match,
        ))
        if is_match:
            match_count += 1
    return lines, truncated, match_count


# ── Python 回退后端 ────────────────────────────────────────────────────────────


def _iter_files(root: Path, file_glob: str | None):
    if root.is_file():
        yield root
        return
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if file_glob and not fnmatch.fnmatch(name, file_glob):
                continue
            yield Path(dirpath) / name


def _read_text_lines(path: Path) -> list[str] | None:
    """读为按行切分的文本；非 UTF-8（视作二进制）返回 None。"""
    try:
        with path.open("rb") as f:
            head = f.read(_BINARY_SNIFF_BYTES)
            if _NUL in head:
                return None
        return path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None


def _py_files(pattern, root, file_glob, ignore_case, max_results) -> tuple[list[str], bool]:
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    found: list[str] = []
    truncated = False
    for path in _iter_files(root, file_glob):
        lines = _read_text_lines(path)
        if lines is None:
            continue
        if any(rx.search(line) for line in lines):
            if len(found) >= max_results:
                truncated = True
                break
            found.append(str(path))
    return sorted(found), truncated


def _py_content(pattern, root, file_glob, ignore_case, before, after, max_results):
    rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    out: list[_Line] = []
    match_count = 0
    truncated = False
    for path in _iter_files(root, file_glob):
        lines = _read_text_lines(path)
        if lines is None:
            continue
        emitted: set[int] = set()  # 已输出的行号（1-based），避免上下文重复
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            if match_count >= max_results:
                truncated = True
                return out, truncated, match_count
            lo = max(0, i - before)
            hi = min(len(lines), i + after + 1)
            for j in range(lo, hi):
                lineno = j + 1
                if lineno in emitted:
                    continue
                emitted.add(lineno)
                out.append(_Line(path=str(path), lineno=lineno, text=lines[j], is_match=(j == i)))
            match_count += 1
    return out, truncated, match_count
