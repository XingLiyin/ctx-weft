"""window_text：一次一屏 + 续读提示；长文档不整份进上下文。"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator._text_window import TextWindowConfig, window_text

CFG = TextWindowConfig(default_lines=5, max_chars=64, max_line_chars=10)
LONG = "".join(f"line{i}\n" for i in range(1, 101))


def test_default_window_caps_lines_and_hints_next_page() -> None:
    out = window_text(LONG, cfg=CFG)
    assert "line5" in out and "line6" not in out
    assert "[Showing lines 1-5 of 100. Continue: read_file(path, offset=6)]" in out
    assert out.splitlines()[0] == "     1\tline1"          # 带行号


def test_offset_and_limit_page_forward() -> None:
    out = window_text(LONG, offset=6, limit=2, cfg=CFG)
    assert "line6" in out and "line7" in out
    assert "line5" not in out and "line8" not in out
    assert "offset=8" in out


def test_last_page_has_no_continuation_hint() -> None:
    out = window_text("a\nb\n", cfg=CFG)
    assert out == "     1\ta\n     2\tb"


def test_char_budget_stops_short_of_line_limit() -> None:
    # 每行 10 字符 → 64 字符预算在第 7 行前用尽（第 5 行已被 default_lines 截住之前）
    text = "".join("x" * 9 + "\n" for _ in range(20))
    out = window_text(text, limit=20, cfg=TextWindowConfig(max_chars=20, max_line_chars=10))
    assert "KB budget" in out and "offset=3" in out
    assert len(out.splitlines()) == 3          # 2 行内容 + 1 行提示


def test_over_long_line_truncated_with_char_continuation() -> None:
    text = "short\n" + "y" * 30 + "\ntail\n"
    out = window_text(text, cfg=CFG)
    assert "...[line truncated]" in out
    # 该行从第 6 个字符开始，展示了 10 个 → 余下从 16 续读，读到 36
    assert "read_file(path, char_offset=16)" in out
    assert "char_offset reaches 36" in out
    assert "resume line mode at read_file(path, offset=3)" in out

    rest = window_text(text, char_offset=16, char_limit=20, cfg=CFG)
    assert rest.startswith("y" * 20)


def test_empty_and_beyond_eof() -> None:
    assert window_text("", cfg=CFG) == "[empty file]"
    assert "beyond end of file (2 lines)" in window_text("a\nb\n", offset=9, cfg=CFG)
    assert "beyond end of file (length 4)" in window_text("a\nb\n", char_offset=99, cfg=CFG)


def test_invalid_args_raise() -> None:
    with pytest.raises(ValueError):
        window_text(LONG, offset=1, char_offset=0, cfg=CFG)
    with pytest.raises(ValueError):
        window_text(LONG, offset=0, cfg=CFG)
    with pytest.raises(ValueError):
        window_text(LONG, limit=0, cfg=CFG)
    with pytest.raises(ValueError):
        window_text(LONG, char_offset=-1, cfg=CFG)


def test_crlf_and_no_trailing_newline() -> None:
    out = window_text("a\r\nb", cfg=CFG)
    assert out == "     1\ta\n     2\tb"
