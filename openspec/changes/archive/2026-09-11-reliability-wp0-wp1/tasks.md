# Tasks: reliability-wp0-wp1

## 1. WP1 验收测试先行（基线必红）

- [x] 1.1 新建 `tests/unit/test_gateway_argument_channels.py` 五组用例（fixture 沿用 `test_gateway_arg_validation.py` 的 `_state_ctx`/`_gw` 风格）：① 原始认证头 → provider 收明文；② HITL 改写认证头 → provider 收改写后明文且过校验；③ 非敏感字段不受脱敏影响；④ 调用方传入 dict 全程不被修改；⑤ TOOL_AUDIT/审计记录中认证头为 `***`
- [x] 1.2 在未改实现的基线上运行该文件，确认 ①② **失败**（provider 收到 `***`）且失败原因即 H4，记录输出作为「现象成立」证据

## 2. WP1 最小实现（H4 修复）

- [x] 2.1 `capability_gateway.py`：`invoke` 参数管线拆通道——`audit_args = _sanitize(effective_args)`（改名+注释收窄为审计副本语义），`_record_invocation` 收 `audit_args`（不变），`_stream_tool` 改收 `effective_args`；`_stream_tool` 内 `provider.invoke` 同步改执行参数，并核实现存对该参数对象的任何事件序列化点走独立脱敏机制（design D2）
- [x] 2.2 验证 `uv run pytest tests/unit/test_gateway_argument_channels.py tests/unit/test_gateway_arg_validation.py tests/unit/test_gateway_authz_hitl.py tests/unit/test_event_redaction.py -q` 全绿（1.1 的 ①② 由红转绿）
- [x] 2.3 全量回归 `uv run pytest tests/unit tests/integration -q -W ignore`：与既知基线一致（unit 2841 过/1 预存、integration 107 过/1 预存），零新增失败；若有测试断言 `***` 到达 provider，逐一确认为有意翻转并记录
- [x] 2.4 探针交叉验证：`uv run python docs/plans/verification/verify_agent_architecture.py --expect baseline` 中 **H4 由 true 转 false**（provider 收到 `FAKE_TEST_TOKEN`）而 H1/H2/H3 保持 true；更新方案文档 §1.2 的探针说明（H4 已修复的标注方式按文档自身口径，不删故障注入）

## 3. WP0 基线夹具（钉住 H1/H2/H3 缺陷现状）

- [x] 3.1 新建 `tests/integration/test_runtime_storage_failure.py`（H1）：完整 Runtime + FailingStore，barrier 控时序，断言旧契约（emit 不抛 / 观察者收到 / stored=0 / 会话继续推进），注释标明「钉旧契约，WP3 按 required 提交门翻转」
- [x] 3.2 新建 `tests/integration/test_snapshot_commit_interleaving.py`（H2）：双 task 未提交窗口交错（B 触发快照后 A 才 commit_provisional），断言全量回放={a,b} 而快照恢复={b}，注释标明「WP4 按 position 游标翻转」；用 asyncio.Event 控序，不用 sleep
- [x] 3.3 新建 `tests/integration/test_tool_outcome_unknown.py`（H3）：工具结果 memory 写入失败后 ReconcileStep 重跑（外部副作用计数 ×2），含子进程 SQLite 计数器夹具（副作用证据跨进程退出可读，tmp_path 隔离，仅 stdlib），注释标明「WP6 按恢复策略翻转」
- [x] 3.4 验证三份夹具与探针输出语义一致（与 `verify_agent_architecture.py` 对应 H 段的 observed 值同构），并在每份文件 docstring 里回链探针与方案条目

## 4. 文档与收尾

- [x] 4.1 ARCHITECTURE.md §6 参数管线描述补一句「执行参数与审计参数分离（audit 副本仅用于 TOOL_AUDIT/事件，Provider 收 effective 未脱敏参数）」，行号如有移动一并校正
- [x] 4.2 跑 `openspec validate "reliability-wp0-wp1"` 通过；工作包级提交拆分：夹具 commit（纯新增）与 H4 修复 commit（测试+实现）分开，均可独立回滚
