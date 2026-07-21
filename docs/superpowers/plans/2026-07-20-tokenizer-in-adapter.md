# Tokenizer 与校准下沉 adapter — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 token 估算（tokenizer）与 EMA 校准从 core 全局态下沉为 adapter 所有，经 `LLMClient.tokenizer` 硬契约暴露，core 全链路（循环热路径 + 装配链路）改道消费。

**Architecture:** 协议加同步 `Tokenizer`（`count` 返回已校准值 / `observe` 回喂），删 `count_tokens`；providers 提供 `HeuristicTokenizer`（启发式费率 × 对数空间伺服 EMA）默认组件，adapter 按 model 分桶持有；core 删 `token_calibration.py` 全局单例，gateway/prepare 显式收 tokenizer，装配链路经 `ContextRequest.token_counter` 传递。

**Tech Stack:** Python 3.11、pytest（asyncio_mode=auto）。不引新依赖。

**Spec:** `docs/superpowers/specs/2026-07-20-tokenizer-in-adapter-design.md`

## Global Constraints

- `protocols/` 层零运行时依赖（`Tokenizer` 必须是纯 Protocol，不 import providers/core 实现）。
- core 不 import providers（只经协议类型）。
- `Tokenizer.count` 同步、纯本地、禁网络。
- 伺服参数（HeuristicTokenizer 默认值）：α=0.3、min_sample_tokens=512、clamp [0.5, 3.0]、首样本直接种入。
- 费率纯函数 `core.utils.estimate_tokens` 原地保留不改（CJK 1.5/字、高熵段 len/2、其余 len/3）。
- 每个 task 结束时 `python -m pytest tests/ -q` 必须与基线同绿（存量环境性失败 8 个不计：compact_flow_e2e 1 个、observe_outcomes 1 个、bash_exec 2 个、script_runner 3 个、golden_conformance 1 个）。
- 提交信息风格：`feat(llm)/refactor(...)` + 中文说明 + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

---

### Task 1: HeuristicTokenizer 默认组件

**Files:**
- Create: `src/ctx_weft/providers/llm/tokenizer.py`
- Test: `tests/unit/test_heuristic_tokenizer.py`

**Interfaces:**
- Consumes: `ctx_weft.core.utils.estimate_tokens`（已有纯函数）
- Produces: `HeuristicTokenizer` 类——`count(text: str) -> int`（已校准）、`observe(estimated: int, actual: int) -> None`、`factor: float` 只读属性（测试/调试用）。构造参数 `alpha=0.3, min_sample_tokens=512, factor_min=0.5, factor_max=3.0`。后续 task 的 adapter 组合它；gateway/prepare 测试直接构造它。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_heuristic_tokenizer.py
"""HeuristicTokenizer：启发式费率 × 对数空间伺服校准。

count 返回已校准值；observe 收 (已校准估算段, 真实段)。首样本直接种入
factor=act/est（此时 factor=1，act/est 即原始比值）；此后 factor *= (act/est)^α
（均衡点 = 校准后估算贴住真实值）；恒 clamp [0.5, 3.0]；小样本/非正真实值跳过。
"""
from __future__ import annotations

import pytest

from ctx_weft.core.utils import estimate_tokens
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


def test_count_equals_heuristic_when_uncalibrated():
    t = HeuristicTokenizer()
    assert t.count("hello world") == estimate_tokens("hello world")
    assert t.count("你好世界") == estimate_tokens("你好世界")


def test_count_empty_is_zero():
    assert HeuristicTokenizer().count("") == 0


def test_first_sample_seeds_factor_directly():
    t = HeuristicTokenizer()
    t.observe(1000, 2000)
    assert t.factor == pytest.approx(2.0)


def test_count_applies_learned_factor():
    t = HeuristicTokenizer()
    t.observe(1000, 2000)  # factor=2.0
    # "R"*4000 → 启发式(高熵段) 2000 → ×2
    assert t.count("R" * 4000) == 4000


def test_subsequent_samples_damped_by_alpha():
    t = HeuristicTokenizer(alpha=0.3)
    t.observe(1000, 2000)          # 种入 2.0
    t.observe(1000, 1500)          # factor *= 1.5^0.3
    assert t.factor == pytest.approx(2.0 * 1.5 ** 0.3)


def test_equilibrium_no_drift():
    # 校准后估算 == 真实 → factor 不动（伺服均衡点）
    t = HeuristicTokenizer()
    t.observe(1000, 2000)
    t.observe(1000, 1000)
    assert t.factor == pytest.approx(2.0)


def test_factor_clamped_both_directions():
    hi = HeuristicTokenizer()
    hi.observe(1000, 999_000)
    assert hi.factor == 3.0
    lo = HeuristicTokenizer()
    lo.observe(999_000, 1000)
    assert lo.factor == 0.5


def test_small_or_nonpositive_samples_ignored():
    t = HeuristicTokenizer(min_sample_tokens=512)
    t.observe(100, 10_000)   # est < 512
    t.observe(1000, 0)       # act <= 0
    t.observe(1000, -5)
    assert t.factor == 1.0


def test_nonempty_count_at_least_one():
    t = HeuristicTokenizer()
    t.observe(1000, 500)  # factor=0.5
    assert t.count("a") >= 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_heuristic_tokenizer.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'ctx_weft.providers.llm.tokenizer'`

- [ ] **Step 3: 最小实现**

```python
# src/ctx_weft/providers/llm/tokenizer.py
"""HeuristicTokenizer：启发式费率 × 伺服校准的默认 Tokenizer 实现。

adapter 组合此组件实现 LLMClient.tokenizer 硬契约（见 protocols.llm.Tokenizer）。
count 返回**已校准**估算——「原始估算 × factor」的内部结构对 core 不可见；observe
收 (已校准估算段, 真实段)，在对数空间伺服修正：factor *= (act/est)^α，均衡点即
「校准后估算 = 真实值」。首样本直接种入（此时 factor=1，act/est 就是原始比值），
避免慢启动。恒 clamp [factor_min, factor_max]：异常样本危害有界。est <
min_sample_tokens 或 act ≤ 0 的样本噪声占主导，跳过。

进程内状态、不持久化（一个会话几轮内收敛）；将来接真实 tokenizer（tiktoken/HF
词表）只需 adapter 内部换实现，协议不动。
"""
from __future__ import annotations

from ctx_weft.core.utils import estimate_tokens


class HeuristicTokenizer:
    def __init__(
        self,
        alpha: float = 0.3,
        min_sample_tokens: int = 512,
        factor_min: float = 0.5,
        factor_max: float = 3.0,
    ) -> None:
        self._alpha = alpha
        self._min_sample_tokens = min_sample_tokens
        self._factor_min = factor_min
        self._factor_max = factor_max
        self._factor = 1.0
        self._seeded = False

    @property
    def factor(self) -> float:
        return self._factor

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(estimate_tokens(text) * self._factor))

    def observe(self, estimated: int, actual: int) -> None:
        if estimated < self._min_sample_tokens or actual <= 0:
            return
        ratio = actual / estimated
        if not self._seeded:
            self._factor = ratio
            self._seeded = True
        else:
            self._factor *= ratio ** self._alpha
        self._factor = max(self._factor_min, min(self._factor_max, self._factor))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_heuristic_tokenizer.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add src/ctx_weft/providers/llm/tokenizer.py tests/unit/test_heuristic_tokenizer.py
git commit -m "feat(llm): HeuristicTokenizer——启发式费率×对数空间伺服校准的默认 Tokenizer 组件"
```

---

### Task 2: 协议换槽位 + 三 adapter 实现

**Files:**
- Modify: `src/ctx_weft/protocols/llm.py`（加 `Tokenizer` 协议 + `LLMClient.tokenizer` 抽象属性；删 `count_tokens`，约 line 267-270）
- Modify: `src/ctx_weft/providers/llm/openai.py`（组合 tokenizer，删 count_tokens 约 line 258-259）
- Modify: `src/ctx_weft/providers/llm/anthropic.py`（同上，约 line 277-278）
- Modify: `src/ctx_weft/providers/llm/mock.py`（同上 + usage 生成改走自身 tokenizer，line 89-106/113-114）
- Modify: `src/ctx_weft/providers/llm/provider.py`（`_FixedModelClient.tokenizer` 委派，删 count_tokens 委派 line 106-107）
- Test: `tests/unit/test_tokenizer_protocol.py`

**Interfaces:**
- Consumes: Task 1 的 `HeuristicTokenizer`
- Produces: `protocols.llm.Tokenizer`（runtime_checkable Protocol：`count(text: str) -> int`、`observe(estimated: int, actual: int) -> None`）；`LLMClient.tokenizer -> Tokenizer` 抽象属性；各 adapter 的 `tokenizer_for(model: str) -> Tokenizer`（按 model 惰性分桶，_FixedModelClient 经它取绑定模型那只）。后续所有 core task 用 `ctx.llm.tokenizer`。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_tokenizer_protocol.py
"""LLMClient.tokenizer 硬契约：三 adapter + _FixedModelClient 符合性；count_tokens 已删。"""
from __future__ import annotations

from ctx_weft.protocols.llm import LLMClient, Tokenizer
from ctx_weft.providers.llm.anthropic import AnthropicAdapter
from ctx_weft.providers.llm.mock import MockLLMAdapter, MockResponse
from ctx_weft.providers.llm.openai import OpenAIAdapter
from ctx_weft.providers.llm.provider import _FixedModelClient


def test_adapters_expose_tokenizer():
    for client in (
        OpenAIAdapter(api_key="k", model="m"),
        AnthropicAdapter(api_key="k", model="m"),
        MockLLMAdapter(responses=[]),
    ):
        assert isinstance(client.tokenizer, Tokenizer)


def test_tokenizer_per_model_isolated():
    adapter = OpenAIAdapter(api_key="k", model="m1")
    t1 = adapter.tokenizer_for("m1")
    t2 = adapter.tokenizer_for("m2")
    assert t1 is not t2
    assert adapter.tokenizer_for("m1") is t1        # 同 model 稳定同一实例
    t1.observe(1000, 2000)
    assert t1.factor != t2.factor                   # 各学各的


def test_fixed_model_client_binds_model_tokenizer():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "mx", context_limit=100_000, output_reserve=4096)
    assert client.tokenizer is adapter.tokenizer_for("mx")


def test_count_tokens_removed_from_protocol():
    assert not hasattr(LLMClient, "count_tokens")


async def test_mock_usage_via_own_tokenizer():
    adapter = MockLLMAdapter(responses=[MockResponse(text="ok")])
    from ctx_weft.protocols import LLMMessage, LLMRequest
    req = LLMRequest(model="mock", system="SYS",
                     messages=[LLMMessage(role="user", content="hello world")])
    usage = None
    async for ch in adapter.complete(req):
        if ch.kind == "usage":
            usage = ch.usage
    expected = adapter.tokenizer.count("SYS" + "\n".join(["hello world"]))
    assert usage.prompt_tokens == expected
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_tokenizer_protocol.py -q`
Expected: FAIL（`Tokenizer` 不存在 / adapter 无 tokenizer 属性）

- [ ] **Step 3: 协议实现**

`protocols/llm.py`：在 `LLMClient` 定义前加（模块 docstring 的 Adapter 契约列表同步补一行 `Tokenizer`）：

```python
@runtime_checkable
class Tokenizer(Protocol):
    """同步、纯本地的 token 计数 + 真实用量回喂。禁止网络调用。

    count 返回**已校准**估算（内部校准结构对 core 不可见）；observe 由循环在真实
    usage 到达后回喂 (估算段, 真实段)，实现据此自校准（如伺服 EMA）。
    """

    def count(self, text: str) -> int: ...

    def observe(self, estimated: int, actual: int) -> None: ...
```

`LLMClient` 里删除整个 `count_tokens` 抽象方法，加：

```python
    @property
    @abstractmethod
    def tokenizer(self) -> "Tokenizer":
        """该 client 绑定模型的 tokenizer。count 已含校准；observe 回喂真实用量。"""
        ...
```

- [ ] **Step 4: adapter 实现**

三个 adapter（openai/anthropic/mock）各自：删 `count_tokens` 方法与 `estimate_tokens` import（mock 的 usage 生成同步改，见下），加：

```python
    # __init__ 末尾：
        self._tokenizers: dict[str, HeuristicTokenizer] = {}

    def tokenizer_for(self, model: str) -> HeuristicTokenizer:
        """按 model 惰性分桶的校准 tokenizer（_FixedModelClient 经此取绑定模型那只）。"""
        if model not in self._tokenizers:
            self._tokenizers[model] = HeuristicTokenizer()
        return self._tokenizers[model]

    @property
    def tokenizer(self) -> HeuristicTokenizer:
        return self.tokenizer_for(self._model)
```

（import：`from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer`。mock 无 `self._model`，其 `tokenizer` 属性用 `self.tokenizer_for("mock")`；usage 生成处 `estimate_tokens(prompt_text)` → `self.tokenizer.count(prompt_text)`、`estimate_tokens(text)` → `self.tokenizer.count(text)`。）

`provider.py` `_FixedModelClient`：删 count_tokens 委派（line 106-107），加：

```python
    @property
    def tokenizer(self):
        return self._adapter.tokenizer_for(self._model)
```

- [ ] **Step 5: 跑测试 + 清残留**

Run: `python -m pytest tests/unit/test_tokenizer_protocol.py -q` → 全 PASS
Run: `grep -rn "count_tokens" src/ tests/` → src 无残留；tests 若有调用点一并改为 `tokenizer.count`（同步、去 await）。
Run: `python -m pytest tests/ -q` → 与基线同绿

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(llm): LLMClient.tokenizer 硬契约替代 count_tokens——三 adapter 按 model 分桶组合 HeuristicTokenizer"
```

---

### Task 3: utils 计费助手收 count 回调

**Files:**
- Modify: `src/ctx_weft/core/utils.py`（`estimate_content_tokens` line 147-156、`estimate_tool_calls_tokens` line 159-168）
- Test: `tests/unit/test_estimate_tokens.py`（追加）

**Interfaces:**
- Consumes: 无（纯函数改造）
- Produces: `estimate_content_tokens(content, count=None)`、`estimate_tool_calls_tokens(tool_calls, count=None)`——`count: Callable[[str], int] | None`，None 回退模块内 `estimate_tokens`（未校准启发式，供纯单测/无 llm 场景）；生产调用点后续 task 全部显式传 tokenizer.count。framing(4)/图片(1600) 常数不变、不过回调。

- [ ] **Step 1: 写失败测试**（追加到 `tests/unit/test_estimate_tokens.py` 末尾）

```python
def test_content_tokens_routes_text_through_count_callback():
    # 文本费率经回调；framing 常数不过回调
    assert estimate_content_tokens("hello world", count=lambda t: 100) == 4 + 100


def test_content_tokens_callback_default_is_heuristic():
    assert estimate_content_tokens("hello world") == estimate_content_tokens(
        "hello world", count=estimate_tokens)


def test_tool_calls_tokens_routes_through_count_callback():
    tc = [{"name": "w", "input": {"c": "x"}}]
    assert estimate_tool_calls_tokens(tc, count=lambda t: 10) == 20  # name + args 各 10


def test_image_constant_not_routed_through_callback():
    c = [TextPart(text="x"), ImagePart(data="A" * 100, media_type="image/png")]
    got = estimate_content_tokens(c, count=lambda t: 0)
    assert got == 4 + 0 + 1600
```

- [ ] **Step 2: 确认失败**：`python -m pytest tests/unit/test_estimate_tokens.py -q` → TypeError（不收 count）

- [ ] **Step 3: 实现**：两函数加 `count=None` 关键字参数，函数体内 `count = count or estimate_tokens`，把原 `estimate_tokens(...)` 文本调用换成 `count(...)`（`_MSG_FRAMING_TOKENS`/`_IMAGE_PART_TOKENS` 常数逻辑不动）。docstring 注明「文本费率经 count 回调走 tokenizer；None 回退未校准启发式（纯单测/无 llm 场景）」。

- [ ] **Step 4: 确认通过 + 回归**：`python -m pytest tests/unit/test_estimate_tokens.py tests/unit/test_dynamic_max_tokens.py -q` → 全 PASS

- [ ] **Step 5: Commit**：`git add -A && git commit -m "refactor(utils): 计费助手收 count 回调——文本费率可改道 tokenizer，framing/图片常数留 core"`

---

### Task 4: gateway 改道 + act 回喂 + 删 token_calibration

**Files:**
- Modify: `src/ctx_weft/core/loop/llm_gateway.py`（`request_prompt_estimate` 约 line 295-320、`_estimate_message_tokens`/`_estimate_request_tokens` line 266-292、import 块 line 53-63）
- Modify: `src/ctx_weft/core/loop/steps/act.py`（`_run_llm_turn` 内估算调用 line ~214 与回喂 line ~283、import）
- Modify: `src/ctx_weft/core/loop/steps/observe.py`（line ~113 调用点）
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（line ~82 调用点）
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py`（line ~137 调用点）
- Delete: `src/ctx_weft/core/loop/token_calibration.py`
- Delete: `tests/unit/test_token_calibration.py`（有效测试已由 Task 1/本 task 新测试覆盖）
- Modify: `tests/unit/conftest.py`（删 `_reset_token_calibration` autouse fixture；`_FakeLLM` 加 tokenizer）
- Modify: `tests/unit/test_dynamic_max_tokens.py`（request_prompt_estimate 调用点补 tokenizer 参数 + 校准应用测试）

**Interfaces:**
- Consumes: `protocols.llm.Tokenizer`（Task 2）、`HeuristicTokenizer`（测试构造用）
- Produces: `request_prompt_estimate(tokenizer, request, loop_guard, baseline_msg_count) -> int`；模块常量 `PROMPT_EST_BASE_KEY = "prompt_est_base"`（gateway 定义，act import）；`_estimate_message_tokens(m, count)`、`_estimate_request_tokens(request, count)`。act 回喂模式：`ctx.llm.tokenizer.observe(est − base, usage.prompt_tokens − base)`。

- [ ] **Step 1: 写失败测试**（`test_dynamic_max_tokens.py`：现有 `request_prompt_estimate(req, guard, n)` 调用全部改为 `request_prompt_estimate(_tok(), req, guard, n)`，文件头加 helper；并追加校准语义测试）

```python
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
from ctx_weft.core.loop.llm_gateway import PROMPT_EST_BASE_KEY


def _tok(factor_from: tuple[int, int] | None = None) -> HeuristicTokenizer:
    t = HeuristicTokenizer()
    if factor_from:
        t.observe(*factor_from)
    return t


def test_estimate_incremental_delta_calibrated():
    # tokenizer 学到 2x → 增量段 ×2、真实基线不乘
    req = _req(messages=[
        LLMMessage(role="user", content="old"),
        LLMMessage(role="assistant", content="prev"),
        LLMMessage(role="tool", content="R" * 4000, tool_call_id="t1"),
    ])
    est = request_prompt_estimate(_tok((1000, 2000)), req, _guard(context_tokens=50_000), 2)
    # delta = framing(4，常数不乘) + count("R"*4000)=4000（2000×2） = 4004
    assert est == 50_000 + 4 + 4000
    assert req.metadata[PROMPT_EST_BASE_KEY] == 50_000


def test_estimate_full_path_calibrated_and_base_zero():
    req = _req(messages=[LLMMessage(role="user", content="X" * 40_000)])
    tok = _tok((1000, 2000))
    est = request_prompt_estimate(tok, req, _guard(context_tokens=0), None)
    assert est == _estimate_request_tokens(req, tok.count)
    assert req.metadata[PROMPT_EST_BASE_KEY] == 0
```

（注意：`_estimate_message_tokens`/`_estimate_request_tokens` 的既有测试调用同步补 `count` 参数——纯启发式处传 `estimate_tokens`。）

- [ ] **Step 2: 确认失败**：`python -m pytest tests/unit/test_dynamic_max_tokens.py -q` → TypeError/断言失败

- [ ] **Step 3: gateway 实现**

```python
PROMPT_EST_BASE_KEY = "prompt_est_base"  # request.metadata 瞬态键：估算基线（act 回喂用）


def _estimate_message_tokens(m: LLMMessage, count) -> int:
    total = estimate_content_tokens(m.content, count=count) \
        + estimate_tool_calls_tokens(m.tool_calls, count=count)
    if m.reasoning_content:
        total += count(m.reasoning_content)
    return total


def _estimate_request_tokens(request: "LLMRequest", count) -> int:
    total = count(request.system or "")
    for m in request.messages:
        total += _estimate_message_tokens(m, count)
    for t in request.tools:
        total += count(t.name) + count(t.description or "")
        total += count(json.dumps(t.input_schema, ensure_ascii=False))
    return total


def request_prompt_estimate(tokenizer, request, loop_guard, baseline_msg_count):
    # docstring 更新：估算全经 tokenizer.count（已校准值）；不再有 raw/factor 概念；
    # metadata 只记 PROMPT_EST_BASE_KEY 供 act 回喂算估算段/真实段。
    ctx_tokens = getattr(loop_guard, "context_tokens", 0) if loop_guard is not None else 0
    if baseline_msg_count is not None and ctx_tokens > 0:
        delta = sum(
            _estimate_message_tokens(m, tokenizer.count)
            for m in request.messages[baseline_msg_count:]
        )
        request.metadata[PROMPT_EST_BASE_KEY] = ctx_tokens
        return ctx_tokens + delta
    full = _estimate_request_tokens(request, tokenizer.count)
    request.metadata[PROMPT_EST_BASE_KEY] = 0
    return max(full, ctx_tokens)
```

删除 gateway 对 `token_calibration` 的 import（`PROMPT_EST_BASE_KEY/PROMPT_EST_RAW_KEY/calibration_factor`）。

- [ ] **Step 4: 四个调用点 + act 回喂**

act/observe/compact/recognize_intent 的 `request_prompt_estimate(...)` 首参补 `ctx.llm.tokenizer`。act.py 回喂处（原 `observe_request_outcome(llm_request, usage)`）替换为：

```python
    # token 自校准回喂：真实 usage 与发送前估算段作比（基线不参与），喂给该模型 tokenizer
    base = llm_request.metadata.get(PROMPT_EST_BASE_KEY)
    if usage.prompt_tokens > 0 and llm_request.prompt_token_estimate and base is not None:
        ctx.llm.tokenizer.observe(
            llm_request.prompt_token_estimate - base, usage.prompt_tokens - base)
```

（act import 改：删 `observe_request_outcome`，从 gateway 加 `PROMPT_EST_BASE_KEY`。）

- [ ] **Step 5: 删全局态**

删 `src/ctx_weft/core/loop/token_calibration.py`、`tests/unit/test_token_calibration.py`、conftest 的 `_reset_token_calibration` fixture。conftest `_FakeLLM` 加：

```python
    class _FakeLLM:
        def __init__(self) -> None:
            from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer
            self.tokenizer = HeuristicTokenizer()
        ...
```

Run: `grep -rn "token_calibration\|PROMPT_EST_RAW" src/ tests/` → 无残留。

- [ ] **Step 6: 确认通过 + 回归**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -q` → PASS
Run: `python -m pytest tests/ -q` → 与基线同绿。其余测试文件里以 SimpleNamespace 伪造 llm 且经过 act/observe/compact 路径的 fake，报 `AttributeError: tokenizer` 处一律补 `tokenizer=HeuristicTokenizer()` 字段。

- [ ] **Step 7: Commit**：`git add -A && git commit -m "refactor(gateway): 估算改道 LLMClient.tokenizer——删 core 全局校准单例，act 回喂 tokenizer.observe"`

---

### Task 5: prepare 改道

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/prepare.py`（`_estimate_record_tokens` line ~63、`_estimate_assembled_tokens` line ~77、`_estimate_tokens` line ~245、execute line ~150；删 calibration import 与 `resolve_llm_identity` 校准接线）
- Modify: `tests/unit/test_prepare_estimate.py`
- Test: `tests/unit/test_token_calibration.py` 中 prepare 两测试的等价物并入 `test_prepare_estimate.py`

**Interfaces:**
- Consumes: `ctx.llm.tokenizer`（Task 2）、utils 回调签名（Task 3）
- Produces: `_estimate_record_tokens(r, count)`、`_estimate_assembled_tokens(prompt, count)`（model 参数删除，改收 counter）；`PrepareStep._estimate_tokens` 增量段经 `ctx.llm.tokenizer.count`。

- [ ] **Step 1: 写失败测试**（`test_prepare_estimate.py`：现有调用补 `count=estimate_tokens` 形参改为第二参 `estimate_tokens`；追加）

```python
from ctx_weft.core.utils import estimate_tokens


def test_assembled_estimate_uses_counter():
    prompt = SimpleNamespace(
        system="SYS", messages=[LLMMessage(role="user", content="R" * 4000)], tools=[])
    # counter 恒 7 → system 7 + (framing 4 + content 7)
    assert _estimate_assembled_tokens(prompt, lambda t: 7) == 7 + 4 + 7


async def test_prepare_incremental_estimate_via_llm_tokenizer(fake_state_ctx):
    from ctx_weft.core.loop.steps.prepare import PrepareStep

    state, ctx = fake_state_ctx
    ctx.llm.tokenizer.observe(1000, 2000)  # 学到 2x
    state.agent.loop_guard.context_message_count = 1
    est, has_baseline = await PrepareStep()._estimate_tokens(state, ctx)
    assert has_baseline is True
    # 增量 = 最新 1 条 LLM_RESPONSE "hello llm"：framing 4 + count(9 chars)=6（3×2）
    assert est == 1000 + 4 + 6
```

- [ ] **Step 2: 确认失败**：`python -m pytest tests/unit/test_prepare_estimate.py -q`

- [ ] **Step 3: 实现**：`_estimate_record_tokens(r, count)` / `_estimate_assembled_tokens(prompt, count)` 内部全走 `count=count`；`_estimate_tokens` 里 `factor` 相关两行替换为 `count = ctx.llm.tokenizer.count` 并传入 `_estimate_record_tokens(r, count)`（不再乘 factor——count 已校准）；execute 的 `_estimate_assembled_tokens(prompt, resolve_llm_identity(state)[0])` → `_estimate_assembled_tokens(prompt, ctx.llm.tokenizer.count)`。删 `calibration_factor`/`resolve_llm_identity` import（后者若仅剩校准用途）。

- [ ] **Step 4: 确认通过 + 回归**：`python -m pytest tests/unit/test_prepare_estimate.py tests/ -q` → 与基线同绿

- [ ] **Step 5: Commit**：`git add -A && git commit -m "refactor(prepare): compact 触发估算改道 ctx.llm.tokenizer"`

---

### Task 6: 装配链路——ContextRequest.token_counter

**Files:**
- Modify: `src/ctx_weft/core/assembler/assembler.py`（ContextRequest 加字段，line ~59-75）
- Modify: `src/ctx_weft/core/assembler/composer.py`（line ~324 token_count）
- Modify: sources（每处 `estimate_tokens(...)` → `request.token_counter(...)`）：
  `sources/blackboard.py:77`、`sources/agent_recall.py:120`、`sources/identity.py:50,69`、
  `sources/capability.py:80,103,123`、`sources/guidance.py:47`、`sources/knowledge.py:61`、
  `sources/task_spec.py:52`、`sources/long_memory.py:63`、`sources/_history.py:88`
- Modify: ContextRequest 构造点（`grep -rn "ContextRequest(" src/` 全列；已知：prepare.py、compact.py、observe.py、background_observe.py、runtime.py 若有）——有 ctx 的传 `token_counter=ctx.llm.tokenizer.count`
- Test: `tests/unit/test_assembler_token_counter.py`

**Interfaces:**
- Consumes: Task 2 的 tokenizer
- Produces: `ContextRequest.token_counter: Callable[[str], int] = estimate_tokens`（默认未校准启发式——测试/无 llm 场景回退；生产构造点全部显式传）。sources/composer 统一经 `request.token_counter` 计数。

- [ ] **Step 1: 写失败测试**

```python
# tests/unit/test_assembler_token_counter.py
"""装配链路统一经 ContextRequest.token_counter 计数（默认回退未校准启发式）。"""
from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import ContextRequest
from ctx_weft.core.utils import estimate_tokens
from ctx_weft.protocols import MemoryScope


def _request(counter=None):
    kw = {} if counter is None else {"token_counter": counter}
    return ContextRequest(
        purpose="act",
        scope=MemoryScope(session_id="s", task_id="t", agent_id="a"),
        task=SimpleNamespace(id="t"),
        agent=SimpleNamespace(id="a"),
        session=SimpleNamespace(id="s"),
        template=None,
        bound_capabilities=[],
        **kw,
    )


def test_default_counter_is_heuristic():
    assert _request().token_counter is estimate_tokens


def test_counter_field_carried():
    marker = lambda t: 42
    assert _request(marker).token_counter is marker


async def test_guidance_source_uses_request_counter():
    from ctx_weft.core.assembler.sources.guidance import GuidanceSource

    req = _request(lambda t: 42)
    req.extra = {"act_guidance": "some guidance text"}
    blocks = []
    async for b in GuidanceSource().fetch(req, SimpleNamespace()):
        blocks.append(b)
    assert blocks and blocks[0].token_estimate == 42
```

（GuidanceSource 的类名/fetch 签名以 `sources/guidance.py` 实际为准，测试写作时对齐。）

- [ ] **Step 2: 确认失败**：`python -m pytest tests/unit/test_assembler_token_counter.py -q` → TypeError（无该字段）

- [ ] **Step 3: 实现**

ContextRequest 加字段（`from ctx_weft.core.utils import estimate_tokens` 已可 import；置于 extra 之前、给默认值）：

```python
    # token 计数回调：生产由 step 构造时传 ctx.llm.tokenizer.count（已校准、随当次模型）；
    # 默认回退未校准启发式（测试/无 llm 场景）。sources/composer 统一经它计数。
    token_counter: Callable[[str], int] = estimate_tokens
```

composer.py:324：两处 `estimate_tokens(...)` → `request.token_counter(...)`。
11 个 source 文件：每处 `estimate_tokens(...)` → `request.token_counter(...)`（`_history.py:88` 保持 `record.metadata.get("token_count") or request.token_counter(text)` 结构）；随后删除各文件失效的 `estimate_tokens` import。
构造点：`grep -rn "ContextRequest(" src/` 逐个补 `token_counter=ctx.llm.tokenizer.count`（无 ctx.llm 的构造点——如有——留默认并在该处注释原因）。

- [ ] **Step 4: 确认通过 + 回归**：`python -m pytest tests/unit/test_assembler_token_counter.py tests/ -q` → 与基线同绿
Run: `grep -rn "estimate_tokens" src/ctx_weft/core/assembler/` → 仅 assembler.py 的默认值 import 残留

- [ ] **Step 5: Commit**：`git add -A && git commit -m "refactor(assembler): 装配链路计数统一经 ContextRequest.token_counter——随请求携带当次模型 tokenizer"`

---

### Task 7: 散点改道 + 全量收口

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/compact.py`（`_active_memory_tokens` line ~221）
- Modify: `src/ctx_weft/core/loop/steps/finalize.py`（line ~277 短任务阈值判断）
- Modify: `src/ctx_weft/core/loop/steps/background_observe.py`（line ~77 短段免折门）
- Modify: `docs/spec/`、`docs/ctx-weft_设计文档.md` 中提及 `count_tokens` 的段落（grep 后按实际改）

**Interfaces:**
- Consumes: `ctx.llm.tokenizer.count`
- Produces: 全仓生产路径无直接 `estimate_tokens` 调用（仅剩：utils 内部实现、HeuristicTokenizer、ContextRequest 默认值、测试）。

- [ ] **Step 1: 三个散点改道**

模式统一：函数若已有 `ctx` 在作用域 → `estimate_tokens(text)` 改 `ctx.llm.tokenizer.count(text)`；若是无 ctx 的纯 helper（finalize.py:277、background_observe.py:77 按实际签名判断）→ helper 加 `count` 参数，由持有 ctx 的调用方传 `ctx.llm.tokenizer.count`。改完各文件删失效 import。

- [ ] **Step 2: 无残留验证**

Run: `grep -rn "estimate_tokens" src/ctx_weft/ --include="*.py" | grep -v "utils.py\|tokenizer.py\|assembler.py"`
Expected: 空（或每条残留有明确豁免注释）

- [ ] **Step 3: 文档 touch-up**

`grep -rn "count_tokens" docs/` → 提及协议契约处改为 `tokenizer`（只改契约描述，不重写文档）。

- [ ] **Step 4: 全量回归 + lint**

Run: `python -m pytest tests/ -q` → 与基线同绿（存量 8 个环境性失败之外零失败）
Run: `python -m ruff check src/ tests/ --output-format concise | grep -v "RUF00"` → 无新增（与改前逐文件计数对比）

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(loop): 散点阈值判断改道 tokenizer + 文档契约同步——全链路改道收口"
```
