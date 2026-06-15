from ctx_weft.providers._encoding import decode_console


def test_decodes_plain_utf8():
    assert decode_console("héllo 世界".encode("utf-8")) == "héllo 世界"


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
