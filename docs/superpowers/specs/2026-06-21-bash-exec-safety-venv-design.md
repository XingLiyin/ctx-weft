# bash_exec 安全加固 + Python .venv 自动引导 — 设计

日期：2026-06-21
目标文件：`src/ctx_weft/providers/capability_filesystem/`

## 背景与动机

`bash_exec`（`provider.py:155`）是 FilesystemToolsProvider 暴露给 LLM 的 shell 执行工具，以 session workspace 为 cwd。当前有两处不足：

1. **安全检查过浅**：只把 `shlex.split` 后的 `tokens[0]` 与 `_BASH_BLACKLIST` 比对。任何把危险命令放在非首词的写法都能绕过：
   - 链式：`git status && rm -rf foo`、`echo x; del bar`
   - 管道：`cat f | sudo tee g`
   - 绝对路径：`/bin/rm x`、`C:\Windows\System32\del.exe y`
   - 环境前缀（POSIX）：`FOO=1 rm x`
   - 命令替换：`$(rm x)`、`` `rm x` ``
2. **Python 无隔离环境**：执行 `python`/`pip` 直接落到宿主解释器，污染全局、且行为不可复现。期望：检测到 Python 类命令时，在 workspace 下懒创建并“激活” `.venv`。

## 约束

- **core 品牌中性**（见 `upstream-port-queue` 记忆）：不读 env 文件、不硬编码 host/产品品牌。所有可调项走 `FilesystemConfig` 字段 → `invoke()` 注入 `ctx.extra` → 工具内 `ctx.extra.get(...)` 读取，与现有 limits 一致。
- **每次 `bash_exec` 是全新子进程**（`run_with_liveness`），shell `activate` 不会跨调用持久。因此“激活”只能在子进程 env 层做。
- **跨平台**：Windows 经 `cmd.exe`，POSIX 经 `/bin/sh`；venv bin 目录与可执行名不同。
- **回灌上游**：落地后按 port-queue 约定逐文件 `weft→wefta` sed 回灌 `ctx-wefta`，并更新 `upstream-port-queue` 记忆。（spec/plan 文档在 sync 不变量之外，不回灌。）

## 决策（已与用户确认）

| 维度 | 选择 |
|------|------|
| 改动落点 | 直接改 core + 进回灌队列 |
| 安全力度 | 分段扫全部命令词（黑名单内容不扩充）+ 拦命令替换 |
| venv 机制 | 懒创建 + 注入 PATH（不改写命令、不依赖 activate 脚本） |
| Python 识别 | 首词 ∈ `{python, python3, py, pip, pip3}`（去目录/去 .exe/大小写不敏感） |
| 代码组织 | 安全逻辑入 `_bash_safety.py`；venv 入 `_venv.py`（与 `_grep.py`/`_file_reader.py` 同风格） |
| venv 创建失败 | 硬错误（发 `VENV_ERROR` 事件并终止），venv 视为前置条件 |
| 命令替换 | 直接拦截（不扫描内部） |
| 特性开关 | 默认开启（`bash_auto_venv=True`） |

## 架构

### 新模块 `_bash_safety.py`

把 `_BASH_BLACKLIST` 从 `provider.py` 迁入此处（`_bash_exec_description()` 改为从此导入），令“黑名单的定义、解析、判定”聚于一处。

```python
BASH_BLACKLIST: frozenset[str]          # 由 provider.py 旧常量迁入，内容不变

def split_segments(command: str) -> list[str]:
    """按 shell 连接符把命令拆成会各自起一条命令的段。
    连接符：;  &&  ||  |  &  以及换行。"""

def command_word(segment: str) -> str | None:
    """取一段里真正的命令词，做归一化：
      1. shlex.split（失败回退 str.split）
      2. 跳过前导 VAR=val 环境赋值 token
      3. 取首个真实 token → basename（剥 /bin、C:\\...\\）→ 去 .exe/.bat/.cmd → lower()
    空段返回 None。"""

def check_command_safety(command: str) -> str | None:
    """返回错误消息字符串（命中）或 None（放行）。
      - 含命令替换 $(...) 或反引号 → 返回拦截原因
      - 逐段 command_word，命中 BASH_BLACKLIST → 返回 "Command '<word>' is not allowed"
    """
```

`command_word` 的归一化设计直接消解“绝对路径/.exe 后缀/环境前缀”三类绕过。

### 新模块 `_venv.py`

依赖 `_bash_safety` 的 `split_segments`/`command_word` 做分段解析（两者同为 bash_exec 内部件，耦合可接受）。

```python
PYTHON_COMMANDS: frozenset[str] = {"python", "python3", "py", "pip", "pip3"}

class VenvError(RuntimeError): ...

def command_is_python(command: str) -> bool:
    """任一段首词 ∈ PYTHON_COMMANDS。"""

def venv_layout(workspace: Path, venv_dir: str) -> tuple[Path, Path, Path]:
    """返回 (venv_path, bindir, python_exe)。
    Windows: bindir=venv/Scripts, python_exe=python.exe
    POSIX:   bindir=venv/bin,     python_exe=python"""

async def ensure_venv(workspace: Path, venv_dir: str) -> bool:
    """懒创建：python_exe 已存在则返回 False（无操作）；
    否则用宿主 sys.executable -m venv 创建，返回 True。
    用模块级 per-workspace asyncio.Lock 串行化，避免并发 python 调用竞态。
    创建失败 raise VenvError。"""

def venv_env(env: dict[str, str], venv_path: Path) -> dict[str, str]:
    """返回注入了 venv 的 env 副本：
      - PATH 头部插入 bindir
      - VIRTUAL_ENV = venv_path
      - 删除 PYTHONHOME（若有）"""
```

模块级 `_VENV_LOCKS: dict[str, asyncio.Lock]`（键=workspace 绝对路径）。`ensure_venv` 在锁内二次检查存在性（double-checked），避免重复创建。`sys.executable` 即宿主解释器，品牌中性、不读 env。

### `provider.py` 改动

1. 删除本地 `_BASH_BLACKLIST`，改 `from ._bash_safety import BASH_BLACKLIST`（描述函数随之引用）。
2. `bash_exec` 流程（替换现有 tokens[0] 检查）：
   ```
   if not command.strip(): -> EMPTY_COMMAND（不变）
   err = check_command_safety(command)
   if err: -> CapabilityEvent(error, code="COMMAND_BLACKLISTED", message=err); return
   yield progress "starting"（不变）
   ws = _workspace(ctx); cwd = ...; idle/hard/max_out（不变）
   env = {**os.environ, PYTHONIOENCODING: utf-8}（不变）
   # venv 引导
   auto_venv = ctx.extra.get("bash_auto_venv", True)
   venv_dir  = ctx.extra.get("bash_venv_dir") or ".venv"
   if auto_venv and ws and command_is_python(command):
       venv_path, _, python_exe = venv_layout(ws, venv_dir)
       if not python_exe.exists():
           yield progress {"status": "creating_venv", "path": str(venv/dir)}
       try:
           await ensure_venv(ws, venv_dir)
       except VenvError as e:
           yield CapabilityEvent(error, code="VENV_ERROR", message=str(e)); return
       env = venv_env(env, venv_path)
   # 之后 runner 逻辑不变
   ```
3. `_bash_exec_description()` 增一句中性说明：python/pip 在工作目录下自动创建并使用 `.venv`。

### Config

`FilesystemConfig` 增字段：
```python
bash_auto_venv: bool = True
bash_venv_dir: str = ".venv"
```
`invoke()` 注入：
```python
extra["bash_auto_venv"] = self._cfg.bash_auto_venv
extra["bash_venv_dir"]  = self._cfg.bash_venv_dir
```

## 错误处理

| 情形 | 行为 |
|------|------|
| 空命令 | `EMPTY_COMMAND`（不变） |
| 命中黑名单（任一段）/命令替换 | `COMMAND_BLACKLISTED`，消息含命中词或原因 |
| venv 创建失败 | `VENV_ERROR`，终止；不静默降级裸跑 |
| 无 workspace | 跳过 venv 引导（无处落 .venv），命令照常执行 |
| `bash_auto_venv=False` | 跳过 venv 引导 |

## 测试

`tests/unit/test_bash_safety.py`：
- 放行：`echo ok`、`git status && echo done`、`ls | head`（POSIX 语义下的合法复合）
- 拦截：`git status && rm -rf x`、`echo a; del b`、`cat f | sudo tee g`、`/bin/rm x`、`C:\\Windows\\System32\\del.exe y`、`FOO=1 rm x`、`echo $(rm x)`、`` echo `rm x` ``
- `command_word` 归一化单测（大小写、.exe、目录前缀、环境赋值前缀、空段）

`tests/unit/test_bash_venv.py`：
- `command_is_python`：`python a.py`/`pip install x`/`py -3 a.py`/`PY a.py` 为真；`node a.js`/`pytest`/`git` 为假
- `venv_layout` 按平台给出正确 bindir/可执行名
- `ensure_venv` 首调创建、再调免创建（mock `sys.executable -m venv` 或在 tmp 真建小 venv）；失败 raise `VenvError`
- `venv_env` 正确插 PATH 头、设 VIRTUAL_ENV、删 PYTHONHOME
- 集成式：`bash_exec` 对 `python -V`（带 workspace）触发 ensure+注入；`bash_auto_venv=False` 不触发；非 python 命令不触发

并扩 `test_fs_config.py`：新两字段默认值 + 经 `invoke()` 进 `ctx.extra`。

## 回灌

落地 + core 测试绿后，按 `upstream-port-queue` 约定把 `_bash_safety.py`/`_venv.py`/`provider.py` 及两个测试逐文件 `weft→wefta` sed 回灌至 `C:\Users\Xing\Documents\codes\LoomeX-00\ctx-wefta`，更新 `upstream-port-queue` 记忆队列状态。

## 不做（YAGNI）

- 不扩充黑名单内容（只改判定范围）
- 不做路径围栏 / redirect 目标审查（本次明确排除）
- 不支持 uv/poetry/pytest 等触发（只 python/pip 家族）
- 不改写命令为 `activate &&`（用 env 注入替代）
- 不做 venv 复用缓存/跨 session 共享（per-workspace 懒建即可）
```
