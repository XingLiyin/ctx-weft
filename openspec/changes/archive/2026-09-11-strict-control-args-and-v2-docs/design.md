# Design: strict-control-args-and-v2-docs

## Context

见 proposal.md 的 Why。当前实现的关键约束（已探查确认）：

- 剥未知键是 gateway 层被约 15 个测试有意锁定的设计（`test_strip_unknown_keys.py` 等）：动机是模型臆造键与畸形缓冲救援碎片，**不能全局反转**。
- 错误反馈管道已现成：`[Error: invalid arguments for '{tool}': ...]` 格式（gateway `_validate_args` 失败路径）+ `InvocationResult(is_error=True)`；act 循环把错误 append 进 `current_messages`（act.py 既有语义），LLM 同 run 可见可重试。本设计**零新通道**。
- gateway 已 `from ctx_weft.core.capabilities.control_tools import PROVIDER_NAME as CONTROL`，且 `DISPATCH_TOOLS` / `SILENT_TOOLS` 已按 `qualify(f"{CONTROL}:…")` 识别控制工具；控制工具 capability id 形如 `control:finish_task`。
- `run_observe_react`（observe.py L172-173）对 terminal 工具的结果不检查 `is_error`——必填缺失今天就会以错误文案终止观察循环（预存缺陷），收紧参数校验后触发面扩大，必须一并修复。
- 控制工具 schema 由 `@control_tool` 装饰器经 `extract_schema` 从函数签名生成（扁平、含 `properties`、无 `additionalProperties`），结构精确可控，这是「控制工具可以安全收紧」的前提。
- ARCHITECTURE.md（499 行）相对 v2 实现的 11 节漂移地图已完成（含精确 file:line），重写有事实底稿。

## Goals / Non-Goals

**Goals:**

- 控制工具的参数误用对 LLM 可见、可恢复（同 run 重试），交付物不再静默丢失。
- 收紧范围精确：只影响 `cap.id` 以 `control:` 开头的工具；非控制工具行为逐字节不变。
- observe 循环对 terminal 工具失败容错（修预存缺陷）。
- ARCHITECTURE.md 与 v2 代码一致，并把新校验行为写进 §6。

**Non-Goals:**

- 不改 `_strip_unknown_keys` 对非控制工具的语义与其全部锁定测试。
- 不改 SILENT_TOOLS 错误不落 memory 的既有语义（错误仍只在 run 内可见）。
- 不动 ControlCapabilityProvider `_handle` 的第二层签名过滤（防御性冗余，保留）。
- 不在本 change 内同步 README.md（如需另开 change）。
- 不为外部工具 schema 引入 `additionalProperties` 强制校验（spec B 决策不变）。

## Decisions

### D1：收紧范围 = 控制工具前缀判定，而非协议级开关

用 `cap.id.startswith(f"{CONTROL}:")` 判定，与 `DISPATCH_TOOLS` / `SILENT_TOOLS` 的既有识别方式同构。

- 备选 1：给 `ToolCapability` 协议 dataclass 加 `strict_args` 字段——协议面变更，波及全部 provider 构造点，收益仅是「不耦合 control 命名」；而 gateway 事实上已 import CONTROL 并按其前缀分派，耦合早已存在。弃。
- 备选 2：仅对已知误用名（`result` / `output`）报错——黑名单式补丁，模型换个臆造键仍静默丢失。弃。

### D2：插入点与资格判定复用

在 `invoke()` 的 `_raw` 哨兵检查之后、`_strip_unknown_keys` 之前插入严格检查。剥键资格判定（schema 非 dict / 无 `properties` / 组合关键字 / `$ref` / 显式 `additionalProperties` → fail-open）从 `_strip_unknown_keys` 抽出共享 helper，严格检查与剥键共用同一判定——「strip 不适用的 schema，strict 同样不适用」，spec 场景三由此保证。通过严格检查后（无未知键）strip 成为恒等操作，非控制工具路径零改动。

### D3：错误文案与出口完全复用既有管道

`[Error: invalid arguments for '{tool}': unknown parameter(s): 'result'; declared parameters: deliverables_summary — re-send the call with only the declared parameters]`，经 `_error_and_record` 返回（自动遵循 is_dispatch / is_silent 的落库语义）。与 unknown-tool / required-missing 同出口，不新增事件类型、不新增 memory 写入。

### D4：observe terminal 守卫 = `is_error` 不终止

`run_observe_react` 中 `if tc.name == terminal_tool_name` 的赋权加 `and not result.is_error`；错误内容照常 append 进 `current_messages`（现有代码已做）供模型下轮修正。轮次耗尽走既有 `(None, last_text)` 兜底，与纯文本超时路径一致。前台 observe 与后台 background_observe 共用此函数，一处修复两处受益。

### D5：根因 docstring 修正随行

`control_tools.py` L415（report_task_outcome.task_summary）与 L514（collect_process_report.task_summary）删除「goes in finish_task's `result`」措辞，改为「最终答复 = 收尾回合正文」。只改校验不改这两句，同类误用会持续发生。

### D6：ARCHITECTURE.md 整篇重写而非补丁

漂移遍布全部 11 节（组件路径表、step 流水线、记忆词表、编排、恢复、事件系统），逐条补丁不可维护。保留原文档结构、风格、mermaid + ASCII 双图形态，按漂移地图逐节重写；行号在写作时抽查核实（约 10 个承重锚点），文首「行号对应当前 master」说明改为当前分支。新校验行为写入 §6（CapabilityGateway 节）。

### D7：受影响测试 mock 的修改口径

既有 mock 模拟 LLM 传无效 `result=` 的调用点（约 8 处）按「去掉无效键；若断言依赖该文本则挪进 `MockResponse(text=...)`」机械修改。这是收紧后的**预期可见效应**（测试里的误用从被吞改为被拒），不是迁就实现的改测试。纯 memory fixture 型用例（`test_capsule_interleaved` / `test_legacy_capsule_recall`，构造的是存量 memory 记录、不经 gateway）不动。

## Risks / Trade-offs

- [线上模型习惯性带无效参数调用控制工具 → 消耗额外轮次重试] → act 循环既有 max_turns + observe retry 兜底；错误文案明确列出声明参数，单轮即可纠正；对照静默丢失交付物的后果，此代价可接受。
- [严格检查在冷路径（reconcile 重入）行为不一致] → 检查在 `invoke()` 内、coerce 之后，所有调用路径（act / observe / background_observe / reconcile）共用同一入口，天然一致。
- [观察循环重试放大轮次消耗] → 仅 terminal 工具失败时多耗轮次；轮次耗尽兜底已存在（`(None, last_text)`），不产生新失败模式。
- [ARCHITECTURE.md 重写引入新的不准确] → 全部内容以漂移地图的 file:line 为底稿；写完后抽查锚点 + grep 核实；文档不准确的风险低于维持 v1 旧文的误导风险。
- [收紧跟其他改动冲突] → 改动面集中在 gateway 单点插入 + observe 单行守卫 + 2 处 docstring，git 冲突面小。

## Migration Plan

单仓顺序交付，无需运行时迁移：代码收紧与测试更新同一 commit（否则 CI 必红）；ARCHITECTURE.md 可独立 commit。回滚 = revert 单个 commit，无数据/协议迁移。

## Open Questions

（无——范围、判定方式、文案形态、测试口径均已在探查中确认。）
