# Tokenizer 与校准下沉到 adapter — 设计

日期：2026-07-20
状态：已批准
前置：2026-07-13-dynamic-max-tokens-design.md（动态 max_tokens）、本轮已落地的三层修复
（高熵费率 / 比例 margin / core 侧 EMA 自校准，见 `core/loop/token_calibration.py`）

## 背景与动机

token 估算此前分散两处：字符启发式费率在 `core.utils.estimate_tokens`（模块级纯函数，
全仓十几处直接 import），EMA 自校准在 `core/loop/token_calibration.py`（进程级全局单例，
按 model 名分桶）。问题：

1. **归属错位**：tokenizer 是 provider 私有知识（OpenAI 有 tiktoken、Anthropic 只有计数
   API、DeepSeek/Qwen 要 HF 词表），却由 core 统一猜；协议里的 `count_tokens` 槽位两头
   没接（adapter 全是 `return estimate_tokens(text)` 空壳，core 从不调用）。
2. **全局单例**：校准状态挂在 core 模块全局，生命周期与任何组件都不对齐，测试需要
   autouse fixture 清场，持久化没有自然落点。
3. **core 记账噪声**：EMA 需要"未乘 factor 的原始估算段"，core 被迫在 request.metadata
   里维护两个键（base + raw），并用 `resolve_llm_identity` 按 model 名取 factor。

## 决策（已与用户对齐）

| 决策点 | 结论 |
|---|---|
| 协议形态 | 硬契约（`@abstractmethod`），同步方法，命名走 `tokenizer`；删除 `count_tokens` |
| core 改道范围 | 一次性全改道：循环热路径 + 装配链路（composer/sources） |
| 真实 tokenizer | 本次不引新依赖，全部 adapter 用「启发式×EMA」默认组件；协议留好换实现的口子 |
| 持久化 | 本次不做；状态在 adapter 后，后续加 dump/load 即可 |

## 1. 协议层（`protocols/llm.py`）

```python
@runtime_checkable
class Tokenizer(Protocol):
    """同步、纯本地的 token 计数（返回已校准值）+ 真实用量回喂。禁止网络调用。"""

    def count(self, text: str) -> int: ...
    def observe(self, estimated: int, actual: int) -> None: ...


class LLMClient(Protocol):
    @property
    @abstractmethod
    def tokenizer(self) -> "Tokenizer":
        """该 client 绑定模型的 tokenizer。count 返回已含校准的估算；
        observe 由循环在真实 usage 到达后回喂 (估算段, 真实段)。"""

    # async def count_tokens(...) 删除（两头未接的死槽位，语义由 tokenizer 取代）
```

- `count` 返回**已校准值**；"原始估算 × factor" 的内部结构对 core 不可见。
- `observe` 放在 Tokenizer 上（不在 LLMClient 上）：状态归属清晰，测试可单独构造。
- `Tokenizer` 是纯 Protocol，protocols 层保持零运行时依赖。

## 2. 默认实现（`providers/llm/tokenizer.py`，新模块）

`HeuristicTokenizer`：

```
count(text)       = int(heuristic(text) × factor)
                    # heuristic = core.utils.estimate_tokens（费率纯函数原地保留：
                    #   CJK ceil(1.5n)、高熵段 ≥20 连续 [A-Za-z0-9+/=_-] 按 len/2、其余 len/3）
observe(est, act) : est < min_sample(512) 或 act ≤ 0 → 跳过
                    首样本：factor = act/est 直接种入（此时 factor=1，act/est 即原始比值）
                    此后：factor *= (act/est)^α（α=0.3，对数空间伺服）
                    恒 clamp [0.5, 3.0]
```

**伺服式更新替代原「原始值 EMA」**：core 只见校准值，反馈环闭合在 adapter 内。均衡点
是"校准后估算 = 真实值"；首样本全额纠偏、后续 α 阻尼，收敛性质与原设计等价。

**状态归属**：adapter 实例持 `_tokenizers: dict[model, HeuristicTokenizer]`（一个 adapter
服务多模型），惰性创建；`_FixedModelClient.tokenizer` 返回其绑定模型那只（经 adapter 的
`tokenizer_for(model)` 取）。生命周期 = adapter = 账号注册期；进程内存活，不持久化。

openai / anthropic / mock 三个 adapter 都组合此默认组件。mock 的 usage 生成改用自身
tokenizer（保持自洽）。

## 3. core 循环热路径改道

- `request_prompt_estimate(tokenizer, request, loop_guard, baseline_msg_count)`：新收
  tokenizer 参数；四个调用点（act / observe / compact / recognize_intent）传
  `ctx.llm.tokenizer`。
- metadata 记账减为一个键 `prompt_est_base`（原 `prompt_est_raw` 删除——估算已是校准值）。
- act 循环 usage 到达后：
  `ctx.llm.tokenizer.observe(request.prompt_token_estimate − base, usage.prompt_tokens − base)`。
- prepare 两条估算路径（增量 `_estimate_tokens` / 整份 `_estimate_assembled_tokens`）改收
  counter；删除为取 factor 而做的 `resolve_llm_identity` 接线。
- **删除** `core/loop/token_calibration.py` 与 conftest 的 autouse 重置 fixture。
- `utils.estimate_content_tokens / estimate_tool_calls_tokens` 改为必传 `count` 回调：
  framing(4)/图片(1600) 等计费补偿常数留在 core（消息装配的计费结构知识，非 tokenizer
  知识），文本费率经回调走 tokenizer。

## 4. 装配链路改道

**counter 随 `ContextRequest` 传递**（新字段 `token_counter: Callable[[str], int]`，
PrepareStep 构造时带上 `ctx.llm.tokenizer.count`），composer 预算裁剪与各 assembler source 造 IndexCard 时从
request 取。不注入装配器构造函数的原因：会话中途可切模型（`session.llm_model`），构造期
注入会拿到过期 tokenizer；随 request 传天然跟随当次调用的模型，也免了十几个 source 的
构造签名改动。

散落在 finalize / background_observe / compact 的阈值判断类 `estimate_tokens` 调用同样
改走 `ctx.llm.tokenizer.count`（这些 step 都有 ctx）。

## 5. 测试

- 新增 Tokenizer 伺服语义单测（首样本种入 / α 阻尼 / clamp / 小样本跳过），替代
  `test_token_calibration.py` 的 EMA 部分。
- gateway / prepare 测试直接构造 `HeuristicTokenizer` 传入——无全局态、无需清场。
- 三个 adapter 加协议符合性断言（`isinstance(client.tokenizer, Tokenizer)`）。
- `test_estimate_tokens.py` 的费率数值断言不受影响（费率纯函数原地保留）。

## 风险与边界

- protocols 层零依赖 ✓（Tokenizer 纯 Protocol）；core 不 import providers ✓（只经协议）。
- 删协议方法（count_tokens）：仓库内三个 adapter 一次改齐，无第三方生态负担。
- 多账号同模型各学各的 factor：可接受（不同账号可能代理不同后端，分开学更正确）。
- 冷启动：进程重启后 factor 从 1.0 重学，一两轮收敛；期间由高熵费率修正 + 5% 比例
  margin 兜底（本轮已落地，不动）。

## 不在本次范围

- 真实 tokenizer 接入（tiktoken / HF 词表）——协议口子已留，各 adapter 内部换实现即可。
- 校准状态持久化（随 LLMProvider.store 存 `{model: factor}`）。
- 图片/工具 framing 常数下沉 adapter（provider 计费模型差异，将来可作为 Tokenizer 扩展）。
