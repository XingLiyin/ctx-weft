# LLM Token 用量缓存拆分设计

日期：2026-07-16
状态：待评审
范围：ctx-weft（core + providers）；host（LoomeX 系）仅给对接指引，另仓实施

## 1. 背景与问题

当前 token 统计的完整链路只有一个咽喉：

```
adapter 产 LLMUsage
  → act/observe 以 dataclasses.asdict(usage) 塞进 LLMResponseFinished 等事件 payload
  → host 读 LLM_RESPONSE_FINISHED 累加，自造 SSE 帧 token_update 推前端
```

三处问题：

1. **缓存信息在 adapter 层被丢弃。** Anthropic 返回的 `cache_read_input_tokens`（缓存命中）
   / `cache_creation_input_tokens`（缓存写入），OpenAI 返回的
   `prompt_tokens_details.cached_tokens`（及 DeepSeek 方言 `prompt_cache_hit_tokens`），
   两个 adapter 都没读。`LLMUsage` 也没有承载字段。
2. **统计口径严重高估成本。** host 侧「累计输入」是每轮全量 prompt 的累加（每轮把整个
   上下文重算一遍），随轮数近似平方增长；其中绝大部分实际是缓存命中（按 0.1x 计费），
   不拆分则无法看出真实开销。
3. **口径地雷：Anthropic 的 `input_tokens` 不含缓存部分。** 一旦将来给 Anthropic adapter
   开 `cache_control`，`prompt_tokens` 会骤降为「未缓存部分」，core 的
   `loop_guard.context_tokens`、80% context 阈值、compact 触发、`session.token_used`
   预算熔断全部失真。OpenAI 的 `prompt_tokens` 则**包含**缓存命中。两家口径必须归一。

另有一个已知盲点：`RecognizeIntentStep`（recognize_intent.py）只捞 tool_call chunk，
usage 直接丢弃——意图识别的 LLM 开销目前无账可查。

## 2. 目标

1. 每次 LLM 调用的 usage 拆分为以下可统计量（均为显式字段）：
   - 输入侧四分账：
     - **总输入** `prompt_tokens`（含缓存读/写，口径跨 provider 归一）
     - **缓存命中** `cache_read_tokens`
     - **缓存写入** `cache_write_tokens`（Anthropic 独有计费项，1.25x/2x）
     - **实际未缓存输入** `uncached_prompt_tokens`（显式落盘；未显式给出时构造期自动派生）
   - 输出侧拆分：
     - **推理输出** `reasoning_tokens`（completion 中属于推理/thinking 的子集；
       provider 无单列时为 0）
2. 拆分明细随现有事件 payload 透出（SSE 消费端按需聚合），**纯增量改动**：
   事件类型清单不动、`schema_version` 不动、reducer/投影/恢复不动。
3. 保护 core 记账语义：context 阈值 / token 预算继续基于「真实总输入」，
   Anthropic 将来开缓存也不失真。

## 3. 非目标

- session 级累计拆分（state 模型 / reducer 投影 / 崩溃恢复均不改）——已确认聚合只做在
  每次调用的事件层，host 自行累计。
- 给 Anthropic adapter 启用 `cache_control`（prompt caching）——另行立项；本设计为其
  铺平口径，使届时零统计改动。
- host 仓库（LoomeX）的实施——本文 §8 给对接指引。
- 计费金额换算——core 只出 token 数，价格表是 host/前端的事。

## 4. 方案选型

| 方案 | 说明 | 结论 |
|---|---|---|
| **A. 扩展 LLMUsage + adapter 内归一化** | 协议层加四个显式字段（缓存读/写、推理输出缺省 0，实际输入哨兵自动派生），各 adapter 负责把自家方言翻译成统一口径 | **选定**：改动最小、口径唯一、事件自动透传 |
| B. LLMUsage 加 `provider_usage: dict` 原样透传 | 灵活，但把口径歧义推给每个消费端，payload 结构不稳定，replay 契约弱化 | 否 |
| C. 新增独立统计事件（TokenUsageReported） | 统计流与业务流分离 | 否（需扩 V1 冻结事件清单；用户已确认不需要） |

## 5. 详细设计

### 5.1 协议层：LLMUsage 扩展（protocols/llm.py）

```python
@dataclass
class LLMUsage:
    """token 使用统计。

    口径（跨 provider 归一，由各 adapter 负责翻译）：
      输入侧：prompt_tokens          — 本次请求的全部输入（含缓存读/写部分）
             cache_read_tokens      — 输入中命中缓存的部分
             cache_write_tokens     — 输入中本次写入缓存的部分
                                      （Anthropic cache_creation；OpenAI 系恒 0）
             uncached_prompt_tokens — 实际未缓存输入（全价计费部分）
      输出侧：completion_tokens      — 全部输出
             reasoning_tokens       — 输出中属于推理/thinking 的子集
                                      （OpenAI/DeepSeek 单列；Anthropic 无单列恒 0）
    不变式：
      prompt_tokens = uncached_prompt_tokens + cache_read_tokens + cache_write_tokens
      reasoning_tokens ≤ completion_tokens
      total_tokens = prompt_tokens + completion_tokens
    uncached_prompt_tokens 未显式给出（哨兵 -1）时在 __post_init__ 按不变式自动派生，
    保证任何构造写法下账目自洽。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    uncached_prompt_tokens: int = -1  # 实际输入；未显式给出时自动派生
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        if self.uncached_prompt_tokens < 0:
            self.uncached_prompt_tokens = max(
                0,
                self.prompt_tokens - self.cache_read_tokens - self.cache_write_tokens,
            )
```

要点：

- **显式字段而非派生属性**：与既有惯例一致（`total_tokens` 本就是显式存的冗余和），
  且随 `dataclasses.asdict` 自动进事件 payload，多语言消费端不必各自做减法。
- **哨兵缺省 + 自动派生**：现有全部构造点（mock、finalize、测试）都不传此字段，
  `__post_init__` 里自动算 `prompt − read − write`（无缓存信息时即 = prompt），
  零改动且账目正确；杜绝「缺省 0 = 静默错账」。
- **provider 权威值优先**：adapter 拿得到原始值时显式传入（Anthropic 的 `input_tokens`
  本身就是未缓存部分的权威数），拿不到时留给派生（OpenAI 只报 cached 子集）。
- `max(0, ...)` 防御 provider 返回异常值（cached > prompt）时派生出负数；
  显式传入的值不钳制，保留原始账。

### 5.2 Anthropic adapter 归一化（providers/llm/anthropic.py）

Anthropic streaming 的 usage 分布：`message_start` 带
`{input_tokens, cache_read_input_tokens, cache_creation_input_tokens}`，
`message_delta` 尾包带 `{output_tokens}`（部分代理会在尾包重发输入侧字段）。

改动（`_stream` 内）：

1. `message_start` 处除 `input_tokens` 外，再读 `cache_read_input_tokens`、
   `cache_creation_input_tokens`（缺省 0）。
2. `message_delta` 处若 usage 携带这三个输入侧字段则覆盖（防御代理尾包给全量）。
3. 组装 `LLMUsage` 时归一化：

```python
uncached = input_tokens or 0                       # Anthropic input_tokens 即未缓存部分（权威值）
prompt_total = uncached + cache_read + cache_write # 归一为「全部输入」口径
usage = LLMUsage(
    prompt_tokens=prompt_total,
    completion_tokens=output_tokens,
    total_tokens=prompt_total + output_tokens,
    cache_read_tokens=cache_read,
    cache_write_tokens=cache_write,
    uncached_prompt_tokens=uncached,               # 显式记 provider 原值，不经派生
    # reasoning_tokens 恒 0：Anthropic thinking 计入 output_tokens 无单列；
    # 不用流式 thinking 文本自行估算——估算值混进计费口径就是错账。
)
```

当前 payload 未发 `cache_control`，cache 字段恒 0，`prompt_total == input_tokens`，
**行为与现状全等**；将来开缓存时统计与阈值自动正确。

### 5.3 OpenAI adapter（providers/llm/openai.py）

OpenAI 的 `prompt_tokens` 已含缓存命中，无需归一，只补拆分：

```python
prompt_details = usage_data.get("prompt_tokens_details") or {}
completion_details = usage_data.get("completion_tokens_details") or {}
cache_read = prompt_details.get("cached_tokens") or usage_data.get("prompt_cache_hit_tokens", 0)
usage = LLMUsage(
    prompt_tokens=usage_data.get("prompt_tokens", 0),
    completion_tokens=usage_data.get("completion_tokens", 0),
    total_tokens=usage_data.get("total_tokens", 0),
    cache_read_tokens=cache_read or 0,
    cache_write_tokens=0,   # OpenAI 系不区分/不计费缓存写入
    # uncached_prompt_tokens 不传 → __post_init__ 派生 prompt − cached
    # （OpenAI 只报 cached 子集，无未缓存原始值可记）
    reasoning_tokens=completion_details.get("reasoning_tokens", 0) or 0,
)
```

回退顺序：缓存命中取标准 `prompt_tokens_details.cached_tokens` → DeepSeek 方言
`prompt_cache_hit_tokens` → 0；推理输出取标准 `completion_tokens_details.reasoning_tokens`
（DeepSeek R1 系同名同位）→ 0。方言差异全部封死在 adapter 内。

### 5.4 Mock adapter（providers/llm/mock.py）

`MockResponse` 增加可选 `cache_read_tokens: int = 0`、`cache_write_tokens: int = 0`、
`reasoning_tokens: int = 0`，usage chunk 原样带出。用途：事件层单测与 host/前端联调
可模拟缓存命中/推理输出场景。注意 mock 的 `prompt_tokens` 是估算总输入，模拟时
cache 字段应 ≤ 估算值（测试自行保证）。

### 5.5 事件与 payload：零代码改动，自动透传

- `act.py` / `observe.py`（含 background observe）均以 `dataclasses.asdict(usage)` 入
  payload → `LLMResponseFinished` / `BackgroundObserveResponseFinished` 的 `usage` 自动
  多四个键（`cache_read_tokens` / `cache_write_tokens` / `uncached_prompt_tokens` /
  `reasoning_tokens`）。memory ingest 的 `metadata["usage"]` 同理。
- `_finalize.py` 只透传 usage 对象，不改。
- **`schema_version` 保持 1**：纯增量加键；replay 旧事件时消费端 `.get(..., 0)` 兜底。
- 文档同步：`docs/ctx-weft_设计文档.md` §9 的 `LLMResponseFinished` payload 表与
  `LLMUsageDict` TypedDict 增补四字段。

### 5.6 core 记账语义：不变，且被本设计保护

- `_account_tokens`（act.py）、observe 的 loop_guard 更新、80% context 阈值、
  `session.token_used += prompt + completion`、compact 触发——全部继续读
  `prompt_tokens`。语义上它们需要的是「真实上下文规模」，缓存命中不缩小上下文，
  归一化保证这一点。**不新增任何 core 记账逻辑。**

### 5.7 盲点收口：RecognizeIntentStep 的 usage 透出

`recognize_intent.py` 的 stream 循环补一个分支捕获 usage chunk，并把
`"usage": dataclasses.asdict(usage)` 写进 `RECOGNIZE_INTENT_COMPLETED` payload。

- 只透出、**不记账**（不加进 `session.token_used`）：记账口径变化影响预算熔断语义，
  超出本设计范围，另行决策。
- `BackgroundObserveResponseFinished` 已带 usage 但 host 未统计——列入 §8 host 指引。

## 6. 兼容性

| 影响面 | 结论 |
|---|---|
| LLMUsage 构造点 | 新字段带默认值，现有代码/测试零改动 |
| 事件 payload | 纯增键；reducer 不消费 usage，投影/快照/恢复不受影响 |
| 现有测试 | 均按单字段断言（无 usage dict 全等断言，已核对），全绿预期 |
| host 旧版本 | 忽略新键，无部署顺序约束 |
| replay 旧事件 | usage dict 缺新键：cache 两键 `.get` 兜底 0，`uncached_prompt_tokens` 兜底回退 `prompt_tokens`（旧事件无缓存拆分即全额实际输入） |

## 7. 测试计划

1. **LLMUsage 构造语义**（新增或并入协议测试）：
   - 不传 `uncached_prompt_tokens` → 自动派生 `prompt − read − write`；
   - 无缓存信息 `LLMUsage(prompt_tokens=100)` → `uncached == 100`（旧写法自洽）；
   - 显式传入 → 原样保留不派生；异常账（cached > prompt）派生结果钳 0。
2. **anthropic 单测**（test_anthropic_stream_finalize.py 扩展）：
   - `message_start` usage 带 `input_tokens=7, cache_read_input_tokens=100,
     cache_creation_input_tokens=20` → 断言 `prompt_tokens == 127`、
     `cache_read_tokens == 100`、`cache_write_tokens == 20`、
     `uncached_prompt_tokens == 7`（显式记原值）、`total_tokens == 127 + output`、
     `reasoning_tokens == 0`（Anthropic 无单列）。
   - 不带缓存字段的现有夹具 → 行为与现状全等（回归）。
   - `message_delta` 尾包重发输入侧字段 → 覆盖生效。
3. **openai 单测**（test_openai_stream_finalize.py 扩展）：
   - `prompt_tokens_details.cached_tokens` 路径（断言派生的 uncached = prompt − cached）；
   - DeepSeek `prompt_cache_hit_tokens` 回退路径；
   - `completion_tokens_details.reasoning_tokens` → `reasoning_tokens`；
   - 无 details 的现有夹具回归（cache/reasoning 字段为 0，uncached = prompt）。
4. **事件层**：act 流程用 mock adapter 带缓存字段 → `LLMResponseFinished.payload["usage"]`
   含七键且值正确、满足不变式；memory metadata 同步。
5. **recognize_intent**：usage chunk 进 `RECOGNIZE_INTENT_COMPLETED` payload。
6. **全量回归**：现有 finalize / golden / threshold / observe 套件不动全绿。

## 8. host 对接指引（LoomeX，另仓实施，informative）

- `session.py`（事件翻译器）在 `LLM_RESPONSE_FINISHED` 分支扩展累加：
  `cache_read_used += usage.get("cache_read_tokens", 0)`、
  `cache_write_used += ...`、
  `actual_input_used += usage.get("uncached_prompt_tokens", usage.get("prompt_tokens", 0))`
  （新键直接可用，回退兜住 replay 旧事件）。
- `token_update` SSE 帧增加 `cache_read_tokens_used` / `cache_write_tokens_used` /
  `actual_input_tokens_used`；会话快照 dict 与 `session_update` 帧同步携带。
- 建议顺带把 `BACKGROUND_OBSERVE_RESPONSE_FINISHED` 纳入统计（现为盲区）。
- 前端 `SessionStats` 展示建议：「↑ 输入 xk（命中缓存 yk · 实际 zk）」，输出可附
  「（推理 rk）」；计费估算 = 实际输入×单价 + 命中×0.1x + 写入×1.25x + 输出×单价
  （价格表在 host 配置）。

## 9. 风险与边界

- **provider 返回异常值**（如 cached > prompt）：显式传入的字段保留原始账不钳制，
  `__post_init__` 自动派生路径 `max(0, ...)` 防负；不做静默修正，异常口径靠账目可见性暴露。
- **方言漂移**：新 provider 的缓存字段名各异，约定一律在其 adapter 内翻译成
  LLMUsage 统一口径，协议层字段语义唯一，禁止把方言字段漏到事件里。
- **`prompt_token_estimate` 不受影响**：网关估算的是装配侧输入量，与缓存无关。
