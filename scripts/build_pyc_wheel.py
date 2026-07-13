#!/usr/bin/env python
"""构建 ctx_weft 的 sourceless（仅 .pyc）wheel。

流程：
  1. `uv build --wheel` 正常产出含 .py 的 wheel。
  2. 解包 → 把包内每个 foo.py 编成同位置 foo.pyc（legacy 布局，不放 __pycache__）→ 删 .py。
  3. `wheel pack` 重打包（自动重算 RECORD）。

产物：dist/ctx_weft-<ver>-py3-none-any.whl，内部无任何 .py，仅 .pyc。
注意：.pyc 绑 CPython 小版本（当前 3.11）；host 侧须同锁 3.11（见 spec §9.3）。

用法（在 core 仓根）：
  uv run --with wheel python scripts/build_pyc_wheel.py
"""
from __future__ import annotations

import compileall
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyc-wheel"


def run(*cmd: str) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True)


def main() -> int:
    # 1. 清理并正常构建 wheel
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    for old in DIST.glob("*.whl"):
        old.unlink()
    run("uv", "build", "--wheel", "--out-dir", str(DIST))
    src_whl = next(DIST.glob("*.whl"))
    print(f"  正常 wheel: {src_whl.name}")

    # 2. 解包
    run(sys.executable, "-m", "wheel", "unpack", str(src_whl), "-d", str(WORK))
    pkg_root = next(p for p in WORK.iterdir() if p.is_dir())

    # 3. 编字节码 → legacy 布局 → 删 .py（只动包目录，不碰 *.dist-info）
    pkg_dirs = [d for d in pkg_root.iterdir() if d.is_dir() and not d.name.endswith(".dist-info")]
    for pdir in pkg_dirs:
        compileall.compile_dir(str(pdir), quiet=1, optimize=0, force=True, legacy=False)
    # __pycache__/foo.cpython-XY.pyc → foo.pyc（与源同位置），再删 __pycache__ 与所有 .py
    for cache in pkg_root.rglob("__pycache__"):
        for pyc in cache.glob("*.pyc"):
            mod = pyc.name.split(".")[0]  # foo.cpython-311.pyc → foo
            shutil.move(str(pyc), str(cache.parent / f"{mod}.pyc"))
        shutil.rmtree(cache)
    for py in pkg_root.rglob("*.py"):
        py.unlink()

    # 4. 重打包（wheel pack 会重算 RECORD）
    src_whl.unlink()  # 移除含 .py 的原 wheel
    run(sys.executable, "-m", "wheel", "pack", str(pkg_root), "-d", str(DIST))
    out_whl = next(DIST.glob("*.whl"))

    # 5. 自检：wheel 内不得有任何 .py
    with zipfile.ZipFile(out_whl) as zf:
        pys = [n for n in zf.namelist() if n.endswith(".py")]
        pycs = [n for n in zf.namelist() if n.endswith(".pyc")]
    if pys:
        print(f"✗ wheel 内仍含 .py：{pys[:5]}", file=sys.stderr)
        return 1
    print(f"✓ 产物：{out_whl.name}（{len(pycs)} 个 .pyc，0 个 .py）")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
