from ctx_weft.providers.capability_filesystem import provider as fsprov
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


async def test_edit_file_unique_replace(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello world\n", encoding="utf-8")
    r = await _result(fsprov.edit_file(str(p), "world", "there", ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["replacements"] == 1
    assert p.read_text(encoding="utf-8") == "hello there\n"


async def test_edit_file_not_unique_without_replace_all(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a a a\n", encoding="utf-8")
    r = await _result(fsprov.edit_file(str(p), "a", "b", ctx=_ctx()))
    assert r["error"]["code"] == "NOT_UNIQUE"
    assert p.read_text(encoding="utf-8") == "a a a\n"  # unchanged


async def test_edit_file_replace_all(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("a a a\n", encoding="utf-8")
    r = await _result(fsprov.edit_file(str(p), "a", "b", replace_all=True, ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["replacements"] == 3
    assert p.read_text(encoding="utf-8") == "b b b\n"


async def test_edit_file_string_not_found(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n", encoding="utf-8")
    r = await _result(fsprov.edit_file(str(p), "absent", "x", ctx=_ctx()))
    assert r["error"]["code"] == "STRING_NOT_FOUND"
    assert p.read_text(encoding="utf-8") == "hello\n"  # unchanged


async def test_edit_file_not_found(tmp_path):
    r = await _result(fsprov.edit_file(str(tmp_path / "nope.txt"), "a", "b", ctx=_ctx()))
    assert r["error"]["code"] == "FILE_NOT_FOUND"


async def test_edit_file_path_not_allowed(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("secret\n", encoding="utf-8")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    ctx = ProviderContext(session_id="s1", extra={"allowed_dirs": [str(allowed)]})
    r = await _result(fsprov.edit_file(str(p), "secret", "x", ctx=ctx))
    assert r["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_edit_file_old_equals_new(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hello\n", encoding="utf-8")
    r = await _result(fsprov.edit_file(str(p), "hello", "hello", ctx=_ctx()))
    assert r["error"]["code"] == "INVALID_ARGS"
