# ctx-weft 开发计划 v2

> 基于 `ctx-weft_设计文档.md` v0.17 + Phase 0/1 已落地代码（2026-05-16）
>
> 这是 v1 计划的修订版，反映 **已完成的实际工作** + **基于 Phase 1 经验调整的后续计划**。
>
> v1 计划文档：`ctx-weft_开发计划.md`（保留作历史参考）

---

## 一、当前状态

### 1.1 已完成（Phase 0 + 1，约 3 周工作量）

**包结构**（最终采用**单包 + 子模块**，非 v1 的 4 包 workspace）：

```
ctx-weft/
├── pyproject.toml          # 单 `CtxWeft` 包，extras：[host] [llm] [dev]
├── .python-version (3.11)
├── .github/workflows/ci.yml
├── .pre-commit-config.yaml
├── setup-py311.ps1         # 一键 cleanup + 验证脚本
├── README.md
├── src/CtxWeft/
│   ├── protocols/          # 5 files: knowledge / memory / capability / template / context
│   ├── core/
│   │   ├── orchestrator/   # 骨架（Phase 4）
│   │   ├── loop/           # ✅ driver.py + steps/{reason,act,observe,finalize}.py
│   │   ├── assembler/      # ✅ assembler/budget/composer + sources/×7
│   │   ├── control/        # 骨架（Phase 6）
│   │   ├── events/         # ✅ bus.py (InProcessEventBus) + types.py (65 events)
│   │   ├── llm/            # ✅ client.py protocol + types.py + mock.py
│   │   ├── auth/           # 骨架（Phase 4）
│   │   ├── state/          # ✅ models.py + store.py
│   │   ├── runtime.py      # ✅ CtxWeftRuntime + ProviderRegistry + RunHandle
│   │   ├── errors.py / utils.py
│   ├── providers/
│   │   └── memory_blackboard/in_memory.py  # ✅
│   │   └── (capability_*, knowledge_file 骨架)
│   └── host/               # 全骨架（Phase 8）
└── tests/
    ├── unit/test_protocols_imports.py       # ✅ 2 tests
    └── integration/test_minimal_loop.py     # ✅ 2 tests
```

**代码量**：57 Python files / ~4000 LOC

**测试**：4/4 通过（M1 已达成）
- `test_minimal_echo_loop`：完整 reason→act→observe→finalize
- `test_prompt_structure_matches_miniagents`：Composer 输出格式与 miniAgents 对齐
- `test_imports` / `test_template_construction`：协议层 smoke

### 1.2 已建立的硬约定

| 决策 | 落地位置 |
|------|--------|
| 单 `CtxWeft` 包，子模块 protocols/core/providers/host | `src/CtxWeft/` |
| Python ≥ 3.11（StrEnum / datetime.UTC / asyncio.TaskGroup） | `pyproject.toml` |
| 65 个事件类型冻结清单 | `events/types.py::EVENT_TYPES` |
| 7 个 V1 内置 Source | `assembler/sources/` |
| Composer 输出 miniAgents 风格 prompt（单 user message） | `assembler/composer.py` |
| Memory ingest 契约（USER_PROMPT / LLM_RESPONSE / OBSERVER_SUMMARY） | `loop/steps/{act,finalize}.py` |
| ULID 主键 + 单进程 InProcessEventBus | `events/bus.py` + `utils.py` |

### 1.3 与 v1 计划的偏差（学到的教训）

| v1 计划 | v2 调整 | 原因 |
|--------|--------|------|
| 4 个 PyPI 包（uv workspace） | 单 `CtxWeft` 包 | 过早优化打包；当前阶段单包简单 5 倍 |
| Phase 0 + 1 = 3-4 周 | 实际 1 session（密集） | 协议先写干净后续顺水推舟 |
| Phase 1 需要 mock LLM、in-memory provider、4 个 step、7 个 source 全部 | 实际全部交付 | 跨度大但相互独立可并行 |
| 4 张 PyPI 包发布到 PyPI | 暂缓——单包先 dogfood 一年 | V2 才考虑 |

---

## 二、Phase 2-10 迭代计划（基于 Phase 1 代码骨架）

### 总览

| Phase | 主题 | 预估时间 | 主要新增 | 里程碑 |
|-------|------|---------|---------|--------|
| **2** | Postgres 持久化 + 崩溃恢复 | 1.5 周 | `host/persistence/postgres/` + Alembic migrations | — |
| **3** | 真实 LLM + 内置工具 | 1.5 周 | `host/llm_adapters/` + `providers/capability_builtin/` | **M2** |
| **4** | Spawn / Suspend / Resume | 2 周 | `core/orchestrator/` + control capability | — |
| **5** | Compact 机制 | 1 周 | `core/loop/steps/compact.py` + `LoopGuard` 完整 | **M3** |
| **6** | Control Plane + Replay | 1.5 周 | `core/control/` 全套 | — |
| **7** | HITL | 1 周 | `core/auth/authorizer.py` + HITL Manager | **M4** |
| **8** | Host REST + SSE | 2 周 | `host/api/` + `host/template_registry/` | — |
| **9** | 外部 Provider（MCP / Skill） | 1.5 周 | `providers/capability_{mcp,skill_*}/` | **M5** |
| **10** | Streaming + Observability + Polish | 2 周 | OTel + Prometheus + 流式 LLM | **M6** |

**总剩余工期：~14 周（1 人）/ ~8-10 周（2 人）**

---

## 三、Phase 2：Postgres 持久化 + 崩溃恢复（1.5 周）

### 目标

把 Phase 1 的 in-memory state / event log 切换到 Postgres；进程重启后能完整恢复。

### 任务清单

| ID | 任务 | 文件 | 验收 |
|----|------|------|------|
| 2.1 | Alembic 初始 migration（11 张表） | `host/persistence/postgres/migrations/0001_initial.py` | `alembic upgrade head` 在空 DB 成功 |
| 2.2 | SQLAlchemy 2.0 async 模型 | `host/persistence/postgres/models.py` | 单测：CRUD round-trip |
| 2.3 | `PostgresStateStore` 实现 `StateStore` 协议 | `host/persistence/postgres/state_store.py` | 单测：apply_event 单事务原子 |
| 2.4 | `PostgresEventStore` 实现 `EventStore` 协议 | `host/persistence/postgres/event_store.py` | 单测：append + read_range + snapshot |
| 2.5 | `PostgresMemoryProvider`（替代 InMemory） | `providers/memory_blackboard/postgres.py` | 单测：所有 MemoryProvider 方法 |
| 2.6 | 崩溃恢复服务 | `core/orchestrator/recovery.py` | 集成测试：kill+restart 后状态恢复一致 |
| 2.7 | `CtxWeftRuntime` 支持 `persistence: PostgresConfig` | `core/runtime.py` 扩展 | 集成测试：Phase 1 测试在 Postgres 后端通过 |
| 2.8 | docker-compose.yml（Postgres 16） | `docker-compose.yml` | `docker compose up` 启动 + 健康检查 |

### 与设计文档对照

- §8.5-§8.8 DDL：11 张表（sessions / tasks / agents / hitl_approvals / events / event_snapshots / memory_events / memory_subscriptions / capability_invocations / llm_providers / agent_templates）
- §8.9 apply_event 事务模式
- §8.11 崩溃恢复 7 步流程

### 验收

```bash
# 1. Phase 1 测试在 Postgres 上通过
pytest tests/integration/test_minimal_loop.py -v

# 2. 新增崩溃恢复测试
pytest tests/integration/test_crash_recovery.py -v
# 应该看到：
# - test_session_resume_after_restart PASSED
# - test_task_queue_rebuild PASSED
# - test_capability_cache_rebuild PASSED
```

---

## 四、Phase 3：真实 LLM + 内置工具（1.5 周）

### 目标

换掉 MockLLMAdapter 跑真实 LLM；接入 5 个内置工具。

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 3.1 | `OpenAIAdapter` (非流式版本先) | `host/llm_adapters/openai.py` |
| 3.2 | `AnthropicAdapter` (非流式版本先) | `host/llm_adapters/anthropic.py` |
| 3.3 | tool calling 归一化（统一 ToolCall 格式） | `host/llm_adapters/_normalize.py` |
| 3.4 | `BuiltinToolsCapabilityProvider` | `providers/capability_builtin/__init__.py` |
| 3.5 | `bash_exec` 工具（黑名单 + 超时） | `providers/capability_builtin/bash.py` |
| 3.6 | `http_request` 工具（SSRF 防护 + header 脱敏） | `providers/capability_builtin/http.py` |
| 3.7 | `read_file` / `write_file` / `glob` 工具 | `providers/capability_builtin/file.py` |
| 3.8 | `ControlCapabilityProvider`（仅 `submit_task_assessment` + `request_human_input`） | `core/orchestrator/control_capability.py` |
| 3.9 | LLM provider 注册（`llm_providers` 表 CRUD） | `core/llm/registry.py` |
| 3.10 | `ActStep` 完整化：tool_calls → capability invoke 循环 | `core/loop/steps/act.py` 扩展 |

### 验收

```bash
# 端到端测试（需要 OPENAI_API_KEY）
pytest tests/integration/test_real_llm.py -v
# 期望：
# - test_openai_bash_workflow PASSED：让 GPT 用 bash 列文件并 summarize
# - test_anthropic_file_workflow PASSED：让 Claude 读文件并改写
```

**M2 达成**：单 agent + 真实 LLM + 真实工具端到端跑通。

---

## 五、Phase 4：Spawn / Suspend / Resume（2 周）

### 目标

实现动态多层 Agent 树。父 agent 调 control capability 派生子 task，子全完成后父 resume。

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 4.1 | `TaskQueue`（LIFO + blocked DAG） | `core/orchestrator/task_queue.py` |
| 4.2 | `TaskManager`：订阅 TASK_CREATED/FINISHED/FAILED + 调度 | `core/orchestrator/task_manager.py` |
| 4.3 | `LifecycleManager`：instantiate_agent / _check_spawn / _settle | `core/orchestrator/lifecycle_manager.py` |
| 4.4 | `CapabilityCache`：per-session 软状态 + 重建 | `core/orchestrator/capability_cache.py` |
| 4.5 | `SuspendStep`：写挂起摘要 | `core/loop/steps/suspend.py` |
| 4.6 | 扩展 `ControlCapabilityProvider`：submit_plan / submit_task / replan | `core/orchestrator/control_capability.py` 扩展 |
| 4.7 | `_try_resume_parent`：检测子全终态 → parent resume | `core/orchestrator/task_manager.py` |
| 4.8 | `SessionManager`：create_session / 启动 root agent | `core/orchestrator/session_manager.py` |
| 4.9 | `CtxWeftRuntime.start_session` 升级为完整三层（SM/TM/LM） | `core/runtime.py` 重写 |

### 验收

```python
# 集成测试
async def test_spawn_workflow():
    # Plan agent 派 3 个子 task
    # 子 task 都完成后 parent resume，拿到 blackboard 拉的结果
    handle = await runtime.start_session(template_id="tpl_planner", user_prompt="research X")
    state = await handle.inspect()
    assert state.tasks[root].status == "FINISHED"
    assert len([t for t in state.tasks if t.parent_task_id == root]) == 3
```

---

## 六、Phase 5：Compact 机制（1 周）

### 目标

长对话不爆 context。完整实现 §6.8 的两层机制。

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 5.1 | `LoopGuard` 完整字段持久化（已有 dataclass，补 Postgres 列 + reducer） | `state/models.py` + 0002 migration |
| 5.2 | ActStep 末尾测量 + 写回 `loop_guard.context_tokens` | `loop/steps/act.py` 扩展 |
| 5.3 | ReasonStep 入口：增量估算 + `_should_compact` | `loop/steps/reason.py` 扩展 |
| 5.4 | `CompactStep` | `loop/steps/compact.py` 新增 |
| 5.5 | `MemoryProvider.apply_compact`（Postgres 实现） | `providers/memory_blackboard/postgres.py` 扩展 |
| 5.6 | `MemoryCompactionAgent` 角色 SOUL（内置 template） | `host/template_registry/builtin/memory_compactor.md` |
| 5.7 | StepDriver 注册 CompactStep 到 step 集合 | `core/runtime.py` 扩展 |

### 验收

```python
# 30+ task 的长对话稳定运行，至少触发 1 次 compact
async def test_long_conversation_with_compact():
    handle = await runtime.start_session(...)
    for i in range(30):
        await handle.send_user_message(f"task {i}: ...")
    events = await collect_events(handle)
    assert any(e.type == "MemoryCompacted" for e in events)
    assert handle.session.status == "RUNNING"  # 没崩
```

**M3 达成**：core 主干完整，能跑长对话。

---

## 七、Phase 6：Control Plane + Replay（1.5 周）

### 目标

`pause / resume / cancel / inspect / step / replay` 全部 API 工作。

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 6.1 | `CancelToken` / `PauseToken` / `Deadline` | `core/control/tokens.py` |
| 6.2 | 各 step 加 checkpoint() 调用 | `core/loop/driver.py` + steps/ |
| 6.3 | `RunHandle` 完整 API（取代 Phase 1 占位版） | `core/control/run_handle.py` |
| 6.4 | Snapshot 自动创建（step 边界 + 每 50 events + HITL 前） | `core/control/snapshotter.py` |
| 6.5 | `ReplayEngine`：Pure Replay 算法（§7.5） | `core/control/replay.py` |
| 6.6 | Event reducer 注册表（25+ state-mutating events） | `core/control/reducers.py` |
| 6.7 | `RunStateView` dataclass | `core/control/types.py` |
| 6.8 | `StepDriver.single_step_mode` | `core/loop/driver.py` 扩展 |

### 验收

```python
async def test_replay_to_any_event():
    handle = await runtime.start_session(...)
    await run_some_work(handle)
    # 取任意中间 event
    events = await collect_events(handle)
    mid_event = events[len(events)//2]
    # replay 到该点
    view = await handle.replay(until_event_id=mid_event.id)
    # 验证状态与当时一致
    assert view.target_event_id == mid_event.id
    assert view.events_replayed == len(events)//2 - snapshot_base
```

---

## 八、Phase 7：HITL（1 周）

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 7.1 | `Authorizer` 协议 + `AllowListAuthorizer` | `core/auth/authorizer.py` |
| 7.2 | `HitlManager`：request / wait / approve / reject / modify | `core/orchestrator/hitl_manager.py` |
| 7.3 | `hitl_approvals` 表 + Alembic migration 0003 | `host/persistence/postgres/migrations/0003_hitl.py` |
| 7.4 | ActStep 内 HITL 拦截（pause_token 暂停） | `core/loop/steps/act.py` 扩展 |
| 7.5 | 超时扫描后台 worker | `host/scheduled/hitl_timeout.py` |
| 7.6 | HITL 事件（HitlRequired/Approved/Rejected/Modified/Timeout） | reducer 注册 |

### 验收

```python
async def test_hitl_approval_flow():
    handle = await runtime.start_session(...)  # agent 会触发 HITL
    pending = await wait_for_event(handle, "HitlRequired")
    await runtime.hitl.approve(pending.payload["approval_id"], modify=None)
    final = await handle.wait_for_finish()
    assert final.outcome == "success"

async def test_hitl_timeout():
    handle = await runtime.start_session(..., hitl_timeout_sec=2)
    pending = await wait_for_event(handle, "HitlRequired")
    await asyncio.sleep(3)
    timeout_ev = await wait_for_event(handle, "HitlTimeout")
    assert timeout_ev.payload["approval_id"] == pending.payload["approval_id"]
```

**M4 达成**：runtime 全部功能完整。

---

## 九、Phase 8：Host REST + SSE + TemplateRegistry（2 周）

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 8.1 | FastAPI app 骨架 + 鉴权 middleware（JWT/API Key） | `host/api/main.py` + `host/api/auth.py` |
| 8.2 | Idempotency-Key middleware | `host/api/idempotency.py` |
| 8.3 | Session 端点（POST/GET/cancel/retry/stream/guard-config） | `host/api/sessions.py` |
| 8.4 | Task 端点（POST/GET/list） | `host/api/tasks.py` |
| 8.5 | Run 端点（events/stream/pause/resume/cancel/replay/step） | `host/api/runs.py` |
| 8.6 | HITL 端点 | `host/api/hitl.py` |
| 8.7 | Provider 注册端点（knowledge/memory/capability） | `host/api/providers.py` |
| 8.8 | Template 端点（import/list/get/refresh-prompt） | `host/api/templates.py` |
| 8.9 | SSE 事件流（消费 EventBus） | `host/api/sse.py` |
| 8.10 | `TemplateRegistry`：markdown SOUL/ROLE → AgentTemplate | `host/template_registry/resolver.py` + `markdown_parser.py` |
| 8.11 | CLI 入口（`ipmastercowork serve`） | `host/cli.py` |

### 验收

```bash
# 启动 host
ipmastercowork serve --port 8000

# 创建 session via REST
curl -X POST http://localhost:8000/api/v1/sessions \
  -H "Authorization: Bearer ..." \
  -d '{"template_id": "tpl_echo", "user_prompt": "hi"}'

# 订阅 SSE 看事件流
curl -N http://localhost:8000/api/v1/runs/{run_id}/stream
```

---

## 十、Phase 9：外部 Provider（1.5 周）

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 9.1 | `MCPCapabilityProvider`（stdio transport） | `providers/capability_mcp/stdio.py` |
| 9.2 | `MCPCapabilityProvider`（streamable_http） | `providers/capability_mcp/http.py` |
| 9.3 | MCP 流式事件映射（progress → CapabilityEvent） | `providers/capability_mcp/_events.py` |
| 9.4 | MCP 连接生命周期（懒连接 + 重连） | `providers/capability_mcp/_connection.py` |
| 9.5 | `LocalSkillCapabilityProvider`（SKILL.md + 懒加载） | `providers/capability_skill_local/__init__.py` |
| 9.6 | `RemoteSkillCapabilityProvider` + `GitSkillSyncer` | `providers/capability_skill_remote/git.py` |
| 9.7 | `HttpSkillSyncer` | `providers/capability_skill_remote/http.py` |
| 9.8 | scripts/ 路径白名单（与 bash_exec 联动） | `providers/capability_builtin/bash.py` 扩展 |

### 验收

```python
async def test_mcp_filesystem_workflow():
    # 接入 @modelcontextprotocol/server-filesystem
    runtime.providers.register_capability(MCPCapabilityProvider(...))
    handle = await runtime.start_session(template_id="tpl_with_fs_mcp", ...)
    # 期望 agent 能用 MCP filesystem 工具完成文件操作

async def test_local_skill_invocation():
    runtime.providers.register_capability(LocalSkillCapabilityProvider("./skills"))
    handle = await runtime.start_session(template_id="tpl_with_skill", user_prompt="analyze finance")
    # 期望 agent 调用 financial_analysis skill → 拿到 SKILL.md 指令 → 按指令执行
```

**M5 达成**：可给早期用户试用。

---

## 十一、Phase 10：Streaming + Observability + Polish（2 周）

### 任务清单

| ID | 任务 | 文件 |
|----|------|------|
| 10.1 | OpenAI LLM 流式 chunk 实现 | `host/llm_adapters/openai.py` 扩展 |
| 10.2 | Anthropic LLM 流式 chunk 实现 | `host/llm_adapters/anthropic.py` 扩展 |
| 10.3 | Capability streaming progress 完整实现 | `providers/capability_builtin/bash.py` + http.py |
| 10.4 | EventBus 背压（EventsDropped 元事件） | `core/events/bus.py` 已有，补 metric |
| 10.5 | Prometheus exporter + 关键 SLI | `host/observability/metrics.py` |
| 10.6 | OpenTelemetry tracing（API→SM→TM→LM→AgentLoop→Cap） | `host/observability/tracing.py` |
| 10.7 | 结构化日志（structlog） | `host/observability/logging.py` |
| 10.8 | events 表分区策略（按月） | Alembic 0004 |
| 10.9 | 100 并发 stress test | `tests/load/test_stress.py` |
| 10.10 | 用户文档（usage guide + provider 开发指南） | `docs/user-guide.md` + `docs/provider-dev-guide.md` |
| 10.11 | OpenAPI / REST API reference 自动生成 | `host/api/openapi.py` |
| 10.12 | PyPI 发布配置 | GitHub Actions release workflow |

### 验收

```
- LLM streaming UI：token-by-token 实时显示
- Capability progress UI：tool 执行进度条
- Prometheus 仪表盘可看到关键 SLI（QPS / p95 / error rate）
- OpenTelemetry：能在 Jaeger 看完整 trace
- Stress test：100 concurrent sessions × 10 tasks 全过
- 文档完整 + PyPI 包发布
```

**M6 达成**：V1 发布。

---

## 十二、风险与缓解（v2 更新）

### 已发现的风险（Phase 1 经验）

| 风险 | v1 评估 | v2 实际 | 缓解 |
|------|---------|--------|------|
| 单包 vs 多包 | 推荐 4 包 | 单包更简单且足够 | 已采纳 |
| Python 3.10/3.11 兼容 | 没考虑 | 用户 sandbox 可能是 3.10 | 代码用 3.11 idiom 但保持 3.10 fallback 路径 |
| mount 文件系统不可删 | 没考虑 | sandbox 环境无法删 mount 内文件 | 用户在 Windows 端手动 cleanup |
| 文件 Write 偶尔截断 | 没考虑 | 多次发生（utils.py / assembler.py / runtime.py） | bash heredoc 重写为后备方案 |

### Phase 2+ 新风险

| 风险 | 概率 | 影响 | 缓解 |
|------|-----|------|------|
| Alembic migration 不可逆设计错误 | 中 | Phase 2 返工 | Phase 2 开头先严格 review schema，与 §8.5-§8.8 逐字对照 |
| MCP SDK Python 版本不稳定 | 中 | Phase 9 延期 | Phase 3 后立刻做 MCP 连接 spike |
| Replay 事件数据不完整 | 高 | Phase 6 引擎 bug | Phase 3 实现 LLM adapter 时就强制完整记录 finish_reason 等字段 |
| 真实 LLM 集成时 prompt 与 mock 不一致 | 中 | Phase 3 调试痛苦 | Phase 3 沿用 Phase 1 的 `test_prompt_structure_matches_miniagents` 风格断言 |

---

## 十三、Phase 2 开工 checklist（next session）

下次接手者立刻可做的具体动作：

1. **Windows cleanup**（5 分钟）
   ```powershell
   cd ctx-weft
   .\setup-py311.ps1  # 重建 .venv (3.11) + 跑测试，确认 4/4 通过
   ```

2. **Phase 2.1：写 Alembic 初始 migration**
   - 文件：`host/persistence/postgres/migrations/0001_initial.py`
   - 内容参考：设计文档 §8.5-§8.8 所有 DDL
   - 验收：`alembic upgrade head` 在空 Postgres 16 + Docker 上跑通

3. **Phase 2.2：写 SQLAlchemy 模型**
   - 文件：`host/persistence/postgres/models.py`
   - 风格：与 `state/models.py` 的 dataclass 对应；用 `Mapped[...]` async 模式

4. **Phase 2.3：写 PostgresStateStore（最难的一块）**
   - 关键点：apply_event 单事务（§8.9）
   - 测试：`tests/integration/test_postgres_state_store.py`

5. **Phase 2.7-2.8：把 Postgres 接到 CtxWeftRuntime + docker-compose**
   - 然后跑 Phase 1 的 `test_minimal_loop.py` 切到 Postgres 后端

---

## 十四、附录：Phase 0+1 完整文件清单

```
ctx-weft/
├── .github/workflows/ci.yml
├── .gitignore
├── .pre-commit-config.yaml
├── .python-version
├── README.md
├── pyproject.toml
├── setup-py311.ps1
├── src/CtxWeft/
│   ├── protocols/
│   │   ├── __init__.py
│   │   ├── capability.py     (~80 LOC)
│   │   ├── context.py        (~60 LOC)
│   │   ├── knowledge.py      (~70 LOC)
│   │   ├── memory.py         (~180 LOC)
│   │   └── template.py       (~110 LOC)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── errors.py         (~50 LOC)
│   │   ├── runtime.py        (~270 LOC)
│   │   ├── utils.py          (~45 LOC)
│   │   ├── assembler/
│   │   │   ├── __init__.py
│   │   │   ├── assembler.py  (~175 LOC)
│   │   │   ├── budget.py     (~55 LOC)
│   │   │   ├── composer.py   (~260 LOC)
│   │   │   └── sources/
│   │   │       ├── identity.py
│   │   │       ├── capability.py
│   │   │       ├── short_memory.py
│   │   │       ├── blackboard.py
│   │   │       ├── long_memory.py
│   │   │       ├── knowledge.py
│   │   │       └── task_spec.py
│   │   ├── events/
│   │   │   ├── __init__.py
│   │   │   ├── bus.py        (~130 LOC)
│   │   │   └── types.py      (~140 LOC, 65 EVENT_TYPES)
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── client.py     (~50 LOC)
│   │   │   ├── mock.py       (~115 LOC)
│   │   │   └── types.py      (~95 LOC)
│   │   ├── loop/
│   │   │   ├── __init__.py
│   │   │   ├── driver.py     (~180 LOC)
│   │   │   └── steps/
│   │   │       ├── act.py    (~120 LOC)
│   │   │       ├── finalize.py (~110 LOC)
│   │   │       ├── observe.py  (~50 LOC)
│   │   │       └── reason.py   (~50 LOC)
│   │   ├── state/
│   │   │   ├── __init__.py
│   │   │   ├── models.py     (~180 LOC)
│   │   │   └── store.py      (~110 LOC)
│   │   ├── orchestrator/__init__.py    (空，Phase 4)
│   │   ├── control/__init__.py         (空，Phase 6)
│   │   └── auth/__init__.py            (空，Phase 4)
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── memory_blackboard/
│   │   │   ├── __init__.py
│   │   │   └── in_memory.py  (~230 LOC)
│   │   └── (其他 provider 子目录为空骨架)
│   └── host/                 (全部为空骨架，Phase 8)
└── tests/
    ├── unit/test_protocols_imports.py     (~60 LOC, 2 tests)
    └── integration/test_minimal_loop.py   (~160 LOC, 2 tests)
```

**总计 57 个 Python 文件 / ~4000 LOC，全部通过 syntax check + 集成测试。**

---

*ctx-weft 开发计划 v2 · 基于 Phase 0+1 已落地代码 · 2026-05-16*
