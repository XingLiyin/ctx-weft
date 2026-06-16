"""read_file 的纯分页逻辑：行模式 + 字节窗口模式。

不依赖 ProviderContext / CapabilityEvent —— 只接受 (path, params, config)，返回 ReadResult。
与 provider.py 分离，使分页不变式可独立单测（spec §2 的前进保证）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_NUL = b"\x00"
_BINARY_SNIFF_BYTES = 8192


@dataclass
class ReadConfig:
    default_lines: int = 2000
    max_bytes: int = 262_144
    max_line_bytes: int = 4096
    count_max_bytes: int = 5_242_880


@dataclass
class ReadResult:
    content: str
    metadata: dict


def _is_binary(path: Path) -> bool:
    with path.open("rb") as f:
        return _NUL in f.read(_BINARY_SNIFF_BYTES)


def _count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


def _render(line_no: int, text: str) -> str:
    return f"{line_no:>6}\t{text}"


def read_lines(path: Path, offset: int | None, limit: int | None, cfg: ReadConfig) -> ReadResult:
    start_line = 1 if offset is None else offset
    want = cfg.default_lines if limit is None else limit
    st = path.stat()
    file_size, mtime = st.st_size, st.st_mtime

    if _is_binary(path):
        return ReadResult(
            f"[binary file, {file_size} bytes - not readable as text; use bash]",
            {"mode": "lines", "path": str(path), "binary": True, "file_size": file_size,
             "mtime": mtime, "has_more": False, "next_offset": None, "total_lines": None},
        )

    # 注意：total_lines 来自独立扫描，若文件在两次读之间被改写，total_lines 可能与本次
    # 渲染的行不一致（TOCTOU）。这是无状态读取的固有局限；调用方可用 metadata 的 mtime/
    # file_size 指纹察觉文件变化（spec §10）。
    total_lines = _count_lines(path) if file_size <= cfg.count_max_bytes else None

    rendered: list[str] = []
    long_lines: list[dict] = []
    used = 0
    byte_cap = False
    cur = 0            # 已读到的最后行号
    bpos = 0           # 已读到的最后行尾字节偏移
    next_offset: int | None = None
    has_more = False

    with path.open("rb") as f:
        # 跳到 start_line（不足则停在 EOF）
        while cur < start_line - 1:
            raw = f.readline()
            if not raw:
                break
            cur += 1
            bpos += len(raw)
        # 采集窗口
        while len(rendered) < want:
            line_start = bpos
            raw = f.readline()
            if not raw:
                break
            line_no = cur + 1
            shown = raw[: cfg.max_line_bytes]
            truncated = len(raw) > cfg.max_line_bytes
            if rendered and used + len(shown) > cfg.max_bytes:
                byte_cap = True
                has_more = True
                next_offset = line_no          # 本行成为下一页首行
                break
            cur = line_no
            bpos += len(raw)
            text = shown.decode("utf-8", "replace").rstrip("\r\n")
            if truncated:
                text += " ...[line truncated]"
                long_lines.append({"line": line_no, "byte_start": line_start,
                                   "shown_bytes": len(shown), "byte_end": bpos,
                                   "line_bytes": len(raw)})
            rendered.append(_render(line_no, text))
            used += len(shown)
        # 未因 byte_cap 停止时，探一行判断 has_more
        if not has_more and f.readline():
            has_more = True
            next_offset = cur + 1

    if not rendered:
        if start_line <= 1 and file_size == 0:
            body = "[empty file]"
        else:
            shown_total = total_lines if total_lines is not None else cur
            body = (f"[offset {start_line} is beyond end of file "
                    f"({shown_total} lines). Nothing to show.]")
        return ReadResult(body, {
            "mode": "lines", "path": str(path), "start_line": start_line,
            "end_line": start_line - 1, "line_count": 0, "total_lines": total_lines,
            "has_more": False, "next_offset": None, "bytes_returned": 0,
            "truncation": {"byte_cap": False, "long_lines": []},
            "file_size": file_size, "mtime": mtime,
        })

    body = "\n".join(rendered)
    reminders: list[str] = []
    if has_more:
        of = f" of {total_lines}" if total_lines is not None else ""
        if byte_cap:
            reminders.append(
                f"[Showing lines {start_line}-{cur}{of} (page hit {cfg.max_bytes // 1024}KB budget). "
                f"Continue: read_file(path, offset={next_offset})]")
        else:
            reminders.append(
                f"[Showing lines {start_line}-{cur}{of}. "
                f"Continue: read_file(path, offset={next_offset})]")
    for ll in long_lines:
        cont = ll["byte_start"] + ll["shown_bytes"]
        reminders.append(
            f"[Line {ll['line']} truncated (showed {ll['shown_bytes']} of {ll['line_bytes']} bytes). "
            f"Read its remainder by bytes: read_file(path, byte_offset={cont}), "
            f"paging until byte_offset reaches {ll['byte_end']}. "
            f"Then resume line mode at read_file(path, offset={ll['line'] + 1}).]")
    content = body + ("\n" + "\n".join(reminders) if reminders else "")

    return ReadResult(content, {
        "mode": "lines", "path": str(path), "start_line": start_line, "end_line": cur,
        "line_count": len(rendered), "total_lines": total_lines, "has_more": has_more,
        "next_offset": next_offset, "bytes_returned": used,
        "truncation": {"byte_cap": byte_cap, "long_lines": long_lines},
        "file_size": file_size, "mtime": mtime,
    })


def read_byte_window(
    path: Path, byte_offset: int, byte_limit: int | None, cfg: ReadConfig
) -> ReadResult:
    st = path.stat()
    file_size, mtime = st.st_size, st.st_mtime
    n = cfg.max_bytes if byte_limit is None else min(byte_limit, cfg.max_bytes)

    if byte_offset >= file_size:
        return ReadResult(
            f"[byte_offset {byte_offset} is beyond end of file (size {file_size})]",
            {"mode": "bytes", "path": str(path), "byte_start": byte_offset,
             "byte_end": byte_offset, "next_byte_offset": None, "has_more": False,
             "bytes_returned": 0, "file_size": file_size, "mtime": mtime},
        )

    with path.open("rb") as f:
        f.seek(byte_offset)
        chunk = f.read(n)
    end = byte_offset + len(chunk)
    has_more = end < file_size
    next_byte_offset = end if has_more else None

    content = chunk.decode("utf-8", "replace")
    if has_more:
        content += (f"\n[Showing bytes {byte_offset}-{end} of {file_size}. "
                    f"Continue: read_file(path, byte_offset={next_byte_offset})]")

    return ReadResult(content, {
        "mode": "bytes", "path": str(path), "byte_start": byte_offset, "byte_end": end,
        "next_byte_offset": next_byte_offset, "has_more": has_more,
        "bytes_returned": len(chunk), "file_size": file_size, "mtime": mtime,
    })
