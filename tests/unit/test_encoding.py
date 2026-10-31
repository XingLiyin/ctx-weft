import io

import pytest

from ctx_weft.providers._encoding import ConsoleDecoder, decode_console, iter_console_output


def test_decodes_plain_utf8():
    assert decode_console("héllo 世界".encode()) == "héllo 世界"


def test_decodes_gbk_when_not_valid_utf8():
    # GBK-encoded Chinese is not valid UTF-8; must fall back to GBK, not mangle.
    data = "命令未找到".encode("gbk")
    assert decode_console(data) == "命令未找到"


def test_ascii_passthrough():
    assert decode_console(b"is not recognized") == "is not recognized"


def test_invalid_bytes_never_raise():
    # Bytes that are neither valid UTF-8 nor valid GBK must not raise.
    data = b"\xff\xfe\x00ok"
    out = decode_console(data)
    assert isinstance(out, str)
    assert "ok" in out


def test_empty():
    assert decode_console(b"") == ""


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_decodes_utf16_paths(encoding):
    text = "C:/中文目录/脚本.py\r\n第二行\r\n"
    assert decode_console(text.encode(encoding)) == text


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
async def test_stream_keeps_utf16_units_across_byte_lines(encoding):
    text = "\nC:/中文路径/上报.py\r\n第二行\r\n"
    raw = text.encode(encoding)

    class ByteLines:
        def __init__(self):
            self.source = io.BytesIO(raw)

        async def readline(self):
            return self.source.readline()

    chunks = [part async for part in iter_console_output(ByteLines())]
    assert sum(size for size, _ in chunks) == len(raw)
    assert "".join(value for _, value in chunks) == text


def test_utf16_stream_flushes_partial_final_unit_without_raising():
    decoder = ConsoleDecoder()
    text = decoder.decode(b"\xff\xfeA\x00B") + decoder.decode(b"", final=True)
    assert text == "A\ufffd"
