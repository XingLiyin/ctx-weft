# 按上下文窗口实时计算请求 max_tokens

- 日期：2026-07-13
- 状态：设计待评审
- 相关：`core/loop/llm_gateway.py` / `core/loop/steps/recognize_intent.py` / `protocols/llm.py` /
  `providers/llm/*`；前置输入侧 reserve 见 spec `2026-07-02-effective-context-limit-output-reserve-design.md`

## 1. 背景与问题

ctx-weft 发 LLM 请求时**从不设** `max_tokens`：act/observe/compact/recognize_intent 四处构造
`LLMRequest` 都不传（`protocols/llm.py:170` 默认 `None`）。到 adapter 后：

- Anthropic（协议强制 `max_tokens`）回退到 adapter 的 `_max_output_tokens`（默认 8192，
  `anthropic.py:273`）——**无论窗口多空，永远只请求固定 8192 输出**。
- OpenAI 省略字段（`openai.py:268-269`），交服务端默认。

两个问题：

1. **窗口空时白白浪费输出容量**：模型真实单次输出上限可能远大于 8192（部分模型 32k/64k），
   窗口几乎全空时本可让模型一次写更多，却被固定 8192 卡住。
2. **无窗口感知**：`max_tokens` 与"当前窗口还剩多少"完全脱钩。前置 spec（2026-07-02）已在**输入侧**
   按 `effective_limit = context_limit − reserved_output_tokens` 裁剪、为输出留位置，但**输出侧**
   的请求上限仍是死值，两侧不联动。

目标：`max_tokens` 按 `context_limit` 与**当前窗口实时占用**动态计算——窗口空时顶到模型真实
输出上限，窗口越满越收紧。

## 2. 目标 / 非目标

**目标**
1. 请求 `max_tokens` = 由 `context_limit`、当前已用 token 实时算出。
2. 天花板**默认 = `context_limit`**（"容量释放"语义，用户定 2026-07-13）：窗口空时把整个剩余窗口
   都给输出。可选 `output_ceiling` 作收紧上限（配小才生效），供 Anthropic 等有硬输出上限的 provider 用。
3. 与输入侧 reserve **解耦**：不动 `reserved_output_tokens`(8192)，不缩小输入窗口。
4. 单点收口：计算集中一处，四个调用点零/极小改动。
5. 尊重显式设值：`request.max_tokens` 已被显式设定时不覆盖（如探活 ping 的 16）。

**非目标（本次不做）**
- **不改 token 估算口径**。`estimate_tokens = len//4`（utils.py:48）保持不变——对 CJK/JSON
  系统性低估 2~4× 仍在。本方案用"真实值/估算取大 + margin + 天花板"**缓解**超窗风险，**不根治**
  （对齐 2026-07-02 spec §7 的既有取舍）。
- 不改输入侧 budget 裁剪 / effective_limit（那是 2026-07-02 spec 的范畴）。
- 不为 recognize_intent 引入 self-heal 重试（保持其现有直连 `stream_llm` 语义，仅加一行接线）。

## 3. 已定决策

| # | 决策 | 结论 |
|---|---|---|
| 天花板 | 动态上限取值 | **默认 = `context_limit`**（把剩余窗口全给输出=最大容量释放）。新增可选 `output_ceiling` 作**收紧上限**，未配置→回退 `context_limit`；配小才生效（如 Anthropic 硬输出上限） |
| 已用量来源 | "当前窗口已用" | **`max(loop_guard.context_tokens, estimate_tokens(本次 prompt))`**——真实值(抗 CJK 低估) 与本轮估算(抗新增 tool_result) 取大，两方向漏洞互补 |
| margin | 防本轮输入增长超窗 400 | **固定 4096**（可配） |
| floor | 窗口将满时输出预算下限 | **1024**（可配） |
| 落点 | 计算位置 | 网关 helper；`stream_llm` 加可选 `loop_guard` 入参；`stream_llm_resilient` 与 `recognize_intent` 各传一次 |
| 估算不修 | 是否改 `estimate_tokens` | **否**（非目标） |

## 4. 设计

### 4.1 核心公式

```python
def dynamic_max_tokens(
    context_limit: int,
    context_tokens: int,        # loop_guard.context_tokens（上一轮 provider 真实 prompt_tokens）
    prompt_estimate: int,       # estimate_tokens(本次待发 prompt，含本轮 tool result)
    ceiling: int,               # 收紧上限；默认 = context_limit（回退），配小才生效
    *,
    margin: int = 4096,
    floor: int = 1024,
) -> int:
    used = max(context_tokens, prompt_estimate)
    remaining = context_limit - used - margin
    return max(floor, min(ceiling, remaining))
```

行为（默认 `ceiling = context_limit` 下，`min(ceiling, remaining)` 恒取 `remaining`，即"剩余窗口全给输出"）：
- **窗口空 / 首轮**（`context_tokens=0`，prompt 小）：`remaining ≈ context_limit − margin` → 输出容量最大化。
- **窗口渐满**：`used` 增大 → `remaining` 线性下降 → `max_tokens` 收紧。
- **窗口将满**：被 `floor` 兜住（因 act 在 `0.8×effective_limit` 即触发压缩，`floor` 实际极少触发，属防御）。
- **配了 `output_ceiling`（< context_limit）**：`max_tokens` 再被夹到该上限——供 Anthropic 等硬输出上限
  provider 防 400。

> **为何"取大"**：`context_tokens` 是 provider 真实计数（CJK 也准），但滞后上一轮、且是 act 的 prompt
> 形状；`estimate_tokens(本次)` 精确对应本次 prompt（抓住本轮新增的 tool_result），但 len//4 低估 CJK。
> 两者取大 → 得到更保守（更大）的已用量估计 → `max_tokens` 更小更安全，两个方向的漏洞互补。这是
> 用户"先算清楚再定来源"的结论。

`prompt_estimate` 由 `request` 现成字段求和（网关持有 `request`）：
```
estimate_tokens(request.system)
  + Σ estimate_tokens(content_to_text(m.content)) for m in request.messages
  + estimate_tokens(json.dumps([t schema for t in request.tools]))
```
（`content_to_text` 已在 utils.py:60，处理多模态；工具 schema 计入避免大工具集低估。）

> **本轮 tool result 必须计入（"取大"的关键理由）**：`request.messages` 已含本轮新加的 `role="tool"`
> 消息，上式对**全部** messages 求和，故本轮 tool result 天然计入。而 `context_tokens` 是**上一轮**
> provider 真实值、**不含**本轮刚 append 的 tool result——正是这块增量会让"只信 `context_tokens`"
> 低估当前占用、`max_tokens` 偏大而超窗。`max(context_tokens, prompt_estimate)` 里 `prompt_estimate`
> 抓的就是这块增量；求和实现须遍历所有 message（含 `role="tool"`），不得漏。

### 4.2 天花板：默认 `context_limit`，可选 `output_ceiling` 收紧

默认天花板 = `context_limit`（剩余窗口全给输出）。**不复用 `max_output_tokens`(8192) 作天花板**——它是
2026-07-02 spec 的输入 reserve 来源（`reserved_output_tokens = max_output_tokens`），且值太小会把输出
钉死在 8192、失去容量释放。天花板与 reserve 彻底分开：`reserved_output_tokens`(8192) 只管输入侧裁剪，
天花板只管输出请求上限。

新增**可选收紧上限** `output_ceiling`，**默认回退 `context_limit`、零回归**（不配=剩余窗口全给输出）：

- `providers/llm/provider.py` `ModelConfig`：加 `output_ceiling: int | None = None`
  （None → 消费方回退 `context_limit`）。
- `protocols/llm.py` `LLMClient`（Protocol）：声明可选 `output_ceiling` property。因是结构协议、
  不宜强制所有实现，**网关侧用 `getattr(llm, "output_ceiling", 0) or llm.context_limit` 兜底**
  （惯用模式，见 act.py:295 对 `reserved_output_tokens` 的 getattr 回退）。
- 具体 adapter（`anthropic.py` / `openai.py` / `mock.py`）与 `_FixedModelClient`：加 `output_ceiling`
  构造参数 + property，默认 `None`（→ 网关回退 `context_limit`）。
- host 侧：`_resolve_max_output_tokens`（sessions.py）旁加 `_resolve_output_ceiling`，
  创建会话/构造 client 时透传。env 建议 `IPMC_LLM_OUTPUT_CEILING`（缺省=回退 `context_limit`）。

何时配 `output_ceiling`：provider 对单次输出有**硬上限**（如 Anthropic 各模型的 `max_tokens` 上限）时，
配到该上限防 400。OpenAI 兼容端点（vLLM 等）通常只约束 `输入+输出 ≤ 窗口`、无独立输出硬上限 → 用默认
`context_limit` 即可。

> **不变式保持**：`dynamic_max_tokens` 按真实 token 构造 `used + max_tokens ≤ context_limit`（减 margin），
> 故默认天花板即便等于 `context_limit`，也只授予窗口的实际剩余量——输入+输出永不超窗（除 §4.5 残余风险）。
> 对有硬输出上限的 provider，仍须配 `output_ceiling`，否则默认可能请求超过其单次输出上限而 400（见 §4.5）。

### 4.3 落点：网关单点收口

四个调用点的发送路径：
- act / observe / compact → `stream_llm_resilient(ctx, state, request)`（`llm_gateway.py:307`，有 `state`）。
- recognize_intent → **直连** `stream_llm(ctx.llm, request)`（`recognize_intent.py:132`，不经 resilient）。

计算需要 `loop_guard`（context_limit/context_tokens）+ `llm`（output_ceiling/max_output_tokens）。方案：

1. 新增 helper（`llm_gateway.py`）：
   ```python
   def apply_dynamic_max_tokens(request, loop_guard, llm, *, margin=4096, floor=1024) -> None:
       """仅当 request.max_tokens is None 时，就地按窗口算出并写入 max_tokens。"""
       if request.max_tokens is not None or loop_guard is None:
           return
       ceiling = getattr(llm, "output_ceiling", 0) or llm.context_limit
       request.max_tokens = dynamic_max_tokens(
           loop_guard.context_limit, loop_guard.context_tokens,
           _estimate_request_tokens(request), ceiling, margin=margin, floor=floor,
       )
   ```
2. `stream_llm` 加可选参 `loop_guard=None`；非 None 时在合法化后、`llm.complete` 前调 helper。
   - `stream_llm_resilient` 调 `stream_llm(ctx.llm, request, loop_guard=state.agent.loop_guard)`。
     （helper 内 `max_tokens is not None` 短路 → 重试循环重复调用幂等。）
   - `recognize_intent` 直连处改为传 `loop_guard=state.agent.loop_guard`。
3. margin/floor 从 `ctx.config`（`RuntimeConfig`）取，缺省回退 4096/1024（与 `_cfg_val` 现有模式一致）。

> 探活 ping（`provider.py:250-255`）显式设 `max_tokens=16` 且走 adapter 直连、不经网关 → 双重不受影响。

### 4.4 配置

`RuntimeConfig` 加两项（默认写死安全值）：
- `dynamic_max_tokens_margin: int = 4096`
- `dynamic_max_tokens_floor: int = 1024`

`output_ceiling` 走 LLM 配置链（ModelConfig / env / adapter 构造参），非 RuntimeConfig。

### 4.5 已知残余风险

**残余一（估算低估超窗）**：CJK 大 tool_result 的"本轮新增"部分，`context_tokens`（尚未含它）与
`estimate_tokens`（len//4 低估）两个来源都会低估 → 极端下 `used` 偏小、`max_tokens` 偏大，仍可能
`输入+输出 > context_limit` 触发 provider 400。margin(4096) 缓解但不根治，与 2026-07-02 spec §7
"估算不修"的取舍一致。根治需改估算口径，另开专项。

**残余二（硬输出上限 provider）**：默认天花板 = `context_limit`，对**有单次输出硬上限**的 provider
（如 Anthropic 各模型的 `max_tokens` 上限）会在窗口较空时请求超过其输出硬上限而 400。规避：给这类 provider
配 `output_ceiling` 到其硬上限（§4.2）。OpenAI 兼容端点（vLLM 等）无此问题。此为已知取舍：默认偏向
"容量释放"，硬上限 provider 需显式配 `output_ceiling`。

## 5. 受影响文件清单

**core**
- `core/utils.py` — 新增 `dynamic_max_tokens()`（纯算术，单一真源）。
- `core/loop/llm_gateway.py` — 新增 `apply_dynamic_max_tokens()` + `_estimate_request_tokens()`；
  `stream_llm` 加 `loop_guard` 参并在发送前调用；`stream_llm_resilient` 传 `state.agent.loop_guard`。
- `core/loop/steps/recognize_intent.py:132` — 直连 `stream_llm(...)` 传 `loop_guard=state.agent.loop_guard`。
- `core/config.py` — `RuntimeConfig` 加 `dynamic_max_tokens_margin` / `dynamic_max_tokens_floor`。

**providers / protocols**
- `protocols/llm.py` — `LLMClient` 声明可选 `output_ceiling` property（文档说明网关 getattr 兜底）。
- `providers/llm/provider.py` — `ModelConfig.output_ceiling: int | None = None`；`_FixedModelClient`
  加 `output_ceiling` property（None → 网关回退 `context_limit`）；resolve 处透传。
- `providers/llm/anthropic.py` / `openai.py` / `mock.py` — 加 `output_ceiling` 构造参 + property，
  默认 `None`（→ 网关回退 `context_limit`）。

**host**
- `api/sessions.py` — 加 `_resolve_output_ceiling`，创建会话/构造 client 时透传（env `IPMC_LLM_OUTPUT_CEILING`，
  缺省回退）。

## 6. 测试

- **`dynamic_max_tokens()` 单测**：
  - 空窗口（used 小）+ 默认 `ceiling=context_limit` → ≈ `context_limit − margin`（剩余全给输出）。
  - `context_tokens=0` 首轮 → ≈ `context_limit − prompt_estimate − margin`。
  - 窗口渐满 → 线性下降；将满 → 被 `floor` 夹住。
  - 取大逻辑：`context_tokens > estimate` 与反向各一例，取较大 used。
  - **本轮 tool result 计入**：messages 含 `role="tool"` 时 `prompt_estimate` 相应增大，且当它 >
    `context_tokens`（上一轮不含该 result）时 used 取到它。
  - 配 `output_ceiling < context_limit`：`remaining > ceiling` → 被夹到 ceiling。
  - margin/floor 边界；`remaining < floor` → floor。
- **`apply_dynamic_max_tokens()` 单测**：
  - `request.max_tokens` 已设 → 不覆盖。
  - `loop_guard is None` → 不动。
  - `output_ceiling` 未实现（getattr 缺失）或 None → 回退 `context_limit`。
- **网关集成**：经 `stream_llm_resilient` 的请求，发送前 `request.max_tokens` 被正确写入；重试循环幂等
  （第二次尝试不重算/不叠加）。
- **recognize_intent**：直连路径同样写入 `max_tokens`。
- **ceiling 默认/解耦**：`output_ceiling` 未配 → 天花板 = `context_limit`（剩余窗口全给输出）；配小 →
  `max_tokens` 被夹到该上限；无论如何 `reserved_output_tokens`（输入 reserve, 8192）不变。
- **回归**：经网关且断言 `max_tokens` 缺省/为 None 的现有测试（含工作区改动中的
  `tests/unit/test_openai_stream_finalize.py`）更新期望值。ping 路径断言 `max_tokens==16` 仍绿。

## 7. 决策记录

**已确认（2026-07-13）**
- 天花板**默认 = `context_limit`**（剩余窗口全给输出=最大容量释放）；可选 `output_ceiling` 作收紧上限，
  未配→回退 `context_limit`，配小才生效（Anthropic 等硬输出上限 provider 用）。**不复用 `max_output_tokens`**。
- 已用量 = `max(context_tokens, estimate_tokens(本次 prompt))`；`estimate` 遍历全部 messages，
  **含本轮新加的 `role="tool"` result**（context_tokens 漏掉的增量正靠它抓）。
- margin = 固定 4096；floor = 1024；均可配。
- 单点收口：网关 helper + `stream_llm(loop_guard=...)`，`stream_llm_resilient` 与 `recognize_intent` 接线。
- 仅当 `max_tokens is None` 时计算，尊重显式设值；ping 不受影响。
- 估算口径本次不动（非目标），残余 CJK 超窗风险已知并接受（§4.5）。

**评审门开放项**：无。待用户复核 spec 后转 writing-plans。
