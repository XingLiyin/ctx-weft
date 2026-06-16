from ctx_weft.providers.capability_filesystem import provider as fsprov
from ctx_weft.providers.capability_filesystem.provider import _FS_TOOLS
from ctx_weft.protocols.context import ProviderContext


async def _result(events):
    out = {"content": "", "metadata": {}, "error": None}
    async for e in events:
        if e.kind == "result":
            out["content"] = e.payload.get("content", "")
            out["metadata"] = e.payload.get("metadata", {})
        elif e.kind == "error":
            out["error"] = e.payload
    return out


def _ctx():
    return ProviderContext(session_id="s1")


async def test_read_file_line_mode(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\nb\nc\n")
    r = await _result(fsprov.read_file(str(p), 1, 2, ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["mode"] == "lines"
    assert r["metadata"]["next_offset"] == 3
    assert "     1\ta" in r["content"]


async def test_read_file_byte_mode(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"0123456789")
    r = await _result(fsprov.read_file(str(p), byte_offset=2, byte_limit=4, ctx=_ctx()))
    assert r["metadata"]["mode"] == "bytes"
    assert r["content"].startswith("2345")


async def test_read_file_mode_mutual_exclusion(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\nb\n")
    r = await _result(fsprov.read_file(str(p), limit=5, byte_offset=0, ctx=_ctx()))
    assert r["error"] is not None
    assert r["error"]["code"] == "INVALID_ARGS"


async def test_read_file_bad_offset(tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\n")
    r = await _result(fsprov.read_file(str(p), 0, ctx=_ctx()))
    assert r["error"]["code"] == "INVALID_ARGS"


async def test_read_file_not_found(tmp_path):
    r = await _result(fsprov.read_file(str(tmp_path / "nope.txt"), ctx=_ctx()))
    assert r["error"]["code"] == "FILE_NOT_FOUND"


async def test_read_file_path_not_allowed(tmp_path):
    # File lives in tmp_path, but allowed_dirs points elsewhere → blocked.
    p = tmp_path / "f.txt"
    p.write_bytes(b"secret\n")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    ctx = ProviderContext(session_id="s1", extra={"allowed_dirs": [str(allowed)]})
    r = await _result(fsprov.read_file(str(p), ctx=ctx))
    assert r["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_read_file_byte_offset_beyond_eof(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"0123456789")
    r = await _result(fsprov.read_file(str(p), byte_offset=999, ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["mode"] == "bytes"
    assert r["metadata"]["has_more"] is False
    assert "beyond end of file" in r["content"]


def test_read_file_is_not_spillable():
    assert _FS_TOOLS["read_file"].spillable is False


def test_read_file_description_documents_both_modes():
    desc = _FS_TOOLS["read_file"].description
    assert "byte_offset" in desc and "offset" in desc
    assert len(desc.splitlines()) > 1
