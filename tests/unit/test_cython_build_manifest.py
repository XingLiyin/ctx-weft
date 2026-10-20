"""Cython's dependency manifest must include relative imports and from-imports."""
import importlib.util
from pathlib import Path

script = Path(__file__).resolve().parents[2] / "scripts/build_cython_wheel.py"
spec = importlib.util.spec_from_file_location("build_cython_wheel", script)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_manifest_resolves_package_and_module_imports(tmp_path):
    package = tmp_path / "ctx_weft"
    nested = package / "providers"
    nested.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import providers\n", encoding="utf-8")
    (nested / "worker.py").write_text(
        "import json, asyncio\n"
        "from ..protocols import Task\n"
        "from . import sibling\n"
        "from jinja2 import Environment\n"
        "def lazy():\n    from urllib import request\n",
        encoding="utf-8",
    )
    imports = builder.import_manifest(package)
    assert set(imports) == {
        "ctx_weft", "ctx_weft.providers", "ctx_weft.protocols", "ctx_weft.protocols.Task",
        "ctx_weft.providers.sibling", "json", "asyncio", "jinja2", "jinja2.Environment",
        "urllib", "urllib.request",
    }
    assert imports == sorted(set(imports))
