"""Decode raw subprocess output bytes into text.

Modern tools emit UTF-8, older Windows tools often emit GBK, and cmd /u emits
UTF-16 without a BOM. Stream decoding must retain incomplete UTF-16 code units:
readline() splits on a single LF byte, not on a complete UTF-16 newline.
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator
from typing import Protocol


def _utf16_encoding(data: bytes) -> str | None:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    # ASCII path components and CR/LF produce NULs in one byte lane. Require
    # consistent alignment rather than guessing UTF-16 from arbitrary GBK bytes.
    even = data[::2].count(0)
    odd = data[1::2].count(0)
    pairs = max(1, len(data) // 2)
    if odd > even * 2 and odd * 3 >= pairs:
        return "utf-16-le"
    if even > odd * 2 and even * 3 >= pairs:
        return "utf-16-be"
    # A line containing only Chinese may have no NUL until its CR/LF suffix.
    if data.endswith((b"\r\x00\n", b"\r\x00\n\x00")):
        return "utf-16-le"
    if data.endswith(b"\x00\r\x00\n"):
        return "utf-16-be"
    return None


def decode_console(data: bytes) -> str:
    """Decode a complete output buffer; malformed bytes never raise."""
    if not data:
        return ""
    utf16 = _utf16_encoding(data)
    if utf16 and len(data) % 2 == 0:
        try:
            return data.decode(utf16)
        except UnicodeError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


class ConsoleDecoder:
    """Per-stream decoder preserving UTF-16 boundaries across raw byte lines."""

    def __init__(self) -> None:
        self._pending = b""
        self._decoder: codecs.IncrementalDecoder | None = None
        self._started = False

    def decode(self, data: bytes, *, final: bool = False) -> str:
        if self._decoder is not None:
            return self._decoder.decode(data, final=final)
        if not self._started:
            self._pending += data
            # An initial LE LF or partial BOM is insufficient to choose a codec.
            if not final and self._pending in (b"\n", b"\xff", b"\xfe"):
                return ""
            data, self._pending = self._pending, b""
            self._started = True
            utf16 = _utf16_encoding(data)
            if utf16:
                self._decoder = codecs.getincrementaldecoder(utf16)(errors="replace")
                return self._decoder.decode(data, final=final)
        return decode_console(data)


class _ByteLines(Protocol):
    async def readline(self) -> bytes: ...


async def iter_console_output(stream: _ByteLines) -> AsyncIterator[tuple[int, str]]:
    """Yield raw byte counts and decoded text; flush the decoder at EOF."""
    decoder = ConsoleDecoder()
    while data := await stream.readline():
        yield len(data), decoder.decode(data)
    tail = decoder.decode(b"", final=True)
    if tail:
        yield 0, tail
