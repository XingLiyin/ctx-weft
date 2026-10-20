#!/usr/bin/env python
"""Build a native ctx-weft wheel without changing the development source tree.

uv run --no-sync --with Cython==3.2.4 --with setuptools --with wheel \
    python scripts/build_cython_wheel.py

The source wheel also installs this script as Scripts/build_ctx_weft_cython.py,
so a host can compile its exact vendored wheel without a ctx-weft checkout.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import chdir
from email.parser import BytesParser
from importlib.util import resolve_name
from pathlib import Path


def import_manifest(package: Path) -> list[str]:
    """Keep Python import information that PyInstaller cannot read from .pyd files."""
    modules: set[str] = set()
    for source in sorted(package.rglob("*.py")):
        parts = source.relative_to(package.parent).with_suffix("").parts
        parent = ".".join(parts[:-1])
        for node in ast.walk(ast.parse(source.read_bytes(), filename=str(source))):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = "." * node.level + (node.module or "")
                name = resolve_name(name, parent) if node.level else name
                if name:
                    modules.add(name)
                    # Some from-imports refer to modules, others to classes/functions.
                    # The PyInstaller hook resolves these candidates in the host environment.
                    modules.update(f"{name}.{alias.name}" for alias in node.names if alias.name != "*")
    return sorted(modules)


def build_wheel(source_wheel: Path, output_dir: Path, work: Path, jobs: int) -> Path:
    from Cython.Build import cythonize
    from setuptools import Distribution, Extension
    from setuptools.command.bdist_wheel import bdist_wheel
    from wheel.wheelfile import WheelFile

    unpacked = work / "unpacked"
    with zipfile.ZipFile(source_wheel) as archive:
        archive.extractall(unpacked)
    metadata_path, = unpacked.glob("*.dist-info/METADATA")
    metadata = BytesParser().parsebytes(metadata_path.read_bytes())
    if metadata["Name"].replace("_", "-") != "ctx-weft":
        raise ValueError("--source-wheel must be a ctx-weft source wheel")
    package = unpacked / "ctx_weft"
    sources = sorted(package.rglob("*.py"))
    if not sources:
        raise ValueError("The input wheel must contain Python sources")
    imports = import_manifest(package)

    with chdir(unpacked):
        extensions = [
            Extension(".".join(path.relative_to(unpacked).with_suffix("").parts),
                      [path.relative_to(unpacked).as_posix()])
            for path in sources
        ]
        extensions = cythonize(
            extensions,
            build_dir=str(work / "c"),
            nthreads=jobs,
            compiler_directives={
                "language_level": 3,
                "binding": True,
                "annotation_typing": False,
                "infer_types": False,
                "emit_code_comments": False,
            },
        )
        # Docstrings are intentionally retained: tool declarations use __doc__.
        distribution = Distribution({
            "name": metadata["Name"], "version": metadata["Version"],
            "ext_modules": extensions,
        })
        command = distribution.get_command_obj("build_ext")
        command.build_lib = str(work / "native")
        command.build_temp = str(work / "objects")
        command.parallel = jobs
        command.ensure_finalized()
        command.run()
        wheel_command = bdist_wheel(distribution)
        wheel_command.ensure_finalized()
        tag = "-".join(wheel_command.get_tag())

    # Preserve original METADATA (including Requires-Dist/extras), licences, and data.
    for source in sources:
        source.unlink()
    for bytecode in package.rglob("*.pyc"):
        bytecode.unlink()
    for binary in (work / "native").rglob("*"):
        if binary.is_file():
            target = unpacked / binary.relative_to(work / "native")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, target)
    for helper in unpacked.glob("*.data/scripts/build_ctx_weft_cython.py"):
        helper.unlink()
    (package / "_cython_imports.json").write_text(json.dumps(imports, indent=2), encoding="utf-8")
    info = metadata_path.parent
    (info / "RECORD").unlink(missing_ok=True)
    (info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: ctx-weft Cython build\n"
        f"Root-Is-Purelib: false\nTag: {tag}\n",
        encoding="utf-8",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    result = output_dir / f"ctx_weft-{metadata['Version']}-{tag}.whl"
    with WheelFile(result, "w") as archive:
        archive.write_files(str(unpacked))
    return result


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-wheel", type=Path)
    parser.add_argument("--output-dir", type=Path, default=root / "dist" / "native")
    parser.add_argument("--work-dir", type=Path, default=root / "build" / "cython-wheel")
    parser.add_argument("--result-file", type=Path, help="Write the resulting wheel's absolute path")
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 4))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work_dir) as temporary:
        work = Path(temporary).resolve()
        source = args.source_wheel
        if source is None:
            subprocess.run([
                "uv", "build", "--wheel", "--project", str(root),
                "--out-dir", str(work / "source"),
            ], check=True)
            source, = (work / "source").glob("*.whl")
        result = build_wheel(source.resolve(), args.output_dir.resolve(), work, args.jobs)
    if args.result_file:
        args.result_file.write_text(str(result), encoding="utf-8")
    print(result, flush=True)


if __name__ == "__main__":
    main()
