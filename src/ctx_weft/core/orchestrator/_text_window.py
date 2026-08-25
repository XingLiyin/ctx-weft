"""skill_executor.read_file 的纯分页逻辑：行窗口 + 字符窗口。

技能自带的参考文档可以很长，整份塞进上下文会把窗口撑爆，所以 read_file 一次只回一屏，
并在尾部附上续读提示（下一页的 offset / char_offset）。

与 providers/capability_filesystem/_file_reader.py 是**两份平行实现**，口径刻意保持一致
（编号行、KB 预算、超长行截断、[Showing ...] 提示），改一处要想到另一处。不复用是因为：
那份按 Path 逐字节读，而 SkillCapabilityProvider.load_resource 的协议是整份返回 str
（skill 的字节可能根本不落在本地磁盘上），且 core 不依赖 providers。

不依赖 ProviderContext / CapabilityEvent —— 纯 (text, params, config) → str，可独立单测。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextWindowConfig:
    default_lines: int = 2000
    max_chars: int = 262_144
    max_line_chars: int = 4096


DEFAULT_TEXT_WINDOW = TextWindowConfig()


def _segments(text: str) -> list[str]:
    """按 \n 切段，保留原字符长度（用于算 char 偏移）；末尾换行不产生空行。"""
    segs = text.split("\n")
    if segs and segs[-1] == "":
        segs.pop()
    return segs


def _render(line_no: int, body: str) -> str:
    return f"{line_no:>6}\t{body}"


def window_text(
    text: str,
    *,
    offset: int | None = None,
    limit: int | None = None,
    char_offset: int | None = None,
    char_limit: int | None = None,
    cfg: TextWindowConfig = DEFAULT_TEXT_WINDOW,
) -> str:
    """取 text 的一个窗口，返回可直接回给模型的内容（含续读提示）。

    参数非法时抛 ValueError —— read_file 会把它转成 is_error 结果。
    """
    if char_offset is not None:
        if offset is not None or limit is not None:
            raise ValueError("specify either line offset or char_offset, not both")
        if char_offset < 0:
            raise ValueError("char_offset must be >= 0")
        if char_limit is not None and char_limit < 1:
            raise ValueError("char_limit must be >= 1")
        return _char_window(text, char_offset, char_limit, cfg)

    if offset is not None and offset < 1:
        raise ValueError("offset is 1-based; must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    return _line_window(text, offset, limit, cfg)


def _line_window(text: str, offset: int | None, limit: int | None, cfg: TextWindowConfig) -> str:
    start = 1 if offset is None else offset
    want = cfg.default_lines if limit is None else limit

    if not text:
        return "[empty file]"

    segs = _segments(text)
    total = len(segs)
    if start > total:
        return f"[offset {start} is beyond end of file ({total} lines). Nothing to show.]"

    pos = sum(len(s) + 1 for s in segs[: start - 1])   # 首行在原串中的字符偏移
    rendered: list[str] = []
    long_lines: list[dict] = []
    used = 0
    char_cap = False
    has_more = False
    next_offset: int | None = None

    for i in range(start - 1, total):
        if len(rendered) >= want:
            has_more = True
            next_offset = i + 1
            break
        seg = segs[i]
        shown = seg[: cfg.max_line_chars]
        truncated = len(seg) > cfg.max_line_chars
        # 首行永远进窗口：否则超预算的单行会让分页原地打转，拿不到任何进展。
        if rendered and used + len(shown) > cfg.max_chars:
            char_cap = True
            has_more = True
            next_offset = i + 1
            break
        body = shown.rstrip("\r")
        if truncated:
            body += " ...[line truncated]"
            long_lines.append({"line": i + 1, "char_start": pos,
                               "shown": len(shown), "char_end": pos + len(seg)})
        rendered.append(_render(i + 1, body))
        used += len(shown)
        pos += len(seg) + 1

    reminders: list[str] = []
    if has_more:
        end_line = start + len(rendered) - 1
        if char_cap:
            reminders.append(
                f"[Showing lines {start}-{end_line} of {total} "
                f"(page hit {cfg.max_chars // 1024}KB budget). "
                f"Continue: read_file(path, offset={next_offset})]")
        else:
            reminders.append(
                f"[Showing lines {start}-{end_line} of {total}. "
                f"Continue: read_file(path, offset={next_offset})]")
    for ll in long_lines:
        cont = ll["char_start"] + ll["shown"]
        reminders.append(
            f"[Line {ll['line']} truncated (showed {ll['shown']} of "
            f"{ll['char_end'] - ll['char_start']} chars). "
            f"Read its remainder by chars: read_file(path, char_offset={cont}), "
            f"paging until char_offset reaches {ll['char_end']}. "
            f"Then resume line mode at read_file(path, offset={ll['line'] + 1}).]")

    body_text = "\n".join(rendered)
    return body_text + ("\n" + "\n".join(reminders) if reminders else "")


def _char_window(text: str, char_offset: int, char_limit: int | None, cfg: TextWindowConfig) -> str:
    size = len(text)
    if char_offset >= size:
        return f"[char_offset {char_offset} is beyond end of file (length {size})]"
    n = cfg.max_chars if char_limit is None else min(char_limit, cfg.max_chars)
    chunk = text[char_offset : char_offset + n]
    end = char_offset + len(chunk)
    if end < size:
        chunk += (f"\n[Showing chars {char_offset}-{end} of {size}. "
                  f"Continue: read_file(path, char_offset={end})]")
    return chunk
