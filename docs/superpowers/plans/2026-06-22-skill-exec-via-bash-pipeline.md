# Skill exec via bash_exec pipeline + configurable pip index — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make skill `.py` scripts run inside the per-session workspace `.venv` by delegating execution to the `bash_exec` pipeline, and make all pip installs (bash + skill) use a configurable internal index.

**Architecture:** `LocalSkillCapabilityProvider.exec_script` delegates to an injected `bash_runner` callback that routes to `FilesystemToolsProvider.invoke("<fs>:bash_exec", …)`, reusing its venv bootstrap/activation, safety scan, liveness timeout, and window-hiding. Two small brand-neutral seams let the caller pass extra env (`SKILL_DIR`) and override timeouts. Part A is host-only: map `IPMC_PIP_*` settings to standard `PIP_*` env vars at startup so every subprocess inherits the mirror.

**Tech Stack:** Python 3.11, asyncio subprocess, pytest (run via `uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-06-22-skill-exec-via-bash-pipeline-design.md`

**Cross-repo note:** `ctx-weft/` is the local mirror of upstream `ctx_wefta` (sync wefta→weft only). Tasks 1–3 touch core and MUST be queued for upstream backfill (Task 8). Host tasks (4–7) do not.

---

## File Structure

- `src/ctx_weft/providers/capability_filesystem/provider.py` — `bash_exec` merges `ctx.extra["extra_env"]`; `invoke()` uses `setdefault` for the 3 limit knobs.
- `src/ctx_weft/providers/capability_skill_local/provider.py` — `exec_script` delegates to `bash_runner` when set; else current direct run.
- `tests/unit/test_bash_exec_encoding.py` — extra_env merge test.
- `tests/unit/test_fs_config.py` — invoke setdefault test.
- `tests/unit/test_skill_exec_python.py` — delegation tests (existing direct-path tests stay green).
- `src/ipmastercowork/cli.py` — capture fs provider, wire `bash_runner`; call `apply_pip_index_env`.
- `src/ipmastercowork/config.py` — `pip_*` settings + `apply_pip_index_env`.
- `tests/unit/test_pip_index_env.py` (new) — Part A tests.
- `.env.example` — document `IPMC_PIP_*`.

---

### Task 1: `bash_exec` honors caller-provided `extra_env`

**Files:**
- Modify: `src/ctx_weft/providers/capability_filesystem/provider.py` (the `bash_exec` env construction, ~line 191)
- Test: `tests/unit/test_bash_exec_encoding.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_bash_exec_encoding.py`:

```python
async def test_bash_exec_merges_extra_env(monkeypatch):
    captured = {}
    real = asyncio.create_subprocess_shell

    async def spy(cmd, **kw):
        captured["env"] = kw.get("env")
        return await real(cmd, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    ctx = ProviderContext(session_id="s1", extra={"extra_env": {"SKILL_DIR": "X_MARK"}})
    await _collect(fsprov.bash_exec("echo hi", ctx=ctx))

    assert captured["env"]["SKILL_DIR"] == "X_MARK"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bash_exec_encoding.py::test_bash_exec_merges_extra_env -v`
Expected: FAIL — `KeyError: 'SKILL_DIR'` (env has no SKILL_DIR).

- [ ] **Step 3: Implement the merge**

In `provider.py`, find the `bash_exec` env line (currently):

```python
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
```

Replace with:

```python
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # 调用方(如 skill 委托执行)注入的额外环境变量(如 SKILL_DIR)。venv_env 之后会保留这些键。
    _extra_env = ctx.extra.get("extra_env") if ctx else None
    if _extra_env:
        env.update(_extra_env)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bash_exec_encoding.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/capability_filesystem/provider.py tests/unit/test_bash_exec_encoding.py
git commit -m "feat(core): bash_exec merges ctx.extra['extra_env'] into subprocess env

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `invoke()` lets callers override the 3 limit knobs

**Files:**
- Modify: `src/ctx_weft/providers/capability_filesystem/provider.py` (the `invoke()` extra block, ~lines 642-644)
- Test: `tests/unit/test_fs_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_fs_config.py`:

```python
def test_invoke_lets_caller_override_timeouts(tmp_path):
    p = FilesystemToolsProvider(FilesystemConfig(bash_hard_cap_sec=120))
    p.register_session("s1", str(tmp_path))
    captured = {}

    async def fake_dispatch(cap_id, args, ctx):
        captured["extra"] = ctx.extra
        return
        yield  # pragma: no cover — make it an async generator

    p._dispatch = fake_dispatch  # type: ignore[method-assign]
    ctx = ProviderContext(session_id="s1", extra={"bash_hard_cap_sec": 999})
    gen = p.invoke("fs:bash_exec", {"command": "echo hi"}, ctx)

    import asyncio

    async def drain():
        async for _ in gen:
            pass

    asyncio.run(drain())
    assert captured["extra"]["bash_hard_cap_sec"] == 999          # caller value preserved
    assert captured["extra"]["bash_venv_python"] is None          # fs-forced key still set
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_fs_config.py::test_invoke_lets_caller_override_timeouts -v`
Expected: FAIL — `assert 120 == 999` (invoke overwrote the caller's value).

- [ ] **Step 3: Implement setdefault**

In `provider.py` `invoke()`, replace these three lines:

```python
        extra["bash_idle_timeout_sec"] = self._cfg.bash_idle_timeout_sec
        extra["bash_hard_cap_sec"] = self._cfg.bash_hard_cap_sec
        extra["bash_max_output_bytes"] = self._cfg.bash_max_output_bytes
```

with:

```python
        # 限额类:调用方(如 skill 委托)可经 ctx.extra 覆盖;未给才用 fs 配置。
        extra.setdefault("bash_idle_timeout_sec", self._cfg.bash_idle_timeout_sec)
        extra.setdefault("bash_hard_cap_sec", self._cfg.bash_hard_cap_sec)
        extra.setdefault("bash_max_output_bytes", self._cfg.bash_max_output_bytes)
```

(Leave `bash_auto_venv`, `bash_venv_dir`, `bash_venv_python`, `workspace`, `allowed_dirs`, and the file_read/glob/grep keys as direct assignments — fs stays authoritative for those.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_fs_config.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/capability_filesystem/provider.py tests/unit/test_fs_config.py
git commit -m "feat(core): invoke() lets callers override bash timeout/output limits

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: skill `exec_script` delegates to `bash_runner`

**Files:**
- Modify: `src/ctx_weft/providers/capability_skill_local/provider.py` (imports, constructor, `exec_script`)
- Test: `tests/unit/test_skill_exec_python.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_skill_exec_python.py` (the file already imports `RunResult`, `skillprov`, `LocalSkillCapabilityProvider`, `ProviderContext`, `_make_skill`, `_patch_capture`):

```python
import pytest
from ctx_weft.protocols.capability import CapabilityEvent


def _result_runner(content="OUT", exit_code=0):
    async def runner(command, ctx):
        runner.command = command
        runner.extra = ctx.extra
        yield CapabilityEvent(kind="result",
                              payload={"content": content, "metadata": {"exit_code": exit_code}})
    return runner


async def test_exec_script_delegates_to_bash_runner(tmp_path):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "print('hi')\n")
    runner = _result_runner("OUT")
    prov = LocalSkillCapabilityProvider(skills_dir, bash_runner=runner)
    out = await prov.exec_script("py-skill", "scripts/run.py", "--x 1", ProviderContext(session_id="s1"))
    assert out == "OUT"
    assert runner.command.startswith('python "')
    assert runner.command.endswith('run.py" --x 1')
    assert runner.extra["extra_env"]["SKILL_DIR"].endswith("py-skill")
    assert runner.extra["bash_hard_cap_sec"] == 600  # skill's own default


async def test_exec_script_non_py_delegates_without_python(tmp_path):
    skills_dir = _make_skill(tmp_path, "scripts/run.sh", "echo hi\n")
    runner = _result_runner("OK")
    prov = LocalSkillCapabilityProvider(skills_dir, bash_runner=runner)
    await prov.exec_script("py-skill", "scripts/run.sh", "", ProviderContext(session_id="s1"))
    assert "python" not in runner.command
    assert runner.command.endswith('run.sh"')


async def test_exec_script_bash_runner_error_raises(tmp_path):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "x\n")

    async def runner(command, ctx):
        yield CapabilityEvent(kind="error", payload={"code": "TIMEOUT", "message": "boom"})

    prov = LocalSkillCapabilityProvider(skills_dir, bash_runner=runner)
    with pytest.raises(RuntimeError, match="boom"):
        await prov.exec_script("py-skill", "scripts/run.py", "", ProviderContext(session_id="s1"))


async def test_exec_script_bash_runner_nonzero_raises(tmp_path):
    skills_dir = _make_skill(tmp_path, "scripts/run.py", "x\n")
    prov = LocalSkillCapabilityProvider(skills_dir, bash_runner=_result_runner("err", exit_code=2))
    with pytest.raises(RuntimeError, match="exited with code 2"):
        await prov.exec_script("py-skill", "scripts/run.py", "", ProviderContext(session_id="s1"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_skill_exec_python.py -k "delegates or bash_runner" -v`
Expected: FAIL — `LocalSkillCapabilityProvider() got an unexpected keyword argument 'bash_runner'`.

- [ ] **Step 3: Add imports + constructor param**

In `provider.py`, update the import block near the top (after `from pathlib import Path`):

```python
from pathlib import Path
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ctx_weft.protocols.capability import CapabilityEvent
```

In `__init__`, add the parameter after `python_executable: str | None = None,`:

```python
        python_executable: str | None = None,
        bash_runner: "Callable[[str, ProviderContext], AsyncIterator[CapabilityEvent]] | None" = None,
    ) -> None:
```

and store it after `self._python_executable = python_executable`:

```python
        self._python_executable = python_executable
        # 注入后,.py 执行委托给 bash_exec 流水线(workspace venv/安全/超时/隐藏窗口)。
        # None → 回退本地直跑(下方 _exec_direct)。
        self._bash_runner = bash_runner
```

- [ ] **Step 4: Refactor `exec_script` into delegate + direct paths**

Replace the entire `exec_script` method body (from `entry = self._require_entry(...)` through the final `return stdout`) with:

```python
        entry = self._require_entry(skill_name)
        skill_root = entry.skill_dir.resolve()
        resolved = (entry.skill_dir / script_path).resolve()
        try:
            resolved.relative_to(skill_root)
        except ValueError:
            raise ValueError(f"script_path '{script_path}' escapes skill directory")
        if not resolved.is_file():
            raise FileNotFoundError(f"script '{script_path}' not found in skill '{skill_name}'")

        if self._bash_runner is not None:
            return await self._exec_via_bash(skill_root, resolved, args, ctx)
        return await self._exec_direct(skill_root, resolved, args)

    async def _exec_via_bash(self, skill_root, resolved, args, ctx) -> str:
        """委托给 bash_exec 流水线:.py 用裸 python(由 venv 激活解析),脚本走绝对路径。
        注入 SKILL_DIR 与 skill 自己的超时;收集 result/error 事件转成返回值/异常。"""
        import dataclasses

        base = f'python "{resolved}"' if resolved.suffix == ".py" else f'"{resolved}"'
        cmd = f"{base} {args}" if args else base

        extra = {
            **ctx.extra,
            "extra_env": {**(ctx.extra.get("extra_env") or {}), "SKILL_DIR": str(skill_root)},
            "bash_idle_timeout_sec": self._idle_timeout_sec,
            "bash_hard_cap_sec": self._hard_cap_sec,
            "bash_max_output_bytes": self._output_limit_chars,
        }
        ctx2 = dataclasses.replace(ctx, extra=extra)

        content = ""
        exit_code = 0
        async for ev in self._bash_runner(cmd, ctx2):
            if ev.kind == "error":
                msg = ev.payload.get("message") or ev.payload.get("code") or "skill exec failed"
                raise RuntimeError(msg)
            if ev.kind == "result":
                content = ev.payload.get("content", "")
                exit_code = (ev.payload.get("metadata") or {}).get("exit_code", 0)
        if exit_code != 0:
            raise RuntimeError(f"script exited with code {exit_code}\n{content[: self._output_limit_chars]}")
        return content[: self._output_limit_chars]

    async def _exec_direct(self, skill_root, resolved, args) -> str:
        """无 bash_runner 时的回退:本地直跑,cwd=skill_dir,解释器用内置/裸 python。"""
        if resolved.suffix == ".py":
            interp = self._python_executable or "python"
            base = f'"{interp}" "{resolved}"'
        else:
            base = f'"{resolved}"'
        cmd = f"{base} {args}" if args else base

        env = {**os.environ, "SKILL_DIR": str(skill_root), "PYTHONIOENCODING": "utf-8"}
        result = await run_with_liveness(
            cmd,
            cwd=str(skill_root),
            env=env,
            idle_timeout_sec=self._idle_timeout_sec,
            hard_cap_sec=self._hard_cap_sec,
            output_limit_bytes=self._output_limit_chars,
        )
        stdout = result.stdout[: self._output_limit_chars]
        stderr = result.stderr[: self._output_limit_chars]

        if result.timed_out:
            survivor_note = (
                "" if result.terminated_clean
                else f" WARNING: {len(result.survivors)} process(es) may still be "
                     "running; outputs may be partial."
            )
            raise RuntimeError(
                f"exec_script timed out ({result.timeout_kind}).{survivor_note}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        if result.exit_code != 0:
            raise RuntimeError(
                f"script exited with code {result.exit_code}\n"
                f"stdout: {stdout}\nstderr: {stderr}"
            )
        return stdout
```

> Verify you removed the old single-body `exec_script` tail (the old `env = {...}` / `run_with_liveness` / `if result.timed_out` / `if result.exit_code` / `return stdout` block) — it now lives in `_exec_direct`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_skill_exec_python.py tests/unit/test_skill_exec_encoding.py -v`
Expected: PASS — new delegation tests AND the existing direct-path tests (`test_exec_script_uses_configured_python`, etc., which pass no `bash_runner` → `_exec_direct`).

- [ ] **Step 6: Commit**

```bash
git add src/ctx_weft/providers/capability_skill_local/provider.py tests/unit/test_skill_exec_python.py
git commit -m "feat(core): skill exec_script delegates to bash_exec via bash_runner

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: host wires `bash_runner` into the skill provider

**Files:**
- Modify: `src/ipmastercowork/cli.py` (the `build_runtime` provider-registration block, ~lines 82-107)
- Test: `tests/unit/test_skill_bash_runner_wiring.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_skill_bash_runner_wiring.py`:

```python
"""cli.build_runtime 给 skill provider 接上路由到 fs.bash_exec 的 bash_runner。"""

from ctx_weft.providers.capability_filesystem import (
    FilesystemConfig,
    FilesystemToolsProvider,
)
from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider


def test_bash_runner_routes_to_fs_invoke(monkeypatch):
    fs = FilesystemToolsProvider(FilesystemConfig())
    seen = {}

    def fake_invoke(cap_id, args, ctx):
        seen["cap_id"] = cap_id
        seen["args"] = args
        return iter(())  # 占位,不需真跑

    monkeypatch.setattr(fs, "invoke", fake_invoke)
    bash_runner = lambda cmd, ctx, _fs=fs: _fs.invoke(f"{_fs.name}:bash_exec", {"command": cmd}, ctx)

    prov = LocalSkillCapabilityProvider("/tmp/skills", bash_runner=bash_runner)
    prov._bash_runner("echo hi", object())
    assert seen["cap_id"].endswith(":bash_exec")
    assert seen["args"] == {"command": "echo hi"}
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `uv run pytest tests/unit/test_skill_bash_runner_wiring.py -v`
Expected: PASS immediately (it validates the lambda shape, not cli internals). If it FAILS, the core API from Task 3 is wrong — stop and fix.

- [ ] **Step 3: Wire it in `cli.py`**

In `build_runtime`, the filesystem block currently reads:

```python
    if enable_tools:
        from ctx_weft.providers.capability_filesystem import FilesystemToolsProvider, FilesystemConfig
        # 文件系统工具（bash/read/write/glob）自管 per-session workspace。
        providers.register_capability(FilesystemToolsProvider(FilesystemConfig(
            ...
            glob_max_results=cfg.fs_glob_max_results,
            bash_venv_python=cfg.fs_bash_venv_python,
        )))
```

Change it to capture the instance, and add a module/function-scope `fs_provider = None` before the `if enable_tools:`:

```python
    fs_provider = None
    if enable_tools:
        from ctx_weft.providers.capability_filesystem import FilesystemToolsProvider, FilesystemConfig
        # 文件系统工具（bash/read/write/glob）自管 per-session workspace。
        fs_provider = FilesystemToolsProvider(FilesystemConfig(
            bash_idle_timeout_sec=cfg.fs_bash_idle_timeout_sec,
            bash_hard_cap_sec=cfg.fs_bash_hard_cap_sec,
            bash_max_output_bytes=cfg.fs_bash_max_output_bytes,
            file_read_default_lines=cfg.fs_file_read_default_lines,
            file_read_max_bytes=cfg.fs_file_read_max_bytes,
            file_read_max_line_bytes=cfg.fs_file_read_max_line_bytes,
            file_read_count_max_bytes=cfg.fs_file_read_count_max_bytes,
            glob_max_results=cfg.fs_glob_max_results,
            bash_venv_python=cfg.fs_bash_venv_python,
        ))
        providers.register_capability(fs_provider)
```

Then in the skills block, build the runner and pass it:

```python
    if skills_dir.exists():
        from ctx_weft.providers.capability_skill_local import LocalSkillCapabilityProvider
        # skill .py 委托给 fs 的 bash_exec(共用 workspace venv);无 fs 时回退本地直跑。
        bash_runner = None
        if fs_provider is not None:
            bash_runner = (
                lambda cmd, ctx, _fs=fs_provider:
                _fs.invoke(f"{_fs.name}:bash_exec", {"command": cmd}, ctx)
            )
        providers.register_capability(LocalSkillCapabilityProvider(
            skills_dir,
            script_timeout_sec=cfg.skill_script_timeout_sec,
            idle_timeout_sec=cfg.skill_idle_timeout_sec,
            hard_cap_sec=cfg.skill_hard_cap_sec,
            output_limit_chars=cfg.skill_output_limit_chars,
            python_executable=cfg.fs_bash_venv_python,
            bash_runner=bash_runner,
        ))
```

- [ ] **Step 4: Verify nothing broke**

Run: `uv run python -c "import ipmastercowork.cli"` (expect no error) and
`uv run pytest tests/unit/test_skill_bash_runner_wiring.py tests/unit/test_skill_exec_python.py -q` (expect PASS).

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/cli.py tests/unit/test_skill_bash_runner_wiring.py
git commit -m "feat(host): wire skill bash_runner to fs.bash_exec

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Part A — `IPMC_PIP_*` settings

**Files:**
- Modify: `src/ipmastercowork/config.py` (docstring, `Settings` dataclass, `from_env`)
- Test: `tests/unit/test_pip_index_env.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_pip_index_env.py`:

```python
"""可配置 pip 源:IPMC_PIP_* 读入 Settings。"""

from ipmastercowork.config import Settings


def test_settings_reads_pip_fields(monkeypatch):
    monkeypatch.setenv("IPMC_PIP_INDEX_URL", "http://mirror/simple")
    monkeypatch.setenv("IPMC_PIP_EXTRA_INDEX_URL", "http://extra/simple")
    monkeypatch.setenv("IPMC_PIP_TRUSTED_HOST", "mirror")
    s = Settings.from_env()
    assert s.pip_index_url == "http://mirror/simple"
    assert s.pip_extra_index_url == "http://extra/simple"
    assert s.pip_trusted_host == "mirror"


def test_settings_pip_fields_default_none(monkeypatch):
    for k in ("IPMC_PIP_INDEX_URL", "IPMC_PIP_EXTRA_INDEX_URL", "IPMC_PIP_TRUSTED_HOST"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.pip_index_url is None
    assert s.pip_extra_index_url is None
    assert s.pip_trusted_host is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_pip_index_env.py -k settings -v`
Expected: FAIL — `Settings` has no field `pip_index_url`.

- [ ] **Step 3: Add the fields**

In `config.py` `Settings` dataclass, add after `fs_bash_venv_python: str | None`:

```python
    fs_bash_venv_python: str | None
    pip_index_url: str | None
    pip_extra_index_url: str | None
    pip_trusted_host: str | None
```

In `from_env()`, add after `fs_bash_venv_python=_str("IPMC_FS_BASH_VENV_PYTHON", None),`:

```python
            fs_bash_venv_python=_str("IPMC_FS_BASH_VENV_PYTHON", None),
            pip_index_url=_str("IPMC_PIP_INDEX_URL", None),
            pip_extra_index_url=_str("IPMC_PIP_EXTRA_INDEX_URL", None),
            pip_trusted_host=_str("IPMC_PIP_TRUSTED_HOST", None),
```

Add to the module docstring env-keys list (near `IPMC_FS_*`):

```
  IPMC_PIP_INDEX_URL / IPMC_PIP_EXTRA_INDEX_URL / IPMC_PIP_TRUSTED_HOST
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_pip_index_env.py -k settings -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ipmastercowork/config.py tests/unit/test_pip_index_env.py
git commit -m "feat(host): IPMC_PIP_* settings for configurable pip index

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Part A — `apply_pip_index_env` + wire into startup + docs

**Files:**
- Modify: `src/ipmastercowork/config.py` (add `apply_pip_index_env`)
- Modify: `src/ipmastercowork/cli.py` (call it in `build_runtime`)
- Modify: `.env.example`
- Test: `tests/unit/test_pip_index_env.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_pip_index_env.py`:

```python
import os
import ipmastercowork.config as cfgmod


def test_apply_pip_index_env_maps_to_pip(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    os.environ.pop("PIP_INDEX_URL", None)
    os.environ.pop("PIP_TRUSTED_HOST", None)
    os.environ["IPMC_PIP_INDEX_URL"] = "http://mirror/simple"
    os.environ["IPMC_PIP_TRUSTED_HOST"] = "mirror"
    monkeypatch.setattr(cfgmod, "_settings", None)
    cfg = cfgmod.get_settings()
    cfgmod.apply_pip_index_env(cfg)
    assert os.environ["PIP_INDEX_URL"] == "http://mirror/simple"
    assert os.environ["PIP_TRUSTED_HOST"] == "mirror"


def test_apply_pip_index_env_noop_when_unset(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in ("IPMC_PIP_INDEX_URL", "IPMC_PIP_EXTRA_INDEX_URL", "IPMC_PIP_TRUSTED_HOST", "PIP_INDEX_URL"):
        os.environ.pop(k, None)
    monkeypatch.setattr(cfgmod, "_settings", None)
    cfg = cfgmod.get_settings()
    cfgmod.apply_pip_index_env(cfg)
    assert "PIP_INDEX_URL" not in os.environ
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_pip_index_env.py -k apply -v`
Expected: FAIL — `module 'ipmastercowork.config' has no attribute 'apply_pip_index_env'`.

- [ ] **Step 3: Implement `apply_pip_index_env`**

In `config.py`, after the `get_settings()` function, add:

```python
def apply_pip_index_env(settings: "Settings") -> None:
    """把 IPMC_PIP_* 映射成标准 PIP_* 写入 os.environ,使所有子进程(bash_exec / 经
    bash 的 skill)的 pip 走同一内网源。仅对已配置项赋值,未配置不动。幂等。"""
    mapping = {
        "PIP_INDEX_URL": settings.pip_index_url,
        "PIP_EXTRA_INDEX_URL": settings.pip_extra_index_url,
        "PIP_TRUSTED_HOST": settings.pip_trusted_host,
    }
    for key, val in mapping.items():
        if val:
            os.environ[key] = val
```

- [ ] **Step 4: Call it from `build_runtime`**

In `cli.py`, update the import:

```python
    from ipmastercowork.config import get_settings, apply_pip_index_env
```

and right after `cfg = get_settings()` in `build_runtime`:

```python
    cfg = get_settings()
    apply_pip_index_env(cfg)  # 内网 pip 源 → 标准 PIP_*，供 bash/skill 子进程继承
```

(If `cli.py` imports `get_settings` elsewhere/differently, just add `apply_pip_index_env` to that import and call it once after `cfg = get_settings()`.)

- [ ] **Step 5: Document in `.env.example`**

In `.env.example`, under the "Filesystem tools (bash auto-venv)" section (after the `IPMC_FS_BASH_VENV_PYTHON` block), add:

```
# Internal PyPI mirror for `pip install` (used by both bash_exec and skill scripts,
# which run pip in the workspace venv). Mapped to the standard PIP_* env vars at
# startup, so all subprocesses inherit them. For an http (non-https) mirror, also set
# the trusted host to avoid pip's TLS error.
# IPMC_PIP_INDEX_URL=http://10.25.x.x:port/simple
# IPMC_PIP_EXTRA_INDEX_URL=
# IPMC_PIP_TRUSTED_HOST=10.25.x.x
```

- [ ] **Step 6: Run tests + import check**

Run: `uv run pytest tests/unit/test_pip_index_env.py -v` (expect PASS) and
`uv run python -c "import ipmastercowork.cli"` (expect no error).

- [ ] **Step 7: Commit**

```bash
git add src/ipmastercowork/config.py src/ipmastercowork/cli.py .env.example tests/unit/test_pip_index_env.py
git commit -m "feat(host): map IPMC_PIP_* to PIP_* at startup for bash+skill pip

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Full-suite regression check

**Files:** none (verification only)

- [ ] **Step 1: Run core + host suites**

Run: `uv run pytest tests/unit -q` and `uv run pytest tests/ -q`
Expected: core green (only the known `test_grep_tool` ripgrep skip); host green except the documented flaky `test_postgres_snapshot_prune_keeps_latest_n` (confirm it passes in isolation: `uv run pytest tests/test_snapshot_writer.py::test_postgres_snapshot_prune_keeps_latest_n -q`).

- [ ] **Step 2: (no commit — verification task)**

---

### Task 8: Record core changes for upstream backfill

**Files:**
- Modify: memory `upstream-port-queue.md`

- [ ] **Step 1: Append a backfill entry**

Add an entry to the `upstream-port-queue` memory: Tasks 1–3 changed core files
`capability_filesystem/provider.py` (bash_exec `extra_env` merge + `invoke()` setdefault for the 3 limits) and `capability_skill_local/provider.py` (`bash_runner` delegation). These must be ported to `ctx_wefta` (weft→wefta) before the next sync. Reference the Task 1–3 commit SHAs. Brand-neutral. Part A and host wiring (cli/config/.env) are host-only — no backfill.

- [ ] **Step 2: (no code commit — memory only)**

---

## Self-Review

**Spec coverage:**
- Part B core seam 1 (extra_env) → Task 1 ✓
- Part B core seam 2 (timeout override) → Task 2 ✓
- Part B skill delegation (command, SKILL_DIR, skill timeouts, result/error→return/raise, fallback) → Task 3 ✓
- Part B host wiring (capture fs, bash_runner) → Task 4 ✓
- Part A settings → Task 5 ✓; apply + startup + .env.example → Task 6 ✓
- Backfill → Task 8 ✓
- Regression incl. known flake → Task 7 ✓

**Placeholder scan:** none — every code/command step is concrete. The `.env.example` mirror host/port is an illustrative example value, not a placeholder to fill in code.

**Type/name consistency:** `bash_runner` (param + attr `self._bash_runner`), `extra_env` (ctx.extra key, both producer in Task 3 and consumer in Task 1), `_exec_via_bash`/`_exec_direct` (Task 3), `apply_pip_index_env` + `pip_index_url`/`pip_extra_index_url`/`pip_trusted_host` (Tasks 5–6), env keys `IPMC_PIP_*` → `PIP_*`. `result`/`error` event kinds and `payload["content"]`/`metadata["exit_code"]` match `bash_exec` (provider.py:257-260). The fallback `_exec_direct` reproduces the pre-existing direct-run body verbatim so existing `test_skill_exec_python.py` tests stay green.
