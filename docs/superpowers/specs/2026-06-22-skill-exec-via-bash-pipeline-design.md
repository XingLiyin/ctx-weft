# Skill exec_script 复用 bash_exec 流水线 + 可配置 pip 源 — 设计

**日期:** 2026-06-22
**状态:** 已确认,待 writing-plans

本 spec 含两块相关改动:
- **Part B(主):** skill `exec_script` 复用 `bash_exec` 流水线,使 skill `.py` 跑在 workspace `.venv` 里(core + host)。
- **Part A:** 可配置内网 pip 源,bash 与 skill 的 `pip install` 同源(纯 host,无 core 改动)。

## 背景与问题

`LocalSkillCapabilityProvider.exec_script` 现在对 `.py` 脚本拼 `python "<脚本>"`,经 `run_with_liveness` 在 `skill_dir` 下直跑,解释器来自内置 runtime 或 PATH。它**不进 venv**,所以 agent 通过 `bash_exec` 在 workspace `.venv` 里 `pip install` 的第三方依赖,skill 脚本看不到。

`bash_exec`(fs 提供者)已经有一整套:workspace 作 cwd、Python `.venv` 自举/激活(用内置 creator python)、命令安全扫描、活性超时、可靠杀树、Windows 隐藏黑窗、输出上限。

**目标:** 让 skill 的 `.py` 脚本复用 `bash_exec` 这条流水线,从而自动跑在 **per-session workspace `.venv`** 里(与 bash 同一个),agent `pip install` 的依赖直接可用。命令式:agent 先在 bash 里装依赖,再跑 skill。

## 非目标(YAGNI)
- 不引入依赖声明文件、不自动装依赖(由 agent 命令式负责)。
- 不单独为 skill 建 venv —— 复用 workspace `.venv`。
- `venv_dir` 固定 `.venv`(与 `FilesystemConfig` 默认一致)。
- pip 源不做 per-skill / per-session 覆盖 —— 全局一套(进程级 env)。

## 架构

`exec_script` 把真正的执行**委托给 fs 的 `bash_exec`**(通过注入的 `bash_runner` 回调),自己只负责:把脚本解析成绝对路径、拼命令、注入 `SKILL_DIR` 与 skill 自己的超时、消费事件转成返回值/异常。无 `bash_runner` 时回退到现有直跑路径。

数据流:
```
exec_script(skill, script, args, ctx)
  → 解析绝对脚本路径(skill_root + script,带 escape 防护)
  → 命令 = `python "<abs>" <args>`(非 .py:`"<abs>" <args>`)
  → ctx.extra 注入 extra_env={SKILL_DIR} + skill 超时
  → bash_runner(命令, ctx)            # = fs.invoke("fs:bash_exec", {"command": …}, ctx)
       → fs.invoke 注入 workspace/venv 配置(setdefault 超时)→ bash_exec
       → venv 自举/激活、安全扫描、活性超时、隐藏黑窗、跑命令
       → yield stdout / error / result(content + exit_code)
  → 收集 result.content;error 事件或非零 exit_code → RuntimeError;否则返回 content(截断 output_limit)
```

## 组件与改动

### core(`ctx_weft`,品牌中性,需回灌上游 `ctx_wefta`)

**1. `capability_filesystem/provider.py` — 让 `bash_exec` 接受调用方覆盖**
- `FilesystemToolsProvider.invoke()`:把 3 个限额 `bash_idle_timeout_sec` / `bash_hard_cap_sec` / `bash_max_output_bytes` 从"总是写 fs 配置"改为 `setdefault`(ctx.extra 已有则保留)。
  - **仍由 fs 强制(不变,总是覆盖):** `workspace`、`allowed_dirs`、`bash_auto_venv`、`bash_venv_dir`、`bash_venv_python`、各 file_read/glob/grep 限额。调用方不能改这些。
- `bash_exec`:在构造子进程 env 时读 `ctx.extra.get("extra_env")`(dict),并进 env;顺序为 `{**os.environ, "PYTHONIOENCODING": "utf-8", **extra_env}`,随后若进 venv 再 `venv_env(env, venv_path)`(`venv_env` 复制 dict、保留 `SKILL_DIR` 等键)。`extra_env` 缺省为空 dict,行为不变。

**2. `capability_skill_local/provider.py` — 委托执行**
- 构造新增 `bash_runner: Callable[[str, ProviderContext], AsyncIterator[CapabilityEvent]] | None = None`;保留上次加的 `python_executable`(仅在无 `bash_runner` 的回退路径用)。
- `exec_script`:
  - 解析绝对脚本路径(沿用现有 escape 防护)。
  - 若 `bash_runner` 非 None:
    - 命令:`.py` → `f'python "{abs}" {args}'`(裸 `python`,由 bash_exec 的 venv 激活解析);非 .py → `f'"{abs}" {args}'`。args 为空则不追加。
    - 注入 ctx.extra:`extra_env={"SKILL_DIR": str(skill_root)}`、`bash_idle_timeout_sec=self._idle_timeout_sec`、`bash_hard_cap_sec=self._hard_cap_sec`、`bash_max_output_bytes=self._output_limit_chars`(用 `dataclasses.replace(ctx, extra={**ctx.extra, …})`)。
    - `async for ev in bash_runner(cmd, ctx2)`:`kind=="result"` 收 `payload["content"]` 与 `metadata["exit_code"]`;`kind=="error"` → 抛 `RuntimeError(payload["message"])`。
    - 非零 exit_code → `RuntimeError`(带 content);否则返回 content 截断到 `output_limit_chars`。
  - 若 `bash_runner` 为 None:走现有直跑(`run_with_liveness` + `skill_dir` cwd + `python_executable`/裸 python),保持现状。

### host(`src/ipmastercowork`,无需回灌)

**`cli.py`** — 捕获 fs provider 实例,接线 skill 的 `bash_runner`:
```python
fs_provider = FilesystemToolsProvider(FilesystemConfig(...))
providers.register_capability(fs_provider)
...
bash_runner = None
if enable_tools:  # 有 fs 才接
    bash_runner = lambda cmd, ctx: fs_provider.invoke(
        f"{fs_provider.name}:bash_exec", {"command": cmd}, ctx
    )
providers.register_capability(LocalSkillCapabilityProvider(
    skills_dir, ..., python_executable=cfg.fs_bash_venv_python, bash_runner=bash_runner,
))
```

## Part A:可配置 pip 源(纯 host,无 core 改动)

**原理:** pip 原生读环境变量 `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` / `PIP_TRUSTED_HOST`。bash_exec 子进程 env = `{**os.environ, …}`,skill 现在经 bash_exec,二者都继承 `os.environ`。所以 host 只要在启动时把配置写进 `os.environ`,**所有 pip 调用(agent 在 bash 里的 `pip install`、skill 经 bash 的安装)自动同源**。venv 创建本身不需要源(用 PBS 自带 wheel)。

**host 改动:**
- `config.py` `Settings` 新增三个可选字段,从 env 读:
  - `pip_index_url` ← `IPMC_PIP_INDEX_URL`
  - `pip_extra_index_url` ← `IPMC_PIP_EXTRA_INDEX_URL`
  - `pip_trusted_host` ← `IPMC_PIP_TRUSTED_HOST`
  (均 `_str(key, None)`,缺省 None。)
- 新增 host 函数 `apply_pip_index_env(settings)`:对每个非空字段,设 `os.environ["PIP_INDEX_URL"]=…` 等(IPMC_ 值权威,直接赋值)。在 `cli.build_runtime` 早期调用一次(`serve` 与冻结态 `_run` 都经 `build_runtime`,故两路都覆盖)。幂等。
- `.env.example` 文档化这三个 `IPMC_PIP_*`。

**说明:** 内网 http 镜像通常要配 `IPMC_PIP_TRUSTED_HOST=<host:port>` 以免 pip 因非 https 报错。直接在 `.env` 里设标准 `PIP_*` 也能用(load_dotenv → os.environ),`IPMC_PIP_*` 只是纳入 IPMC_ 命名并文档化。

**测试(host):**
- `Settings.from_env` 读到三个字段;缺省为 None。
- `apply_pip_index_env`:给定设了 `IPMC_PIP_INDEX_URL` 的 settings → `os.environ["PIP_INDEX_URL"]` 被设;全为 None → 不动 `os.environ`(用 env 快照隔离)。

## 行为净变化
- skill `.py` 跑在 workspace `.venv` 里;venv 缺失按 bash 同一套自动建(内置 creator python)。
- 配了 `IPMC_PIP_*` 后,agent 在 bash 里 `pip install` 与 skill 安装都走内网源。
- cwd 由 `skill_dir` 变为 **workspace**;`SKILL_DIR` 环境变量保留(脚本仍可定位自身资源)。
- skill 脚本用 **skill 自己的超时**(idle 90 / hard 600),非 bash 的。
- 命令安全黑名单开始覆盖 skill 命令(预期收益)。
- 非 `.py` 脚本:也经 bash(绝对路径直跑),享受隐藏黑窗等,但不进 venv。
- 成功返回的 content **包含 stdout+stderr**(bash_exec 已合并),与旧版只返回 stdout 略有不同;通常更有用(stderr 里的诊断也带回)。

## 边界情况
- **无 workspace 登记**(bash_exec `ws=None`):bash_exec cwd=None、不进 venv → 裸 `python` 跑。打包态每 session 都有 workspace,故罕见;dev 无 `--workspace` 时如此。可接受。
- **无 fs / `enable_tools=False`**:`bash_runner=None` → skill 回退直跑(内置/裸 python,skill_dir cwd),即上次改动的行为。
- `venv_env` 保留 `extra_env` 注入的 `SKILL_DIR`(它只改 PATH/VIRTUAL_ENV、删 PYTHONHOME,其余键原样复制)。

## 测试
**core — `bash_exec` 口子:**
- `invoke()`:ctx.extra 预设 `bash_hard_cap_sec` 时不被 fs 配置覆盖(setdefault);未预设时仍用 fs 配置。
- `bash_exec`:ctx.extra 带 `extra_env={"SKILL_DIR": X}` 时,spy `create_subprocess_shell` 断言子进程 env 含 `SKILL_DIR=X`;且进 venv 后仍保留。

**core — `exec_script` 委托:**
- 假 `bash_runner` 捕获 `(command, ctx)`:`.py` → 命令为 `python "<abs>" args`;ctx.extra 含 `extra_env.SKILL_DIR` 与 skill 超时;假 runner 产 `result` 事件 → 返回其 content;产 `error` 事件 / 非零 exit_code → 抛 `RuntimeError`。
- 非 `.py` → 命令为 `"<abs>" args`(无 `python` 前缀)。
- `bash_runner=None` → 回退仍经 `run_with_liveness`(沿用现有测试)。

**host:**
- `cli.py` 给 skill provider 接上的 `bash_runner` 非 None,且调用它会路由到 `fs.invoke` 的 bash_exec(可用一个最小集成断言:provider 持有可调用的 bash_runner)。

## 回灌
**Part B 的 core 改动**(`provider.py` 两处 + `capability_skill_local/provider.py`)落在 `ctx-weft/`,须按 `weft→wefta` 回灌上游 `ctx_wefta`;记入 `upstream-port-queue` 记忆。
**Part A 全部、Part B 的 host 部分**(`config.py`/`cli.py`/`.env.example`/`_run` 相关)都在 `src/ipmastercowork/`,**不回灌**。
