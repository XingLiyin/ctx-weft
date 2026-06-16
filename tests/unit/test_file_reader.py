from pathlib import Path

from ctx_weft.providers.capability_filesystem._file_reader import (
    ReadConfig,
    read_byte_window,
    read_lines,
)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_small_file_one_page_no_reminder(tmp_path):
    p = _write(tmp_path, "f.txt", b"a\nb\nc\n")
    r = read_lines(p, None, None, ReadConfig())
    assert r.content == "     1\ta\n     2\tb\n     3\tc"
    assert r.metadata["has_more"] is False
    assert r.metadata["next_offset"] is None
    assert r.metadata["total_lines"] == 3
    assert "Continue" not in r.content


def test_line_count_pagination(tmp_path):
    p = _write(tmp_path, "f.txt", b"".join(f"L{i}\n".encode() for i in range(1, 11)))
    r = read_lines(p, 1, 4, ReadConfig())
    assert r.metadata["start_line"] == 1
    assert r.metadata["end_line"] == 4
    assert r.metadata["has_more"] is True
    assert r.metadata["next_offset"] == 5
    assert "Continue: read_file(path, offset=5)" in r.content
    r2 = read_lines(p, 5, 4, ReadConfig())
    assert r2.metadata["end_line"] == 8
    assert r2.metadata["next_offset"] == 9
    r3 = read_lines(p, 9, 4, ReadConfig())
    assert r3.metadata["end_line"] == 10
    assert r3.metadata["has_more"] is False
    assert r3.metadata["next_offset"] is None


def test_byte_budget_stops_before_limit(tmp_path):
    p = _write(tmp_path, "f.txt", b"".join(f"line{i:04d}\n".encode() for i in range(1, 11)))
    cfg = ReadConfig(max_bytes=25)
    r = read_lines(p, 1, 100, cfg)
    assert r.metadata["has_more"] is True
    assert r.metadata["truncation"]["byte_cap"] is True
    assert r.metadata["next_offset"] == r.metadata["end_line"] + 1
    assert "budget" in r.content


def test_long_line_truncated_with_byte_coords(tmp_path):
    big = b"X" * 5000
    p = _write(tmp_path, "f.txt", b"head\n" + big + b"\ntail\n")
    cfg = ReadConfig(max_line_bytes=100)
    r = read_lines(p, 1, 10, cfg)
    ll = r.metadata["truncation"]["long_lines"]
    assert len(ll) == 1
    e = ll[0]
    assert e["line"] == 2
    assert e["byte_start"] == 5
    assert e["shown_bytes"] == 100
    assert e["line_bytes"] == 5001
    assert e["byte_end"] == 5 + 5001
    assert "...[line truncated]" in r.content
    assert "byte_offset=105" in r.content
    assert "resume line mode at read_file(path, offset=3)" in r.content


def test_single_line_no_trailing_newline(tmp_path):
    p = _write(tmp_path, "f.txt", b"hello")
    r = read_lines(p, None, None, ReadConfig())
    assert r.content == "     1\thello"
    assert r.metadata["end_line"] == 1
    assert r.metadata["total_lines"] == 1
    assert r.metadata["has_more"] is False
    assert r.metadata["next_offset"] is None


def test_offset_beyond_eof(tmp_path):
    p = _write(tmp_path, "f.txt", b"a\nb\n")
    r = read_lines(p, 50, None, ReadConfig())
    assert "beyond end of file" in r.content
    assert r.metadata["has_more"] is False


def test_empty_file(tmp_path):
    p = _write(tmp_path, "f.txt", b"")
    r = read_lines(p, None, None, ReadConfig())
    assert r.content == "[empty file]"
    assert r.metadata["total_lines"] == 0


def test_binary_file(tmp_path):
    p = _write(tmp_path, "f.bin", b"abc\x00def")
    r = read_lines(p, None, None, ReadConfig())
    assert "binary file" in r.content
    assert r.metadata["binary"] is True


def test_large_file_total_lines_null(tmp_path):
    p = _write(tmp_path, "f.txt", b"".join(f"L{i}\n".encode() for i in range(1, 51)))
    cfg = ReadConfig(count_max_bytes=10)
    r = read_lines(p, 1, 5, cfg)
    assert r.metadata["total_lines"] is None
    assert r.metadata["has_more"] is True


def test_byte_window_has_more(tmp_path):
    p = _write(tmp_path, "f.bin", b"0123456789")
    cfg = ReadConfig(max_bytes=4)
    r = read_byte_window(p, 0, None, cfg)
    assert r.content.startswith("0123")
    assert r.metadata["mode"] == "bytes"
    assert r.metadata["byte_start"] == 0
    assert r.metadata["byte_end"] == 4
    assert r.metadata["has_more"] is True
    assert r.metadata["next_byte_offset"] == 4
    assert "byte_offset=4" in r.content


def test_byte_window_to_eof(tmp_path):
    p = _write(tmp_path, "f.bin", b"0123456789")
    r = read_byte_window(p, 8, 100, ReadConfig())
    assert r.metadata["byte_end"] == 10
    assert r.metadata["has_more"] is False
    assert r.metadata["next_byte_offset"] is None
    assert "Continue" not in r.content


def test_byte_offset_beyond_eof(tmp_path):
    p = _write(tmp_path, "f.bin", b"0123456789")
    r = read_byte_window(p, 999, None, ReadConfig())
    assert "beyond end of file" in r.content
    assert r.metadata["has_more"] is False


def test_byte_limit_clamped_to_max(tmp_path):
    p = _write(tmp_path, "f.bin", b"0" * 1000)
    cfg = ReadConfig(max_bytes=50)
    r = read_byte_window(p, 0, 999, cfg)  # asks 999, clamped to 50
    assert r.metadata["byte_end"] == 50
