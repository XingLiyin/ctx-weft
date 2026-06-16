import json
import shutil

import pytest

from ctx_weft.providers.capability_filesystem import _grep
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


def _ctx(**extra):
    return ProviderContext(session_id="s1", extra=extra)


@pytest.fixture
def py_backend(monkeypatch):
    """Force the pure-Python backend so tests are deterministic without ripgrep."""
    monkeypatch.setattr(_grep, "_ripgrep_path", lambda: None)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a.py").write_text("import os\nfoo = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("FOO = 2\nbar = 3\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("foo in text\n", encoding="utf-8")
    return tmp_path


async def test_files_with_matches(py_backend, tree):
    r = await _result(fsprov.grep("foo", str(tree), ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["mode"] == "files_with_matches"
    assert str(tree / "a.py") in r["content"]
    assert str(tree / "c.txt") in r["content"]
    assert str(tree / "b.py") not in r["content"]  # FOO is uppercase
    assert r["metadata"]["count"] == 2


async def test_content_mode_line_numbers(py_backend, tree):
    r = await _result(fsprov.grep("foo", str(tree / "a.py"), output_mode="content", ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["mode"] == "content"
    assert f"{tree / 'a.py'}:2:foo = 1" in r["content"]


async def test_file_glob_filter(py_backend, tree):
    r = await _result(fsprov.grep("foo", str(tree), file_glob="*.py", ctx=_ctx()))
    assert str(tree / "a.py") in r["content"]
    assert str(tree / "c.txt") not in r["content"]


async def test_ignore_case(py_backend, tree):
    r = await _result(fsprov.grep("foo", str(tree), ignore_case=True, ctx=_ctx()))
    assert str(tree / "b.py") in r["content"]  # matches FOO
    assert r["metadata"]["count"] == 3


async def test_context_lines(py_backend, tree):
    r = await _result(
        fsprov.grep("foo", str(tree / "a.py"), output_mode="content", context=1, ctx=_ctx())
    )
    assert f"{tree / 'a.py'}-1-import os" in r["content"]  # context line before
    assert f"{tree / 'a.py'}:2:foo = 1" in r["content"]  # the match


async def test_no_matches(py_backend, tree):
    r = await _result(fsprov.grep("zzzznope", str(tree), ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["count"] == 0
    assert r["content"] == "(no matches)"


async def test_invalid_output_mode(py_backend, tree):
    r = await _result(fsprov.grep("foo", str(tree), output_mode="bogus", ctx=_ctx()))
    assert r["error"]["code"] == "INVALID_ARGS"


async def test_missing_pattern(py_backend, tree):
    r = await _result(fsprov.grep("", str(tree), ctx=_ctx()))
    assert r["error"]["code"] == "MISSING_PATTERN"


async def test_path_not_allowed(py_backend, tree):
    allowed = tree / "allowed"
    allowed.mkdir()
    r = await _result(fsprov.grep("foo", str(tree), ctx=_ctx(allowed_dirs=[str(allowed)])))
    assert r["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_path_not_found(py_backend, tmp_path):
    r = await _result(fsprov.grep("foo", str(tmp_path / "nope"), ctx=_ctx()))
    assert r["error"]["code"] == "PATH_NOT_FOUND"


async def test_truncated(py_backend, tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("match\n", encoding="utf-8")
    r = await _result(fsprov.grep("match", str(tmp_path), ctx=_ctx(grep_max_results=3)))
    assert r["metadata"]["truncated"] is True
    assert r["metadata"]["count"] == 3


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_ripgrep_backend_selected(tree):
    r = await _result(fsprov.grep("foo", str(tree), ctx=_ctx()))
    assert r["error"] is None
    assert r["metadata"]["backend"] == "ripgrep"


# ── ripgrep backend, with rg's output stubbed (so it runs even without rg installed) ──


@pytest.fixture
def fake_rg(monkeypatch):
    """Pretend rg exists; capture the args _run_rg is called with and return canned stdout."""
    monkeypatch.setattr(_grep, "_ripgrep_path", lambda: "rg")
    calls = {}

    def _fake_run(rg, args):
        calls["args"] = args
        from subprocess import CompletedProcess
        return CompletedProcess(["rg", *args], 0, stdout=calls["stdout"], stderr="")

    monkeypatch.setattr(_grep, "_run_rg", _fake_run)
    return calls


async def test_rg_files_parse(fake_rg, tmp_path):
    fake_rg["stdout"] = "/w/b.py\n/w/a.py\n"
    r = await _result(fsprov.grep("foo", str(tmp_path), ctx=_ctx()))
    assert r["metadata"]["backend"] == "ripgrep"
    assert r["content"] == "/w/a.py\n/w/b.py"  # sorted
    assert r["metadata"]["count"] == 2
    assert "-l" in fake_rg["args"]


async def test_rg_content_parse(fake_rg, tmp_path):
    fake_rg["stdout"] = "\n".join([
        json.dumps({"type": "context", "data": {"path": {"text": "/w/a.py"},
                    "line_number": 1, "lines": {"text": "import os\n"}}}),
        json.dumps({"type": "match", "data": {"path": {"text": "/w/a.py"},
                    "line_number": 2, "lines": {"text": "foo = 1\n"}}}),
    ])
    r = await _result(fsprov.grep("foo", str(tmp_path), output_mode="content", context=1, ctx=_ctx()))
    assert r["metadata"]["backend"] == "ripgrep"
    assert "/w/a.py-1-import os" in r["content"]  # context line
    assert "/w/a.py:2:foo = 1" in r["content"]    # match line
    assert r["metadata"]["count"] == 1
    assert "--json" in fake_rg["args"]


async def test_rg_error_exit_raises_grep_error(monkeypatch, tmp_path):
    monkeypatch.setattr(_grep, "_ripgrep_path", lambda: "rg")

    def _boom(rg, args):
        raise RuntimeError("regex parse error")

    monkeypatch.setattr(_grep, "_run_rg", _boom)
    r = await _result(fsprov.grep("(", str(tmp_path), ctx=_ctx()))
    assert r["error"]["code"] == "GREP_ERROR"
