# LLM Token 用量缓存拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `LLMUsage` 扩展为七字段（总输入/输出/合计/缓存命中/缓存写入/实际输入/推理输出），adapter 内归一化口径，事件 payload 自动透传；host（IpMasterCoworkPy）「累计输入」与云端上报切换为实际输入口径。

**Architecture:** 数据只有一个咽喉：adapter 产 `LLMUsage` → `dataclasses.asdict` 进 `LLMResponseFinished` 等事件 payload → host `SessionEntry.translate_event` 累加并发 `token_update` SSE 帧。协议层加字段（哨兵缺省 + `__post_init__` 自动派生保证任何构造写法账目自洽），Anthropic/OpenAI adapter 各自把方言翻译成统一口径，core 记账（context 阈值/预算）语义不动；host 侧只改累加取值与帧键。

**Tech Stack:** Python 3.11+ dataclasses，pytest（两仓均 `asyncio_mode=auto`，运行统一 `uv run pytest`），TypeScript/React（frontend-desktop，vite + vitest）。

**Spec:** `docs/superpowers/specs/2026-07-16-llm-usage-cache-split-design.md`

## Global Constraints

- core 仓：`C:\Users\Xing\Documents\codes\Loome-02\ctx-weft`；host 仓：`C:\Users\Xing\Documents\codes\IpMasterCoworkPy`（core 以 vendored wheel 交付给 host）。
- **顺序约束**：Task 1–6（core）必须先完成并提交，Task 7 用 `scripts\revendor-core.ps1` re-vendor，之后才能做 Task 8–11（host）。
- 不变式（协议层，逐字）：`prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens`；`reasoning_tokens ≤ completion_tokens`；`total_tokens = prompt_tokens + completion_tokens`。
- `input_tokens` 哨兵缺省 `-1`：未显式给出时 `__post_init__` 派生 `max(0, prompt − read − write)`；显式传入原样保留不钳制。
- 事件 `schema_version` 保持 1，不新增事件类型；payload 只增键。
- host 云端上报（token-usage-spool）只切 `input_tokens` 取值口径，**不加**拆分键（spec §3/§8.2 已决策）。
- host `context_tokens` 继续取单轮 `prompt_tokens`（窗口规模与缓存无关）；core `_account_tokens`/`observe` 记账不改。
- 各仓在 feature 分支上工作：`git checkout -b feat/llm-usage-cache-split`（core、host 各一）。
- 测试命令：core/host 均 `uv run pytest <path> -v`（在各自仓根目录执行）；前端 `npm run build` / `npm test`（在 `frontend-desktop/` 执行）。

---

### Task 1: LLMUsage 七字段扩展（协议层）

**Files:**
- Modify: `src/ctx_weft/protocols/llm.py:150-156`（LLMUsage 类）
- Test: `tests/unit/test_llm_usage.py`（新建）

**Interfaces:**
- Produces: `LLMUsage` 新字段 `cache_read_tokens: int = 0`、`cache_write_tokens: int = 0`、`input_tokens: int = -1`（自动派生）、`reasoning_tokens: int = 0`。后续所有任务按这些字段名构造/断言。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_llm_usage.py`：

```python
"""LLMUsage 七字段构造语义：input_tokens 哨兵自动派生 + 显式保留 + asdict 透传。"""
from __future__ import annotations

import dataclasses

from ctx_weft.protocols import LLMUsage


def test_legacy_construction_derives_input_tokens():
    # 旧写法（不传新字段）必须自洽：无缓存信息时实际输入 == 总输入
    u = LLMUsage(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    assert u.input_tokens == 100
    assert u.cache_read_tokens == 0 and u.cache_write_tokens == 0
    assert u.reasoning_tokens == 0


def test_derivation_with_cache_split():
    u = LLMUsage(prompt_tokens=127, cache_read_tokens=100, cache_write_tokens=20)
    assert u.input_tokens == 7


def test_explicit_input_tokens_preserved():
    u = LLMUsage(prompt_tokens=127, cache_read_tokens=100, cache_write_tokens=20,
                 input_tokens=7)
    assert u.input_tokens == 7


def test_abnormal_ledger_clamps_derivation_to_zero():
    # provider 异常账（cached > prompt）：派生路径钳 0，不产生负数
    u = LLMUsage(prompt_tokens=5, cache_read_tokens=10)
    assert u.input_tokens == 0


def test_asdict_carries_seven_keys():
    d = dataclasses.asdict(LLMUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13))
    assert set(d) == {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "cache_read_tokens", "cache_write_tokens", "input_tokens", "reasoning_tokens",
    }
    assert d["input_tokens"] == 10
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_llm_usage.py -v`
Expected: FAIL（`TypeError: unexpected keyword argument 'cache_read_tokens'` 或 `AttributeError: input_tokens`）

- [ ] **Step 3: 实现**

`src/ctx_weft/protocols/llm.py` 中把现有 LLMUsage：

```python
@dataclass
class LLMUsage:
    """token 使用统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
```

整体替换为：

```python
@dataclass
class LLMUsage:
    """token 使用统计。

    口径（跨 provider 归一，由各 adapter 负责翻译）：
      输入侧：prompt_tokens      — 本次请求的全部输入（含缓存读/写部分）
             cache_read_tokens  — 输入中命中缓存的部分
             cache_write_tokens — 输入中本次写入缓存的部分
                                  （Anthropic cache_creation；OpenAI 系恒 0）
             input_tokens       — 实际未缓存输入（全价计费部分）。
                                  ⚠ 与 prompt_tokens 的区分：prompt 是「总输入」，
                                  input 是「实际输入」；命名对齐 Anthropic API 的
                                  input_tokens（其原生口径即未缓存部分）。
      输出侧：completion_tokens  — 全部输出
             reasoning_tokens   — 输出中属于推理/thinking 的子集
                                  （OpenAI/DeepSeek 单列；Anthropic 无单列恒 0）
    不变式：
      prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
      reasoning_tokens ≤ completion_tokens
      total_tokens = prompt_tokens + completion_tokens
    input_tokens 未显式给出（哨兵 -1）时在 __post_init__ 按不变式自动派生
    （异常账钳 0），保证任何构造写法下账目自洽；显式传入的值原样保留、不钳制。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    input_tokens: int = -1  # 实际输入；未显式给出时自动派生
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            self.input_tokens = max(
                0,
                self.prompt_tokens - self.cache_read_tokens - self.cache_write_tokens,
            )
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `uv run pytest tests/unit/test_llm_usage.py -v`
Expected: 5 PASS

Run: `uv run pytest tests -q`
Expected: 全绿（新字段带缺省值，现有构造点零改动）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/protocols/llm.py tests/unit/test_llm_usage.py
git commit -m "feat(protocols): LLMUsage 七字段拆分——缓存读/写/实际输入/推理输出，哨兵自动派生"
```

---

### Task 2: Anthropic adapter 口径归一化

**Files:**
- Modify: `src/ctx_weft/providers/llm/anthropic.py:85-91`（attempt 循环内的局部变量初始化）、`:153-155`（message_start 分支）、`:201-212`（message_delta 分支）
- Test: `tests/unit/test_anthropic_stream_finalize.py`（追加 3 个测试）

**Interfaces:**
- Consumes: Task 1 的 `LLMUsage(prompt_tokens, completion_tokens, total_tokens, cache_read_tokens, cache_write_tokens, input_tokens, reasoning_tokens)`。
- Produces: usage chunk 满足归一不变式——`prompt_tokens` = API `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`（Anthropic 的 `input_tokens` 不含缓存部分）；`input_tokens` 显式记 API 原值。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_anthropic_stream_finalize.py` 末尾追加（复用文件内既有的 `_data` / `_adapter` / `_collect` 助手）：

```python
async def test_usage_cache_split_normalized():
    # Anthropic 的 input_tokens 不含缓存部分：prompt 归一为三者之和，input 显式记原值。
    lines = [
        _data({"type": "message_start", "message": {"usage": {
            "input_tokens": 7, "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 127
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 20
    assert u.input_tokens == 7
    assert u.total_tokens == 130
    assert u.reasoning_tokens == 0


async def test_usage_no_cache_fields_regression():
    # 不带缓存字段的现状流：prompt == input，cache 全 0，与旧行为全等。
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 7}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 7 and u.input_tokens == 7
    assert u.cache_read_tokens == 0 and u.cache_write_tokens == 0


async def test_message_delta_overrides_input_side():
    # 部分代理在尾包重发输入侧字段 → 以尾包为准。
    lines = [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
        _data({"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "hi"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
               "usage": {"output_tokens": 3, "input_tokens": 7,
                         "cache_read_input_tokens": 100}}),
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 107
    assert u.input_tokens == 7
    assert u.cache_read_tokens == 100
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_anthropic_stream_finalize.py -v -k "usage or overrides"`
Expected: `test_usage_cache_split_normalized`、`test_message_delta_overrides_input_side` FAIL（cache 字段为 0/prompt 未归一）；`test_usage_no_cache_fields_regression` PASS（现状即如此）

- [ ] **Step 3: 实现**

`src/ctx_weft/providers/llm/anthropic.py`，attempt 循环内局部变量（现 :87 `input_tokens: int | None = None` 处）改为：

```python
            input_tokens: int | None = None
            cache_read = 0
            cache_write = 0
```

`message_start` 分支（现 :153-155）改为：

```python
                        if event_type == "message_start":
                            usage_data = (event.get("message") or {}).get("usage") or {}
                            input_tokens = usage_data.get("input_tokens")
                            cache_read = usage_data.get("cache_read_input_tokens") or 0
                            cache_write = usage_data.get("cache_creation_input_tokens") or 0
```

`message_delta` 分支（现 :201-212）改为：

```python
                        elif event_type == "message_delta":
                            stop_reason = event.get("delta", {}).get("stop_reason")
                            usage_data = event.get("usage") or {}
                            output_tokens = usage_data.get("output_tokens", 0)
                            # 部分代理在尾包重发输入侧字段——带了就覆盖（以尾包为准）
                            if usage_data.get("input_tokens") is not None:
                                input_tokens = usage_data.get("input_tokens")
                            if usage_data.get("cache_read_input_tokens") is not None:
                                cache_read = usage_data.get("cache_read_input_tokens") or 0
                            if usage_data.get("cache_creation_input_tokens") is not None:
                                cache_write = usage_data.get("cache_creation_input_tokens") or 0
                            # Anthropic 的 input_tokens 不含缓存部分 → 归一为「全部输入」口径，
                            # 保证 core 的 context 阈值/预算拿到真实上下文规模（开缓存后不失真）
                            uncached = input_tokens or 0
                            prompt_total = uncached + cache_read + cache_write
                            usage = LLMUsage(
                                prompt_tokens=prompt_total,
                                completion_tokens=output_tokens,
                                total_tokens=prompt_total + output_tokens,
                                cache_read_tokens=cache_read,
                                cache_write_tokens=cache_write,
                                input_tokens=uncached,
                                # reasoning_tokens 恒 0：thinking 计入 output_tokens 无单列，
                                # 不用流式 thinking 文本估算——估算值混进计费口径就是错账
                            )
                            finish_reason = stop_reason or "stop"
                            break  # 终止事件 → 跳出循环做统一收尾
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_anthropic_stream_finalize.py -v`
Expected: 全 PASS（含既有用例）

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/llm/anthropic.py tests/unit/test_anthropic_stream_finalize.py
git commit -m "feat(anthropic): usage 缓存拆分+口径归一——prompt=input+cache_read+cache_write"
```

---

### Task 3: OpenAI adapter 拆分读取

**Files:**
- Modify: `src/ctx_weft/providers/llm/openai.py:147-153`（usage 解析）
- Test: `tests/unit/test_openai_stream_finalize.py`（追加 3 个测试）

**Interfaces:**
- Consumes: Task 1 的 `LLMUsage`。
- Produces: usage chunk——`cache_read_tokens` 取 `prompt_tokens_details.cached_tokens`（回退 DeepSeek `prompt_cache_hit_tokens`）；`reasoning_tokens` 取 `completion_tokens_details.reasoning_tokens`；`input_tokens` 不传（自动派生 prompt − cached）。

- [ ] **Step 1: 写失败测试**

在 `tests/unit/test_openai_stream_finalize.py` 末尾追加（复用 `_data` / `_delta` / `_adapter` / `_collect`）：

```python
async def test_usage_cached_tokens_split():
    lines = [
        _delta({"content": "hello"}, finish_reason="stop"),
        _data({"choices": [], "usage": {
            "prompt_tokens": 110, "completion_tokens": 9, "total_tokens": 119,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 4}}}),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.prompt_tokens == 110
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 0
    assert u.input_tokens == 10   # 派生：prompt − cached
    assert u.reasoning_tokens == 4


async def test_usage_deepseek_dialect_fallback():
    lines = [
        _delta({"content": "hello"}, finish_reason="stop"),
        _data({"choices": [], "usage": {
            "prompt_tokens": 110, "completion_tokens": 9, "total_tokens": 119,
            "prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 10}}),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.cache_read_tokens == 100
    assert u.input_tokens == 10


async def test_usage_no_details_regression():
    lines = [
        _delta({"content": "hello"}, finish_reason="stop"),
        _data({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3,
                                        "total_tokens": 13}}),
        "data: [DONE]",
    ]
    chunks = await _collect(_adapter(lines))
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.cache_read_tokens == 0 and u.cache_write_tokens == 0
    assert u.input_tokens == 10 and u.reasoning_tokens == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_openai_stream_finalize.py -v -k "cached or deepseek or no_details"`
Expected: 前两个 FAIL（cache_read 为 0）；`test_usage_no_details_regression` PASS

- [ ] **Step 3: 实现**

`src/ctx_weft/providers/llm/openai.py` 现 :147-153：

```python
                        usage_data = event.get("usage") or {}
                        if usage_data:
                            usage = LLMUsage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
                            )
```

改为：

```python
                        usage_data = event.get("usage") or {}
                        if usage_data:
                            prompt_details = usage_data.get("prompt_tokens_details") or {}
                            completion_details = usage_data.get("completion_tokens_details") or {}
                            # 缓存命中：标准 prompt_tokens_details.cached_tokens →
                            # DeepSeek 方言 prompt_cache_hit_tokens → 0（方言封死在 adapter 内）
                            cache_read = (prompt_details.get("cached_tokens")
                                          or usage_data.get("prompt_cache_hit_tokens") or 0)
                            usage = LLMUsage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
                                cache_read_tokens=cache_read,
                                cache_write_tokens=0,  # OpenAI 系不区分/不计费缓存写入
                                # input_tokens 不传 → __post_init__ 派生 prompt − cached
                                # （OpenAI 只报 cached 子集，无未缓存原始值可记）
                                reasoning_tokens=completion_details.get("reasoning_tokens", 0) or 0,
                            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_openai_stream_finalize.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/llm/openai.py tests/unit/test_openai_stream_finalize.py
git commit -m "feat(openai): usage 读取 cached_tokens/reasoning_tokens 拆分（含 DeepSeek 方言回退）"
```

---

### Task 4: Mock adapter 模拟字段 + 事件 payload 透传验证

**Files:**
- Modify: `src/ctx_weft/providers/llm/mock.py:15-21`（MockResponse）、`:91-98`（usage chunk 构造）
- Test: `tests/unit/test_llm_usage.py`（追加 mock 测试）、`tests/unit/test_observe_react_helper.py`（追加 payload 测试）

**Interfaces:**
- Consumes: Task 1 的 `LLMUsage`；`tests/unit/test_observe_react_helper.py` 既有助手 `_make_state` / `_make_ctx` / `_make_tool_call_chunk`、模块别名 `_obs_mod`。
- Produces: `MockResponse` 新可选字段 `cache_read_tokens: int = 0`、`cache_write_tokens: int = 0`、`reasoning_tokens: int = 0`（host 联调/事件层测试用）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_llm_usage.py` 末尾追加：

```python
async def test_mock_adapter_carries_cache_split():
    from ctx_weft.protocols import LLMMessage, LLMRequest
    from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse

    adapter = MockLLMAdapter([MockResponse(text="hi", cache_read_tokens=3, reasoning_tokens=1)])
    req = LLMRequest(model="mock", system="sys prompt",
                     messages=[LLMMessage(role="user", content="question")])
    chunks = [c async for c in adapter.complete(req)]
    u = [c for c in chunks if c.kind == "usage"][0].usage
    assert u.cache_read_tokens == 3
    assert u.reasoning_tokens == 1
    assert u.input_tokens == u.prompt_tokens - 3  # 自动派生
```

`tests/unit/test_observe_react_helper.py` 末尾追加（`LLMUsage`、`EventType`、`SimpleNamespace`、`_obs_mod` 该文件已 import）：

```python
class _RecordingBusFull:
    """Records full events (not just types) for payload assertions."""

    def __init__(self):
        self.events = []

    async def emit(self, event) -> None:
        self.events.append(event)


async def test_response_finished_payload_carries_usage_split(monkeypatch):
    """LLM_RESPONSE_FINISHED 的 payload["usage"] 经 asdict 自动携带七字段拆分。"""

    async def _fake_stream(ctx, state, request):
        yield SimpleNamespace(
            kind="usage",
            usage=LLMUsage(prompt_tokens=127, completion_tokens=5, total_tokens=132,
                           cache_read_tokens=100, cache_write_tokens=20),
            tool_call=None, text="",
        )
        yield _make_tool_call_chunk("report_task_outcome")

    monkeypatch.setattr(_obs_mod, "stream_llm_resilient", _fake_stream)

    bus = _RecordingBusFull()
    state = _make_state()
    ctx = _make_ctx(tool_content="DONE", event_bus=bus)

    await run_observe_react(
        state, ctx, system="SYS", messages=[], tools=[],
        request_id_prefix="test", max_rounds=1,
        terminal_tool_name="report_task_outcome",
    )

    finished = [e for e in bus.events if e.type == EventType.LLM_RESPONSE_FINISHED]
    assert finished
    u = finished[0].payload["usage"]
    assert u["prompt_tokens"] == 127
    assert u["cache_read_tokens"] == 100
    assert u["cache_write_tokens"] == 20
    assert u["input_tokens"] == 7
    assert u["reasoning_tokens"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_llm_usage.py::test_mock_adapter_carries_cache_split tests/unit/test_observe_react_helper.py::test_response_finished_payload_carries_usage_split -v`
Expected: mock 测试 FAIL（`TypeError: unexpected keyword argument 'cache_read_tokens'`）；payload 测试 PASS（asdict 透传 Task 1 已生效——若 PASS 属预期，保留作回归锚）

- [ ] **Step 3: 实现**

`src/ctx_weft/providers/llm/mock.py` 的 `MockResponse` 改为：

```python
@dataclass
class MockResponse:
    """一次 LLM 调用的预期返回。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    chunk_size: int = 16  # streaming 时每个 chunk 的字符数
    # usage 拆分模拟（事件层/联调测试用；cache 之和应 ≤ 估算的 prompt_tokens，测试自行保证）
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
```

`_stream` 内 usage chunk 构造（现 :91-98）改为：

```python
        yield LLMChunk(
            kind="usage",
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_write_tokens=response.cache_write_tokens,
                reasoning_tokens=response.reasoning_tokens,
                # input_tokens 自动派生 = prompt − read − write
            ),
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_llm_usage.py tests/unit/test_observe_react_helper.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/llm/mock.py tests/unit/test_llm_usage.py tests/unit/test_observe_react_helper.py
git commit -m "feat(mock): MockResponse 缓存/推理模拟字段+事件 payload 七字段透传回归锚"
```

---

### Task 5: RecognizeIntentStep usage 透出

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py:9-19`（import）、`:129-138`（stream 循环）、`:159-163`（COMPLETED payload）
- Test: `tests/unit/test_recognize_intent_fill.py`

**Interfaces:**
- Consumes: Task 1 的 `LLMUsage`。
- Produces: `RECOGNIZE_INTENT_COMPLETED` payload 新增 `"usage": dict`（七字段）。只透出不记账（不加 `session.token_used`——预算语义不动，spec §5.7）。

- [ ] **Step 1: 写失败测试**

`tests/unit/test_recognize_intent_fill.py`：`_FakeLLM.complete` 增发 usage chunk（顶部 import 行改为 `from ctx_weft.protocols import LLMChunk, LLMUsage, ToolCall`）：

```python
class _FakeLLM:
    def __init__(self, args):
        self._args = args

    async def complete(self, request, stream=True):
        yield LLMChunk(
            kind="tool_call",
            tool_call=ToolCall(id="tc1", name="update_task_metadata", arguments=self._args),
        )
        yield LLMChunk(
            kind="usage",
            usage=LLMUsage(prompt_tokens=50, completion_tokens=8, total_tokens=58),
        )
```

文件末尾追加测试：

```python
async def test_completed_payload_carries_usage():
    """意图识别的 LLM 开销此前无账可查——usage 透进 COMPLETED payload（只透出不记账）。"""
    args = {"title": "T", "description": "D", "session_goal": "G"}
    cap = ToolCapability(id="cap1", name="update_task_metadata", purposes=["recognize_intent"])

    emitted = []

    async def _emit(ev):
        emitted.append(ev)

    ctx = SimpleNamespace(
        provider_ctx=SimpleNamespace(),
        capability_cache=None,
        assembler=_FakeAssembler(),
        llm=_FakeLLM(args),
        capability_gateway=_FakeGateway(),
        event_bus=SimpleNamespace(emit=_emit),
    )
    await RecognizeIntentStep().execute(_state([cap]), ctx)

    completed = [ev for ev in emitted if ev.type == "RecognizeIntentCompleted"]
    assert completed
    u = completed[0].payload["usage"]
    assert u["prompt_tokens"] == 50
    assert u["input_tokens"] == 50   # 无缓存信息 → 派生 = prompt
    assert u["completion_tokens"] == 8
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/unit/test_recognize_intent_fill.py -v`
Expected: `test_completed_payload_carries_usage` FAIL（`KeyError: 'usage'`）；既有 2 个用例 PASS

- [ ] **Step 3: 实现**

`src/ctx_weft/core/loop/steps/recognize_intent.py`：

顶部 import 增加（`import logging` 旁）：

```python
import dataclasses
```

execute 内局部 import（现 :107）改为：

```python
        from ctx_weft.protocols import LLMRequest, LLMUsage
```

stream 循环（现 :129-138）改为：

```python
        tool_name = ""
        tool_args: dict[str, Any] = {}
        usage = LLMUsage()
        try:
            _guard = getattr(state.agent, "loop_guard", None)
            llm_request.prompt_token_estimate = request_prompt_estimate(llm_request, _guard, None)
            apply_dynamic_max_tokens(ctx, llm_request, _guard)
            async for chunk in stream_llm(ctx.llm, llm_request):
                if chunk.kind == "tool_call" and chunk.tool_call:
                    tool_name = chunk.tool_call.name
                    tool_args = chunk.tool_call.arguments
                elif chunk.kind == "usage" and chunk.usage is not None:
                    usage = chunk.usage
```

`RECOGNIZE_INTENT_COMPLETED` payload（现 :159-163）改为：

```python
        await ctx.event_bus.emit(make_event(state, EventType.RECOGNIZE_INTENT_COMPLETED, payload={
            "title": tool_args.get("title", ""),
            "description": tool_args.get("description", ""),
            "session_goal": tool_args.get("session_goal", ""),
            # 只透出不记账：意图识别 LLM 开销此前无账可查（spec 2026-07-16 §5.7）
            "usage": dataclasses.asdict(usage),
        }))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/unit/test_recognize_intent_fill.py tests/unit/test_recognize_intent_launch.py tests/unit/test_recognize_intent_target.py tests/unit/test_recognize_intent_projection.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/core/loop/steps/recognize_intent.py tests/unit/test_recognize_intent_fill.py
git commit -m "feat(recognize_intent): usage 透进 COMPLETED payload——意图识别开销有账可查"
```

---

### Task 6: 设计文档 §9 同步 + core 全量回归

**Files:**
- Modify: `docs/ctx-weft_设计文档.md:4038`（payload 注释）、`:4042-4045`（LLMUsageDict）、`:3813`（事件 payload 表）

**Interfaces:**
- Consumes: Task 1 定稿的七字段名。

- [ ] **Step 1: 更新 LLMUsageDict 与注释**

`docs/ctx-weft_设计文档.md` :4038 的一行：

```python
    usage: LLMUsageDict                  # {prompt_tokens, completion_tokens, total_tokens}
```

改为：

```python
    usage: LLMUsageDict                  # 七字段拆分，见下
```

:4042-4045 的 LLMUsageDict：

```python
class LLMUsageDict(TypedDict):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
```

改为：

```python
class LLMUsageDict(TypedDict):
    prompt_tokens: int        # 总输入（含缓存读/写；跨 provider 归一口径）
    completion_tokens: int    # 全部输出
    total_tokens: int         # prompt + completion
    cache_read_tokens: int    # 缓存命中
    cache_write_tokens: int   # 缓存写入（Anthropic cache_creation；OpenAI 系恒 0）
    input_tokens: int         # 实际未缓存输入（= prompt − read − write）
    reasoning_tokens: int     # 输出中的推理子集
```

:3813 表格行中 `` `usage`（prompt_tokens / completion_tokens / total_tokens） `` 改为 `` `usage`（七字段拆分：prompt/completion/total/cache_read/cache_write/input/reasoning） ``。

- [ ] **Step 2: core 全量回归**

Run: `uv run pytest tests -q`
Expected: 全绿

- [ ] **Step 3: Commit**

```bash
git add docs/ctx-weft_设计文档.md
git commit -m "docs(design): §9 LLMUsageDict 七字段同步"
```

---

### Task 7: re-vendor core wheel 进 host

**Files:**
- Modify（host 仓）: `vendor/ctx_weft-0.1.0-py3-none-any.whl`、`uv.lock`（脚本自动产出）

**Interfaces:**
- Consumes: Task 1–6 已提交的 core 代码。
- Produces: host 环境内 `ctx_weft.protocols.LLMUsage` 为七字段版，Task 8+ 的 host 测试依赖它。

- [ ] **Step 1: 运行 re-vendor 一条龙**

在 host 仓根目录（`C:\Users\Xing\Documents\codes\IpMasterCoworkPy`）执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\revendor-core.ps1
```

Expected: 各步骤 `[OK]`（build → 拷贝 → uv lock → uv sync → import 验证），无 `[ERROR]`
（脚本默认 CoreRepo 即 `..\Loome-02\ctx-weft`，无需参数）

- [ ] **Step 2: 验证新字段就位**

```powershell
uv run python -c "from ctx_weft.protocols import LLMUsage; u = LLMUsage(prompt_tokens=10); print(u.input_tokens, u.cache_read_tokens)"
```

Expected: `10 0`

- [ ] **Step 3: Commit（host 仓）**

```bash
git checkout -b feat/llm-usage-cache-split
git add vendor/ uv.lock pyproject.toml
git commit -m "chore(vendor): re-vendor ctx-weft——LLMUsage 七字段 usage 拆分"
```

---

### Task 8: SessionEntry 累计口径切换 + token_update 拆分键

**Files:**
- Modify（host 仓）: `src/ipmastercowork/api/models/session.py:87-91`（`__init__` 累加器）、`:129-147`（`to_dict`）、`:151-164`（`_session_update_json`）、`:330-348`（`LLM_RESPONSE_FINISHED` 分支）
- Test（host 仓）: `tests/test_token_usage_split.py`（新建）

**Interfaces:**
- Consumes: Task 7 后的七字段 usage payload；host 测试惯例（`async def` + `SimpleNamespace` 事件 + `asyncio_mode=auto`，token_update 帧经 `ensure_future` 进 `entry.sse_events`，断言前 `await asyncio.sleep(0)`）。
- Produces: `SessionEntry.cache_read_used` / `cache_write_used` 累加器；`token_update` / `to_dict` / `session_update` 携带 `cache_read_tokens_used` / `cache_write_tokens_used`；`input_tokens` 累计实际输入。Task 9/10 依赖这些键名。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_token_usage_split.py`：

```python
"""token 统计口径：input 累计实际输入（usage.input_tokens），cache 拆分键进 token_update。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from ctx_weft.core.events import EventType
from ipmastercowork.api.models.session import SessionEntry


@pytest.fixture(autouse=True)
def _mute_cloud_report(monkeypatch):
    """LLM_RESPONSE_FINISHED 路径会同步调云端上报（写 spool 文件）——测试统一静音；
    Task 9 的用例在测试体内再 setattr 覆盖为记录桩（后设者胜）。"""
    monkeypatch.setattr(
        "ipmastercowork.observability.token_usage_subscriber.report_token_usage",
        lambda **kw: None)


def _entry() -> SessionEntry:
    return SessionEntry(
        session_id="s1", template_id="tpl", user_prompt="hi",
        tenant_id="default", llm_model="m", llm_account="acc",
    )


def _finished_ev(usage: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type=EventType.LLM_RESPONSE_FINISHED,
        payload={"request_id": "req1", "content": "hi", "reasoning": "",
                 "tool_calls": [], "usage": usage, "finish_reason": "stop", "turn": 0},
        timestamp=datetime.now(timezone.utc),
        run_id="r1", task_id="tsk_1", agent_id="agt_1",
    )


_SPLIT_USAGE = {
    "prompt_tokens": 127, "completion_tokens": 5, "total_tokens": 132,
    "cache_read_tokens": 100, "cache_write_tokens": 20, "input_tokens": 7,
    "reasoning_tokens": 0,
}


async def test_input_accumulates_actual_input_tokens() -> None:
    e = _entry()
    e.translate_event(_finished_ev(_SPLIT_USAGE))
    assert e.input_tokens == 7          # 实际输入，不再是 prompt 总输入
    assert e.output_tokens == 5
    assert e.cache_read_used == 100
    assert e.cache_write_used == 20
    assert e.context_tokens == 127      # 当前窗口仍取 prompt 总输入


async def test_legacy_usage_falls_back_to_prompt_tokens() -> None:
    # replay 旧事件：usage 无 input_tokens 键 → 回退 prompt_tokens（旧口径全额实际输入）
    e = _entry()
    e.translate_event(_finished_ev(
        {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}))
    assert e.input_tokens == 40
    assert e.cache_read_used == 0 and e.cache_write_used == 0


async def test_token_update_frame_carries_cache_keys() -> None:
    e = _entry()
    e.translate_event(_finished_ev(_SPLIT_USAGE))
    await asyncio.sleep(0)              # token_update 经 ensure_future 进 sse_events
    frames = [json.loads(s) for s in e.sse_events]
    tu = [f for f in frames if f.get("type") == "token_update"]
    assert tu
    assert tu[0]["input_tokens_used"] == 7
    assert tu[0]["cache_read_tokens_used"] == 100
    assert tu[0]["cache_write_tokens_used"] == 20
    assert tu[0]["context_tokens"] == 127


async def test_snapshot_and_session_update_carry_cache_keys() -> None:
    e = _entry()
    e.translate_event(_finished_ev(_SPLIT_USAGE))
    d = e.to_dict()
    assert d["cache_read_tokens_used"] == 100
    assert d["cache_write_tokens_used"] == 20
    su = json.loads(e._session_update_json("RUNNING"))
    assert su["cache_read_tokens_used"] == 100
    assert su["cache_write_tokens_used"] == 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_token_usage_split.py -v`
Expected: FAIL（`AttributeError: cache_read_used`；`input_tokens == 127 != 7`）

- [ ] **Step 3: 实现**

`src/ipmastercowork/api/models/session.py`：

`__init__`（现 :87-91）改为：

```python
        self.token_budget: int = 200_000
        self.input_tokens: int = 0        # 累计实际未缓存输入（usage.input_tokens 口径）
        self.output_tokens: int = 0
        self.context_tokens: int = 0
        self.cache_read_used: int = 0     # 累计缓存命中
        self.cache_write_used: int = 0    # 累计缓存写入
        self.failure_counter: int = 0
```

`to_dict`（现 :138-140 的三行处）在 `"context_tokens": self.context_tokens,` 之后插入：

```python
            "cache_read_tokens_used": self.cache_read_used,
            "cache_write_tokens_used": self.cache_write_used,
```

`_session_update_json`（现 :157-164）在 `"output_tokens_used": self.output_tokens,` 之后插入：

```python
            "cache_read_tokens_used": self.cache_read_used,
            "cache_write_tokens_used": self.cache_write_used,
```

`LLM_RESPONSE_FINISHED` 分支（现 :330-348 的累加与 token_update 部分）改为：

```python
        if t == EventType.LLM_RESPONSE_FINISHED:
            content = p.get("content", "")
            reasoning = p.get("reasoning", "")
            usage = p.get("usage", {})
            # 「累计输入」= 实际未缓存输入（usage.input_tokens；replay 旧事件回退
            # prompt_tokens）。总输入口径每轮把整个上下文重复累加，严重高估真实成本
            # （spec 2026-07-16 llm-usage-cache-split §8.1）。
            self.input_tokens += usage.get("input_tokens", usage.get("prompt_tokens", 0))
            self.output_tokens += usage.get("completion_tokens", 0)
            self.cache_read_used += usage.get("cache_read_tokens", 0)
            self.cache_write_used += usage.get("cache_write_tokens", 0)
            # 当前窗口仍取 prompt 总输入：窗口规模与缓存无关
            self.context_tokens = usage.get("prompt_tokens", 0) or self.context_tokens
```

（其后既有的 turn_seq 注释与 `self._report_token_usage(usage)` 调用保持原位不动），token_update 帧（现 :343-348）改为：

```python
            asyncio.ensure_future(self._append_json(json.dumps({
                "type": "token_update",
                "input_tokens_used": self.input_tokens,
                "output_tokens_used": self.output_tokens,
                "cache_read_tokens_used": self.cache_read_used,
                "cache_write_tokens_used": self.cache_write_used,
                "context_tokens": self.context_tokens,
            })))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_token_usage_split.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit（host 仓）**

```bash
git add src/ipmastercowork/api/models/session.py tests/test_token_usage_split.py
git commit -m "feat(session): 累计输入口径切换为实际输入+token_update 缓存拆分键"
```

---

### Task 9: 云端用量上报口径切换

**Files:**
- Modify（host 仓）: `src/ipmastercowork/api/models/session.py:249-256`（`_report_token_usage` 取值）、`src/ipmastercowork/observability/token_usage_subscriber.py:24-33`（docstring）
- Test（host 仓）: `tests/test_token_usage_split.py`（追加）

**Interfaces:**
- Consumes: Task 8 的 `_finished_ev` / `_entry` / `_SPLIT_USAGE` 测试助手（同文件）。
- Produces: spool（token-usage-spool.jsonl）的 `input_tokens` 从此为实际输入口径；**不加**拆分键（spec §8.2 已决策）。

- [ ] **Step 1: 写失败测试**

`tests/test_token_usage_split.py` 末尾追加：

```python
async def test_cloud_report_uses_actual_input(monkeypatch) -> None:
    # spool 的 input_tokens 切换为实际输入口径；不加拆分键（spec §8.2）
    calls: list[dict] = []

    def _fake(**kw):
        calls.append(kw)

    monkeypatch.setattr(
        "ipmastercowork.observability.token_usage_subscriber.report_token_usage", _fake)
    e = _entry()
    e.translate_event(_finished_ev(_SPLIT_USAGE))
    assert calls
    assert calls[0]["prompt_tokens"] == 7        # 实际输入，不再是 127
    assert calls[0]["completion_tokens"] == 5


async def test_cloud_report_legacy_fallback(monkeypatch) -> None:
    calls: list[dict] = []

    def _fake(**kw):
        calls.append(kw)

    monkeypatch.setattr(
        "ipmastercowork.observability.token_usage_subscriber.report_token_usage", _fake)
    e = _entry()
    e.translate_event(_finished_ev(
        {"prompt_tokens": 40, "completion_tokens": 3, "total_tokens": 43}))
    assert calls and calls[0]["prompt_tokens"] == 40
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_token_usage_split.py -v -k cloud_report`
Expected: `test_cloud_report_uses_actual_input` FAIL（`prompt_tokens == 127`）；legacy 用例 PASS

- [ ] **Step 3: 实现**

`src/ipmastercowork/api/models/session.py` `_report_token_usage` 内（现 :249-256）：

```python
            report_token_usage(
                session_id=self.session_id,
                turn_seq=self.turn_seq,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                llm_account=llm_account,
                llm_model=llm_model,
            )
```

改为：

```python
            report_token_usage(
                session_id=self.session_id,
                turn_seq=self.turn_seq,
                # 上报口径 = 实际未缓存输入（usage.input_tokens；旧事件回退 prompt_tokens）。
                # 上报量小于旧口径属预期的高估修正（spec 2026-07-16 §8.2），云端消费端已知会。
                prompt_tokens=int(usage.get("input_tokens", usage.get("prompt_tokens") or 0) or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                llm_account=llm_account,
                llm_model=llm_model,
            )
```

`src/ipmastercowork/observability/token_usage_subscriber.py` 的 `report_token_usage` docstring（:33）改为：

```python
    """由 SessionEntry.translate_event() 同步调用（不是 EventBus 订阅回调）。绝不抛。

    prompt_tokens 自 2026-07-16 起为「实际未缓存输入」口径（usage.input_tokens），
    不再是含缓存命中的总输入——见 spec llm-usage-cache-split §8.2。
    """
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_token_usage_split.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit（host 仓）**

```bash
git add src/ipmastercowork/api/models/session.py src/ipmastercowork/observability/token_usage_subscriber.py tests/test_token_usage_split.py
git commit -m "feat(observability): 云端用量上报切换实际输入口径（不加拆分键）"
```

---

### Task 10: 前端类型与 token_update 合并（frontend-desktop）

**Files:**
- Modify（host 仓）: `frontend-desktop/src/types/index.ts:22-24`、`frontend-desktop/src/hooks/useSessionSSE.ts:357`

**Interfaces:**
- Consumes: Task 8 的 `token_update` 帧键名 `cache_read_tokens_used` / `cache_write_tokens_used`。
- Produces: `Session` 类型可选字段 `cache_read_tokens_used?: number`、`cache_write_tokens_used?: number`（可选——前端测试夹具/旧快照无此键）。无展示组件改动（spec §3 非目标）。

- [ ] **Step 1: 类型扩展**

`frontend-desktop/src/types/index.ts` 现：

```ts
  input_tokens_used: number
  output_tokens_used: number
  context_tokens: number
```

改为：

```ts
  input_tokens_used: number      // 累计实际未缓存输入（2026-07-16 起不再是 prompt 总输入）
  output_tokens_used: number
  context_tokens: number
  cache_read_tokens_used?: number   // 累计缓存命中
  cache_write_tokens_used?: number  // 累计缓存写入
```

- [ ] **Step 2: token_update 合并新键**

`frontend-desktop/src/hooks/useSessionSSE.ts` :357 的单行 setState 中，在 `context_tokens: (data.context_tokens as number) ?? s.session.context_tokens` 之后（同一对象字面量内）追加：

```ts
, cache_read_tokens_used: (data.cache_read_tokens_used as number) ?? s.session.cache_read_tokens_used, cache_write_tokens_used: (data.cache_write_tokens_used as number) ?? s.session.cache_write_tokens_used
```

- [ ] **Step 3: 构建与测试**

在 `frontend-desktop/` 目录：

Run: `npm run build`
Expected: tsc + vite 构建通过，无类型错误

Run: `npm test`
Expected: vitest 全绿

- [ ] **Step 4: Commit（host 仓）**

```bash
git add frontend-desktop/src/types/index.ts frontend-desktop/src/hooks/useSessionSSE.ts
git commit -m "feat(desktop): Session 类型/SSE 合并接入缓存拆分键"
```

---

### Task 11: host 全量回归收尾

**Files:** 无新改动（验证任务）

- [ ] **Step 1: host 全量回归**

在 host 仓根目录：

Run: `uv run pytest tests -q`
Expected: 全绿（若有既有用例断言旧 token 口径——如假定 input 累计 == prompt 累计——按新口径修正该用例断言，并在 commit message 里注明）

- [ ] **Step 2: core 仓最终确认**

在 core 仓根目录：

Run: `uv run pytest tests -q`
Expected: 全绿

- [ ] **Step 3: 修正（如有）并 Commit**

```bash
git add -A tests
git commit -m "test: token 口径切换的既有用例断言修正"
```

（无修正则跳过本 commit。）

---

## 交付物清单（对照 spec）

| Spec 章节 | 任务 |
|---|---|
| §5.1 LLMUsage 七字段 | Task 1 |
| §5.2 Anthropic 归一化 | Task 2 |
| §5.3 OpenAI 拆分 | Task 3 |
| §5.4 Mock 模拟 | Task 4 |
| §5.5 事件透传（asdict 自动） | Task 4（回归锚） |
| §5.7 recognize_intent 透出 | Task 5 |
| §5.5 文档同步 | Task 6 |
| §8.1 host 累计口径 + token_update 键 | Task 7（revendor）+ Task 8 |
| §8.2 云端上报口径 | Task 9 |
| §8.3 前端类型/合并 | Task 10 |
| §7 测试计划全项 | Task 1-5、8-9 各自 Step + Task 6/11 回归 |

**不在本计划**（spec 标注可选/非目标）：`BACKGROUND_OBSERVE_RESPONSE_FINISHED` 纳入 host 统计（spec §8.1 可选项，host 现无该事件分支，另行立项）；前端展示 UI；Anthropic `cache_control` 启用。
