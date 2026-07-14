# 按窗口实时计算请求 max_tokens 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 发 LLM 请求时按 `context_limit` 与当前窗口实时占用计算 `max_tokens`，窗口空时把剩余窗口全给输出、渐满渐收紧。

**Architecture:** 纯算术函数 `dynamic_max_tokens()`（`core/utils.py`）+ 网关侧 `apply_dynamic_max_tokens()`（`core/loop/llm_gateway.py`）在发送前就地写入 `request.max_tokens`。计算集中在两个持有 `ctx` 的调用点（`stream_llm_resilient` 覆盖 act/observe/compact；`recognize_intent` 直连路径单独接线）。天花板默认 = `context_limit`，可选 `output_ceiling`（经 `ModelConfig` → `_FixedModelClient`）作收紧上限。

**Tech Stack:** Python 3.12+，dataclass，pytest（async 测试，`pytest-asyncio` 已启用——现有测试直接 `async def test_*` 无装饰器）。

## Global Constraints

- **不改 token 估算口径**：`estimate_tokens = len//4`（`core/utils.py:48`）保持不变。CJK/JSON 低估已知并接受。
- **不动输入侧 reserve**：`reserved_output_tokens`(8192) 及 `effective_limit()` 不改。
- **仅当 `request.max_tokens is None` 时计算**：尊重显式设值（探活 ping 走 adapter 直连不经网关，天然不受影响）。
- **默认天花板 = `context_limit`**；`output_ceiling` 未配 → 回退 `context_limit`（零回归）。已用量 = `max(loop_guard.context_tokens, estimate_tokens(本次 prompt))`，estimate **遍历全部 messages（含本轮新加的 `role="tool"` result）**。
- **本仓为 core**：host（`api/sessions.py`、env `IPMC_LLM_OUTPUT_CEILING`）在另一仓，不在本计划范围；本计划到 `ModelConfig.output_ceiling` 为止（host 后续经此字段透传）。
- 参照 spec：`docs/superpowers/specs/2026-07-13-dynamic-max-tokens-design.md`。
- 提交信息用中文 conventional-commit 前缀（`feat:`/`test:` 等），与仓库历史一致。

---

## 文件结构

- `src/ctx_weft/core/utils.py` — 新增纯函数 `dynamic_max_tokens()`（单一真源，紧邻 `effective_limit`）。
- `src/ctx_weft/core/config.py` — `RuntimeConfig` 加两个旋钮。
- `src/ctx_weft/core/loop/llm_gateway.py` — 新增 `_estimate_request_tokens()` + `apply_dynamic_max_tokens()`；`stream_llm_resilient` 顶部接线。
- `src/ctx_weft/core/loop/steps/recognize_intent.py` — 直连 `stream_llm` 前接线。
- `src/ctx_weft/protocols/llm.py` — `LLMClient` 加 `output_ceiling` 说明注释（不改必需接口，网关 duck-type 读取）。
- `src/ctx_weft/providers/llm/provider.py` — `ModelConfig.output_ceiling` + `_FixedModelClient` 携带 + `get_client` 透传。
- `tests/unit/test_dynamic_max_tokens.py` — 新增（Task 1 纯函数 + Task 2 网关 helper）。
- `tests/unit/test_llm_provider_models.py` 或新增 — Task 3 `output_ceiling` 透传断言。

---

### Task 1: `dynamic_max_tokens()` 纯函数

**Files:**
- Modify: `src/ctx_weft/core/utils.py`（在 `effective_limit`（第 55-57 行）之后新增）
- Test: `tests/unit/test_dynamic_max_tokens.py`（新建）

**Interfaces:**
- Consumes: 无。
- Produces: `dynamic_max_tokens(context_limit: int, context_tokens: int, prompt_estimate: int, ceiling: int, *, margin: int = 4096, floor: int = 1024) -> int`

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_dynamic_max_tokens.py`：

```python
"""dynamic_max_tokens：按窗口实时算请求输出上限（纯算术）。"""
from __future__ import annotations

from ctx_weft.core.utils import dynamic_max_tokens


def test_empty_window_releases_full_remaining():
    # 窗口几乎空 + 默认 ceiling=context_limit → ≈ context_limit − margin
    got = dynamic_max_tokens(200_000, 0, 100, ceiling=200_000, margin=4096, floor=1024)
    assert got == 200_000 - 100 - 4096


def test_used_takes_max_of_real_and_estimate():
    # context_tokens(真实,上轮) 与 prompt_estimate(本次含 tool result) 取大
    # 本次估算更大（本轮新加大 tool result）→ 用它算剩余
    got = dynamic_max_tokens(200_000, 50_000, 120_000, ceiling=200_000, margin=4096, floor=1024)
    assert got == 200_000 - 120_000 - 4096
    # 反向：上轮真实更大 → 用真实
    got2 = dynamic_max_tokens(200_000, 120_000, 50_000, ceiling=200_000, margin=4096, floor=1024)
    assert got2 == 200_000 - 120_000 - 4096


def test_ceiling_clamps_when_configured_smaller():
    # 配了 output_ceiling(< 剩余) → 被夹到 ceiling（Anthropic 硬输出上限场景）
    got = dynamic_max_tokens(200_000, 0, 100, ceiling=8192, margin=4096, floor=1024)
    assert got == 8192


def test_floor_when_window_nearly_full():
    # 剩余 < floor → 被 floor 兜住
    got = dynamic_max_tokens(200_000, 199_000, 199_500, ceiling=200_000, margin=4096, floor=1024)
    assert got == 1024


def test_first_turn_context_tokens_zero():
    got = dynamic_max_tokens(128_000, 0, 3000, ceiling=128_000, margin=4096, floor=1024)
    assert got == 128_000 - 3000 - 4096
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -v`
Expected: FAIL —`ImportError: cannot import name 'dynamic_max_tokens'`。

- [ ] **Step 3: 实现**

在 `src/ctx_weft/core/utils.py` 的 `effective_limit`（55-57 行）之后追加：

```python
def dynamic_max_tokens(
    context_limit: int,
    context_tokens: int,
    prompt_estimate: int,
    ceiling: int,
    *,
    margin: int = 4096,
    floor: int = 1024,
) -> int:
    """按当前窗口占用实时算请求 max_tokens。

    used = max(上轮 provider 真实 prompt_tokens, 本次 prompt 估算)——真实值抗 CJK 低估、
    本次估算抓本轮新增 tool result，取大更保守。max_tokens = context_limit − used − margin，
    夹到 [floor, ceiling]。ceiling 默认由调用方传 context_limit（剩余窗口全给输出）；配小
    则作收紧上限（如 Anthropic 硬输出上限）。
    """
    used = max(context_tokens, prompt_estimate)
    remaining = context_limit - used - margin
    return max(floor, min(ceiling, remaining))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -v`
Expected: PASS（5 passed）。

- [ ] **Step 5: 提交**

```bash
git add src/ctx_weft/core/utils.py tests/unit/test_dynamic_max_tokens.py
git commit -m "feat(llm): dynamic_max_tokens 纯函数——按窗口算输出上限"
```

---

### Task 2: 网关接线（估算 + apply + 两处调用点）

**Files:**
- Modify: `src/ctx_weft/core/config.py:11-26`（`RuntimeConfig` 加旋钮）
- Modify: `src/ctx_weft/core/loop/llm_gateway.py`（imports + 两个 helper + `stream_llm_resilient` 顶部接线）
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py:16,132`（import + 直连前接线）
- Test: `tests/unit/test_dynamic_max_tokens.py`（追加网关 helper 用例）

**Interfaces:**
- Consumes: `dynamic_max_tokens(...)`（Task 1）；`estimate_tokens`、`content_to_text`（`core/utils.py`）；`_cfg_val(ctx, name, default)`（`llm_gateway.py:251`）。
- Produces:
  - `_estimate_request_tokens(request) -> int`
  - `apply_dynamic_max_tokens(ctx, request, loop_guard) -> None`（`request.max_tokens is None` 且 `loop_guard` 非 None 时就地写入）
  - `RuntimeConfig.dynamic_max_tokens_margin: int = 4096`、`RuntimeConfig.dynamic_max_tokens_floor: int = 1024`

- [ ] **Step 1: 写失败测试（追加到 test_dynamic_max_tokens.py 末尾）**

```python
# ── 网关 helper ────────────────────────────────────────────────────────────────
from types import SimpleNamespace

from ctx_weft.protocols import LLMMessage, LLMRequest, LLMTool
from ctx_weft.core.loop.llm_gateway import (
    apply_dynamic_max_tokens,
    _estimate_request_tokens,
)


def _req(**kw):
    base = dict(model="m", system="sys", messages=[LLMMessage(role="user", content="hi")], tools=[])
    base.update(kw)
    return LLMRequest(**base)


def _ctx(llm, config=None):
    return SimpleNamespace(llm=llm, config=config)


def _llm(context_limit=200_000, output_ceiling=None):
    return SimpleNamespace(context_limit=context_limit, output_ceiling=output_ceiling)


def _guard(context_limit=200_000, context_tokens=0):
    return SimpleNamespace(context_limit=context_limit, context_tokens=context_tokens)


def test_estimate_includes_tool_result_messages():
    # role="tool" 消息必须计入（context_tokens 漏掉的本轮增量靠它抓）
    req = _req(messages=[
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="tool", content="X" * 4000, tool_call_id="t1"),
    ])
    est = _estimate_request_tokens(req)
    assert est >= 1000  # 4000 字符 tool result ≈ 1000 token，被计入


def test_apply_sets_max_tokens_when_none():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard(context_tokens=0))
    # 空窗口 → 剩余窗口全给输出，接近 context_limit
    assert req.max_tokens is not None and req.max_tokens > 100_000


def test_apply_respects_explicit_max_tokens():
    req = _req(max_tokens=16)
    apply_dynamic_max_tokens(_ctx(_llm()), req, _guard())
    assert req.max_tokens == 16  # 不覆盖


def test_apply_noop_when_loop_guard_none():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm()), req, None)
    assert req.max_tokens is None


def test_apply_ceiling_fallback_to_context_limit():
    # output_ceiling 缺省(None) → 回退 context_limit，不被夹到小值
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm(context_limit=50_000, output_ceiling=None)), req, _guard(context_limit=50_000))
    assert req.max_tokens > 40_000


def test_apply_ceiling_clamps_when_configured():
    req = _req()
    apply_dynamic_max_tokens(_ctx(_llm(output_ceiling=8192)), req, _guard(context_tokens=0))
    assert req.max_tokens == 8192
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -v`
Expected: FAIL —`ImportError: cannot import name 'apply_dynamic_max_tokens'`。

- [ ] **Step 3a: 加 RuntimeConfig 旋钮**

在 `src/ctx_weft/core/config.py` `RuntimeConfig` 末尾（第 26 行 `llm_self_heal_max_interval_sec` 之后）追加：

```python
    # 动态 max_tokens（apply_dynamic_max_tokens 读取；默认=安全值）
    dynamic_max_tokens_margin: int = 4096
    dynamic_max_tokens_floor: int = 1024
```

- [ ] **Step 3b: 加网关 helper**

在 `src/ctx_weft/core/loop/llm_gateway.py` 顶部 import 区（第 49 行 `from ctx_weft.protocols import ...` 附近）补：

```python
import json
from ctx_weft.core.utils import content_to_text, dynamic_max_tokens, estimate_tokens
```

在 `stream_llm`（233 行）之前新增两个 helper：

```python
def _estimate_request_tokens(request: "LLMRequest") -> int:
    """估算本次待发 prompt 的 token（system + 全部 messages + tools schema）。

    遍历**全部** messages，含本轮新加的 role="tool" result——这是 loop_guard.context_tokens
    （上一轮真实值）漏掉的增量，"取大"逻辑正靠它补齐。len//4 口径不变（低估已知）。
    """
    total = estimate_tokens(request.system or "")
    for m in request.messages:
        total += estimate_tokens(content_to_text(m.content))
    for t in request.tools:
        total += estimate_tokens(t.name) + estimate_tokens(t.description or "")
        total += estimate_tokens(json.dumps(t.input_schema, ensure_ascii=False))
    return total


def apply_dynamic_max_tokens(ctx, request: "LLMRequest", loop_guard) -> None:
    """发送前按窗口就地写入 request.max_tokens。

    仅当未显式设值（None）且有 loop_guard 时生效。天花板取 llm.output_ceiling（duck-type，
    缺省/None → 回退 llm.context_limit）。margin/floor 从 ctx.config 取，缺省回退安全值。
    """
    if request.max_tokens is not None or loop_guard is None:
        return
    llm = ctx.llm
    margin = int(_cfg_val(ctx, "dynamic_max_tokens_margin", 4096))
    floor = int(_cfg_val(ctx, "dynamic_max_tokens_floor", 1024))
    ceiling = getattr(llm, "output_ceiling", None) or llm.context_limit
    request.max_tokens = dynamic_max_tokens(
        loop_guard.context_limit,
        loop_guard.context_tokens,
        _estimate_request_tokens(request),
        ceiling,
        margin=margin,
        floor=floor,
    )
```

> 说明（相对 spec §4.3 的实现精化）：不给 `stream_llm` 加 `loop_guard` 参，改在两个持有 `ctx` 的调用点接线——`ctx.config`（margin/floor）只在那里可得，且 `stream_llm` 签名保持不变。

- [ ] **Step 3c: `stream_llm_resilient` 顶部接线**

在 `src/ctx_weft/core/loop/llm_gateway.py` `stream_llm_resilient`（307 行）函数体内、`from ctx_weft.protocols import LLMCallError`（316 行）之后、`max_attempts = ...`（318 行）之前插入：

```python
    loop_guard = getattr(getattr(state, "agent", None), "loop_guard", None)
    apply_dynamic_max_tokens(ctx, request, loop_guard)
```

（`state` 可能为 None / 无 agent 的 SimpleNamespace——getattr 链回退 None，helper 自动 no-op，现有 self-heal 测试不受影响。）

- [ ] **Step 3d: `recognize_intent` 直连前接线**

在 `src/ctx_weft/core/loop/steps/recognize_intent.py`：
- 第 16 行 `from ctx_weft.core.loop.llm_gateway import stream_llm` 改为
  `from ctx_weft.core.loop.llm_gateway import stream_llm, apply_dynamic_max_tokens`
- 在第 132 行 `async for chunk in stream_llm(ctx.llm, llm_request):` 之前插入一行：

```python
        apply_dynamic_max_tokens(ctx, llm_request, getattr(state.agent, "loop_guard", None))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -v`
Expected: PASS（Task 1 的 5 + 本 Task 的 6，共 11 passed）。

- [ ] **Step 5: 回归——跑网关 / self-heal / 集成相关套件**

Run: `python -m pytest tests/unit/test_llm_self_heal.py tests/unit/test_llm_provider_retry.py tests/integration/test_compact_flow_e2e.py -v`
Expected: PASS。若某集成测试因 `MockLLMAdapter` 经网关后 `max_tokens` 由 None 变为计算值而断言失败——该断言若断的是"max_tokens 为 None"，改为期望计算值或去掉该断言（新行为正确）；`MockLLMAdapter` 无 `output_ceiling` 属性 → `getattr` 回退 `context_limit`，符合预期。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/config.py src/ctx_weft/core/loop/llm_gateway.py src/ctx_weft/core/loop/steps/recognize_intent.py tests/unit/test_dynamic_max_tokens.py
git commit -m "feat(llm): 网关发送前按窗口实时算 max_tokens"
```

---

### Task 3: `output_ceiling` 配置透传（ModelConfig → _FixedModelClient → get_client）

**Files:**
- Modify: `src/ctx_weft/providers/llm/provider.py:27-31`（`ModelConfig`）、`50-83`（`_FixedModelClient`）、`211-215`（`get_client`）
- Modify: `src/ctx_weft/protocols/llm.py:203-205`（`max_output_tokens` property 后加 `output_ceiling` 说明注释）
- Test: `tests/unit/test_dynamic_max_tokens.py`（追加透传用例）

**Interfaces:**
- Consumes: 网关 `apply_dynamic_max_tokens` 的 `getattr(llm, "output_ceiling", None)`（Task 2）。
- Produces: `ModelConfig.output_ceiling: int | None = None`；`_FixedModelClient(..., output_ceiling: int | None = None)` + `@property output_ceiling -> int | None`。

- [ ] **Step 1: 写失败测试（追加到 test_dynamic_max_tokens.py 末尾）**

```python
# ── output_ceiling 配置透传 ─────────────────────────────────────────────────────
from ctx_weft.providers.llm.provider import ModelConfig, _FixedModelClient
from ctx_weft.providers.llm.mock import MockLLMAdapter


def test_fixed_model_client_exposes_output_ceiling():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "m", context_limit=200_000, max_output_tokens=8192, output_ceiling=64_000)
    assert client.output_ceiling == 64_000


def test_fixed_model_client_output_ceiling_defaults_none():
    adapter = MockLLMAdapter(responses=[])
    client = _FixedModelClient(adapter, "m", context_limit=200_000, max_output_tokens=8192)
    assert client.output_ceiling is None


def test_model_config_has_output_ceiling_field():
    cfg = ModelConfig(name="m", context_limit=200_000, output_ceiling=32_000)
    assert cfg.output_ceiling == 32_000
    assert ModelConfig(name="m2", context_limit=100_000).output_ceiling is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -k output_ceiling -v`
Expected: FAIL —`TypeError: __init__() got an unexpected keyword argument 'output_ceiling'`。

- [ ] **Step 3a: `ModelConfig` 加字段**

`src/ctx_weft/providers/llm/provider.py` 第 27-31 行：

```python
@dataclass
class ModelConfig:
    name: str
    context_limit: int
    max_output_tokens: int = 8192
    output_ceiling: int | None = None  # 单次输出收紧上限；None → 网关回退 context_limit
```

- [ ] **Step 3b: `_FixedModelClient` 携带**

`src/ctx_weft/providers/llm/provider.py` `_FixedModelClient.__init__`（53-63 行）加参与存储，并在 `max_output_tokens` property（69-71 行）后加 property：

```python
    def __init__(
        self,
        adapter: LLMClient,
        model: str,
        context_limit: int,
        max_output_tokens: int,
        output_ceiling: int | None = None,
    ) -> None:
        self._adapter = adapter
        self._model = model
        self._context_limit = context_limit
        self._max_output_tokens = max_output_tokens
        self._output_ceiling = output_ceiling
```

在 `max_output_tokens` property 之后：

```python
    @property
    def output_ceiling(self) -> int | None:
        return self._output_ceiling
```

- [ ] **Step 3c: `get_client` 透传**

`src/ctx_weft/providers/llm/provider.py` 第 211-215 行：

```python
        model_cfg = next((m for m in acc.models if m.name == resolved_model), None)
        ctx_limit = model_cfg.context_limit if model_cfg else 128_000
        max_out = model_cfg.max_output_tokens if model_cfg else 8192
        ceiling = model_cfg.output_ceiling if model_cfg else None

        return _FixedModelClient(adapter, resolved_model, ctx_limit, max_out, ceiling)
```

- [ ] **Step 3d: protocols 加说明注释（不改必需接口）**

`src/ctx_weft/protocols/llm.py` `max_output_tokens` property（201-205 行）之后加注释（不加 abstractmethod，避免强制所有结构实现者 + 破坏 `runtime_checkable` isinstance）：

```python
    # 可选（duck-typed，非协议必需）：output_ceiling -> int | None
    #   单次输出的收紧上限。网关经 getattr 读取，缺省/None → 回退 context_limit。
    #   实现方（_FixedModelClient）可提供；未提供者网关自动回退。
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/unit/test_dynamic_max_tokens.py -v`
Expected: PASS（全部：5 + 6 + 3 = 14 passed）。

- [ ] **Step 5: 回归——LLM provider 套件**

Run: `python -m pytest tests/unit/test_llm_provider_models.py tests/unit/test_llm_provider_retry.py -v`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/providers/llm/provider.py src/ctx_weft/protocols/llm.py tests/unit/test_dynamic_max_tokens.py
git commit -m "feat(llm): ModelConfig.output_ceiling 经 _FixedModelClient 透传"
```

---

## 收尾验证

- [ ] **全量单测**

Run: `python -m pytest tests/unit -q`
Expected: 全绿（新增 14 用例，无回归；如有集成断言需按 Task 2 Step 5 调整）。

- [ ] **端到端 sanity（可选，若环境具备真实 LLM）**：跑一次含大 tool result 的 act 回合，确认请求 `max_tokens` 随窗口下降、无 provider 400。

---

## Self-Review 记录

- **Spec 覆盖**：§4.1 公式→Task 1；§4.1 estimate 含 tool result→Task 2 `_estimate_request_tokens` + 专项用例；§4.2 output_ceiling 解耦/默认 context_limit→Task 2 getattr 回退 + Task 3 配置path；§4.3 单点收口→Task 2 两处接线；§4.4 config→Task 2 Step 3a；§4.5 残余风险→Global Constraints 标注（不修估算）；§6 测试→各 Task 测试 + 收尾。host（§5 host 段）明确超出本仓范围（Global Constraints）。
- **占位符扫描**：无 TBD/TODO；每个代码步含完整代码。
- **类型一致性**：`dynamic_max_tokens` 签名（Task 1 定义 / Task 2 调用）一致；`output_ceiling: int | None`（ModelConfig / _FixedModelClient / getattr 回退）一致；`apply_dynamic_max_tokens(ctx, request, loop_guard)` 签名（定义 / 两处调用）一致。
- **修正**：spec §6 曾提 `test_openai_stream_finalize.py` 需改期望——经核实该文件不断言 `max_tokens`，故本计划不动它（回归以 `test_llm_self_heal` / 集成套件为准）。
