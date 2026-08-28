# 模态能力回归 adapter——core 对多模态零判断

> 状态：设计已批准（2026-08-28），待实施
> 上级：`2026-08-20-multimodal-design.md`（§6.7 视觉门控由本设计推翻）
> 相关：`2026-08-24-multimodal-phase3a-guardrails.md`（引入被推翻的那道门控）

## 1. 问题

Phase 3a 在入口加了一道视觉门控：`validate_content` 读
`getattr(llm, "supports_vision", False)`，为假就抛 `VisionNotSupportedError`、
拒绝整份内容、不落库。严格默认——未声明即视为无视觉能力。

这道门控有三个问题，且互相加强。

**① core 在替一个它管不着的东西做决定。** README 写得很直白：「ctx-weft 只定义
`LLMClient` 协议，不包含任何真实 LLM SDK」。host 接自己的模型时**整个 `complete()`
都是它的**——序列化、模态处置、报错还是降级，全在它手里。core 却在入口先行判定
「这个模型不能收图」，等于替一个尚未运行、且完全由 host 实现的组件预先做了决定。

**② 判据本身是 duck-typed 的，且大概率读不到。** `supports_vision` 不在
`LLMClient` 协议上（`protocols/llm.py` 明写「可选（duck-typed，非协议必需）」），
第三方 adapter 没有任何义务实现它。于是严格默认的实际效果是：**凡是 host 自写的
adapter，一律被判为无视觉能力**——哪怕它明明支持。host 要解开这道门，得去
`ModelConfig` 上配一个只有内置 `LLMProvider` 才认的字段。

**③ 已经衍生出第二处判据。** 因为入口拒了用户递的图，工具产出的图就成了唯一漏网
路径，于是 `llm_gateway._gate_tool_images` 又读一遍同一个属性、只降 `role == "tool"`
的图。同一件事两处判定、覆盖面还不同（一处拒、一处降；一处全角色、一处单角色），
这是典型的会分叉结构。

**代价是真实的**：无视觉模型收到图片时，内容在入口就被拒，**一个字都不落库**。
换个支持视觉的模型重开会话，那张图并不存在——不是看不到，是从来没进来过。

## 2. 决定

**能力由「注册了哪个 adapter 类」表达——类型即声明。** core 对模态零判断，
runtime 全程透传多模态数据到 `LLMClient` 面前，由实现方决定发什么。

三条被否决的替代方案，记下来免得日后重提：

| 方案 | 否决理由 |
|---|---|
| `LLMRequest` 新增 `supports_vision` 字段下发 | 请求本身就自带答案——`request.messages` 里有没有 `ImagePart` 一看便知。再塞一个标志是把已在场的信息从外面复述一遍 |
| 保留 `ModelConfig.supports_vision`，adapter 查表 | 能力是 adapter 实现的性质，不是模型的配置项。且 adapter 是 per-account 共享的、`ModelConfig` 是 per-model，要把表交到 adapter 手上必须传 `LLMAccount` 引用（`remove_model` 会重新绑定 `account.models`，快照必然过期） |
| 内置 adapter 按 `request.model` 自备能力表 | 等于把一张模型能力表焊进 SDK，必然过时，且宿主无从覆盖 |

## 3. 内置 adapter：一个可覆盖的接缝

`_serialize_messages` **一字不改**。它现在已经能正确处理图片 part——
`_parts_to_blocks`、OpenAI 的 tool 图重定位全都在。两种 adapter 的唯一区别是：
图片能不能活着走到它面前。

故在基类上开一个接缝，纯文本行为是基类的默认：

```python
# AnthropicAdapter（基类 = 纯文本）
def _prepare_messages(self, messages: list[LLMMessage]) -> list[LLMMessage]:
    """纯文本 adapter：图片降级成确定性文本占位并告警。子类覆盖以原样透传。

    含图才降级、才告警；纯文本返回**同一对象**，零开销、零日志。
    """

# AnthropicMultimodalAdapter(AnthropicAdapter) —— 类的全部内容
def _prepare_messages(self, messages):
    return messages
```

调用点：`_build_payload` 里的 `_serialize_messages(request.messages)` 改成
`_serialize_messages(self._prepare_messages(request.messages))`。OpenAI 侧同构
（其 `_serialize_messages` 签名是 `(system, messages)`，只换第二个实参）。

降级复用归一层的 `content.downgrade_images_to_text`，占位是 `[image {media_type}]`
——与 per-purpose 降级同一条，逐字节确定、不砸 prompt 前缀缓存
（占位清单见 `core/media/refs.py` 模块 docstring）。

### 3.1 只记 warning，不抛异常

adapter 在**同步出网主路径**上，任何 raise 都会掀掉整个 LLM 请求。这与仓内已反复
确立的取向一致：`_parts_to_blocks` 对畸形 part 走 `getattr` 兜底而不 raise、
`rehydrate_content` 在 `get` 返回 None 时降级而不 raise、`MemoryBlobStore.get`
契约上恒不抛。同一条。

warning 措辞要点明三件事，**把修复路径直接写进日志**：哪个 model、降了几张图、
以及「需要发图请改用 `AnthropicMultimodalAdapter` / `OpenAIMultimodalAdapter`」。

**只在真的降级了时发**（即本次请求确实含图）。纯文本会话一条日志都不多——
这条同时也是「纯文本路径逐字节不变」这一贯穿约束在本设计里的落点。

### 3.2 降级后 OpenAI 的 tool 图重定位天然空转

`_TOOL_IMAGE_NOTICE` 那段逻辑（把 `role="tool"` 里的图攒起来、合并成随后的 user
消息）在纯文本 adapter 上不需要任何改动：降级后已无 image part，`relocated` 恒为空，
一条 user 消息都不追加。

## 4. 接线

- `SUPPORTED_STYLES` 增加 `"anthropic-multimodal"` / `"openai-multimodal"`，
  `_build_adapter` 相应分派
- `providers/llm/__init__.py` 按既有 PEP 562 惰性导出两个新名字——它们依赖 httpx，
  不能模块级 import（沿用 `AnthropicAdapter` / `OpenAIAdapter` 现有的 `__getattr__` 写法）
- 存量 account 的 style 字符串不变 → 仍拿到纯文本 adapter，**零迁移**。要发图就改
  style，或走 README 方式 A 直接传实例
- `MockLLMAdapter` 不动：它不拼 wire payload，没有 `_serialize_messages` / `_build_payload`
  这条链，模态处置对它无意义

## 5. 删除清单

| 落点 | 动作 |
|---|---|
| `ModelConfig.supports_vision` / `_FixedModelClient.supports_vision` | 删。这 4 处是多模态分支自己加的，master 上没有——删完即回 master 形状，宿主零成本 |
| `content.validate_content` | 删 `llm` / `llm_resolver` 两个参数与视觉门控分支；三道门变两道，顺序理由注释随之改写 |
| `llm_gateway._gate_tool_images` | 整个删掉，含 `stream_llm` 里的调用点 |
| `runtime._validate_and_normalize_content` | 签名收缩掉 `llm` / `llm_account` / `llm_model` |
| `runtime.start_session` | 「惰性解析保住纯文本不提前解析 LLM」的长注释作废——不再解析 LLM，该不变量自动成立 |
| `runtime._normalize_hitl_content` | 不再取 `req.resume_llm_*`，只留 tenant 解析 |
| `errors.VisionNotSupportedError` | 删。再无抛出点，且不在 `ctx_weft/__init__` 导出面上；留一个永不被抛的异常类只会让宿主写出永不执行的 `except` |
| `protocols/llm.py` duck-typed 注释块 | 改写：core 对模态零判断，如何处置由 `LLMClient` 实现方自理 |

**`_gate_tool_images` 必须删而不是搬。** 它只降 `role == "tool"` 的图，docstring 里
写明理由是「用户递的图在入口已被门控拒掉，工具产出的图是唯一漏网路径」。那个前提
没了，覆盖面就得扩到所有角色——而那正是 `_prepare_messages` 干的事。留在 gateway
就是第 1 节 ③ 说的第二处判据，将来必然分叉。

## 6. 保留清单（明确不动）

`validate_content` 的另外两道门控**都留着**，它们与模型能力无关：

- **格式校验**（media_type 白名单 / `b64decode(validate=True)` / 单图 5 MiB 上限）
  ——防的是畸形字节。`b64decode` 默认 `validate=False` 不抛，会静默解出垃圾，
  这是静默损坏而非能力问题。
- **EventBlobStore 门控**——守的是「事件库恒不含字节」这条不变量。没有 event blob
  store 就无处放字节，与哪个模型收这份内容无关。

`validate_content` 的门控顺序理由（格式 → blob）保持不变：格式畸形的内容必须报
`InvalidContentError`，不能被 blob 门控抢先拦成 `BlobStoreRequiredError`。

## 7. 行为变更

无视觉模型 + 图片：

| | 旧 | 新 |
|---|---|---|
| 入口 | 抛 `VisionNotSupportedError` | 正常接收 |
| 落库 | **不落库** | 正常落 memory + 外部化进 blob |
| 出网 | 不可达 | 纯文本 adapter 降级成 `[image {media_type}]` + warning |
| 会话 | 中断 | 继续 |

**代价要说清楚**：图片会占 memory / blob 存储与 token 预算，即使这个 adapter 永远
不会把它发出去；此外纯文本 adapter 下每一轮仍会为每张图付一次 blob 读 + base64
编码（`stream_llm` 的 rehydrate 在 adapter 降级之前跑），编码完就被丢弃。换来的是
三件事——core 对模态零判断、host 自写的 `LLMClient` 完全自治、以及换成多模态
adapter 后**同一份历史立刻可看图**（旧行为下那张图从未进过系统）。

`ModelConfig.supports_vision` 消失，`spec 2026-08-20-multimodal-design §6.7` 的
「严格默认拒绝」随之作废。

## 8. 测试

- 删 `tests/unit/test_vision_gating.py`
- 新建 `tests/unit/test_adapter_multimodal_dispatch.py`：四个类（两家 × 纯文本 /
  多模态）× user / assistant / tool 三种角色，断言 payload 形态，并断言
  **warning 只在含图时发一次、纯文本会话零 warning**
- `test_content_validation.py` / `test_hitl_multimodal_validation.py` /
  `test_multimodal_entry.py` 中断言抛 `VisionNotSupportedError` 的用例 →
  改断言「落库成功 + 出网被降级」
- `test_openai_tool_image_relocation.py` 补纯文本 adapter 用例：断言**不**产生
  重定位的 user 消息
- 新增回归钉住核心承诺：纯文本 adapter 的会话里，图片仍进 memory、
  `media:get_image` 仍取得回——证明是「传递」不是「丢弃」
- 补 `_build_adapter` 对两个新 style 的分派用例
