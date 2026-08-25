# 多模态（multimodal）整体设计

日期：2026-08-20
状态：设计已评审，待实现
子设计：[图片折叠与回放](2026-08-20-image-fold-replay-design.md)

## 1. 目标

让所有能开启 agent loop 的接口都接受多模态消息（文本 + 图片），并让图片在
memory、事件、装配、出网、折叠、恢复的全链路上保持完整、可估算、可回收、可取回。

**本期范围**：图片（`ImagePart`）。用户侧输入方向。

**本期不做**（§9 详述）：工具返图、音视频、摘要内含图。

## 2. 现状判断

**类型层已经就绪，搬运层全部拍扁。**

`ContentPart = TextPart | ImagePart`（`protocols/context.py:35`）已贯穿三个关键契约：

- `MemoryEvent.content` / `MemoryRecord.content`：`str | list[ContentPart]`（`memory.py:153,212`）
- `LLMMessage.content`：`str | list[ContentPart]`（`llm.py:107`）
- `ContextBlock.content`：`str | list[Any]`（`assembler.py:112`）

但每一个真正搬运数据的地方都无条件调用 `content_to_text`（`utils.py:134`，注释明写
"images skipped"）把它拍成字符串。所以工作量不在"加类型"，在"拆掉那一串拍扁点 +
补齐持久化序列化"。

### 2.1 数据流与拍扁点

```
host
 └─ SessionStartParams.user_prompt: str                    ← 入口①（runtime.py:302）
    ├─ run_single_task(user_prompt: str)                   ← 入口②（runtime.py:618）
    ├─ HitlManager.answer(text) / reject(message)          ← 入口③（hitl_manager.py:180,193）
    └─ TaskManager.reopen_task(new_prompt)                 ← 入口④（task_manager.py:498）
         ↓
SessionManager.create_session / resume_session（session_manager.py:35,96）
 ├─ Session.user_prompt / Task.user_prompt
 └─ SESSION_CREATED / SESSION_RESUMED / TASK_CREATED payload   ★ 事件持久化，dataclass 不可 json
         ↓
StepDriver.run → _persist_user_prompt（driver.py:167）          ★★ 拍扁：图第一次消失
         ↓  MemoryEvent(content=text) → memory provider
Assembler
 ├─ AgentRecallSource（agent_recall.py:94）                      ★★ 拍扁
 ├─ record_to_history_block（_history.py:67）                    ★★ 拍扁
 │    └─ token_estimate = token_counter(text)（_history.py:111）  ★ 图算 0 token
 ├─ TaskSpec / Knowledge / LongMemory sources                    ○ 拍扁是**正确**的（检索 query）
 ├─ budget.py 裁剪                                               ○ 按 metadata 判，不碰内容
 └─ Composer
      ├─ _history_to_messages_with_sources（composer.py:889）     ★★ 拍扁
      ├─ 当前消息框重建（composer.py:571-616）                     ★★ 拍扁后重建为纯 str
      ├─ 三个拼接器（composer.py:816,829,854）                     ★★ 拍扁
      └─ token 计数（composer.py:400）                            ★ 图算 0 token
         ↓
llm_gateway.py                                                   ✓ 已 parts-aware
         ↓
providers/llm/anthropic.py:343,361,377 · openai.py:381,398,405   ★★ 出网前丢图
```

## 3. 设计原则

**① 单一归一层。** 所有内容形态转换收在一个模块里，其余地方只调它，不各自写
`isinstance`。这是把改动从"N 处散点"收成"1 个模块 + N 处替换"的关键。

**② 只允许两类地方拍扁。** 其余全链路保 `list[ContentPart]`：

- **语义检索 query** —— `knowledge.py:39`、`long_memory.py:34`、`task_spec.py:35`
- **摘要输入** —— `compact.py:126,205`、`finalize.py:415`、`background_observe.py:100`、
  `recognize_intent.py:127`

  > **勘误（评审 2026-08-23 fix wave，I3）**：这四处读的是 **memory 记录**（用于生成
  > 摘要文本 / token 估算），**不在 LLM prompt 路径上**。Phase 2 实测（探针驱动
  > `act` / `compact` / `observe` / `recognize_intent` / `background_observe` 五个
  > compose purpose）确认：**真正发给 LLM 的 prompt 由 composer 构建，五个 purpose
  > 现在全部携带 inline base64 parts**，包括本条意在覆盖的 compact/recognize_intent/
  > background_observe 三个 facet purpose 自身的 prompt——这条拍扁点没有覆盖到它本想
  > 覆盖的路径。后果与准入条件见 §13。

**③ ref 最晚 rehydrate。** core 全程只见 blob ref，只有 LLM adapter 拼 wire payload
时才换回 base64。memory / 事件 / 装配链搬的都是几十字节字符串。

## 4. 内容模型与归一层

### 4.1 `ImagePart` 扩一个 source_type

```python
@dataclass
class ImagePart:
    data: str            # base64 / URL / blob ref，按 source_type 解释
    media_type: str
    source_type: Literal["base64", "url", "ref"] = "base64"
    type: Literal["image"] = "image"
```

只扩取值，不加字段（`protocols/context.py:26` 改一行）。`ContentPart` 联合类型与
所有下游签名不变。

### 4.2 `core/content.py`

```python
def normalize_content(x) -> str | list[ContentPart]
    """归一入口内容。base64 图片经 BlobStore 外部化成 source_type="ref"。"""

def content_to_text(x) -> str
    """保留在 utils.py:134 原位，语义不变，由本模块 re-export。
    与同在 utils 的 estimate_content_tokens 是一对，不拆开。"""

def content_with_prefix(content, text) -> str | list[ContentPart]
def content_with_suffix(content, text) -> str | list[ContentPart]
    """保 parts 的拼接：str 走原逻辑，list 则拼进首/尾个 TextPart（没有就新插一个）。"""

def content_to_jsonable(x) -> str | list[dict]
def content_from_jsonable(x) -> str | list[ContentPart]
    """事件 / 投影的规范 JSON 形态。ContentPart 是普通 dataclass，json.dumps 会直接炸。"""

def redact_content_for_event(x) -> str
    """把 ImagePart 渲染成 [image image/png ref:ab12…]，供事件 payload 用。"""
```

`estimate_content_tokens` 与图片 token 口径**留在 `utils.py:168`**（评审决定）。

## 5. Blob 存储

### 5.1 协议：并入 `protocols/filesystem.py`

与 `SpillSink`（`filesystem.py:34`）同处。两者形状一致——"core 不直接碰存储，只知道
有个 sink"——且宿主侧实现落点相同（`FilesystemToolsProvider` 已持有 per-session
workspace）。

```python
BLOB_REF_PREFIX = "blob:"

class BlobStore(ABC):
    """core 的二进制 sink 契约。

    put 必须内容寻址且幂等：同样的 data 返回同样的 ref，重复调用不重复存。
    get 对不存在 / 已回收的 ref 返回 None（**不得 raise**）——调用方据此降级为
    文本占位，绝不因取图失败中断 loop。
    """
    @abstractmethod
    async def put(self, data: bytes, media_type: str, ctx: ProviderContext) -> str: ...
    @abstractmethod
    async def get(self, ref: str, ctx: ProviderContext) -> tuple[bytes, str] | None: ...
```

注册走现有 `ProviderRegistry`（`runtime.py:187`），加一对
`register_blob_store` / `get_blob_store`。

### 5.2 内容寻址 + side index

blob 本体按 `sha256` 存；另存 `(tenant_id, session_id, sha)` 索引用于 GC。

选内容寻址而非作用域 key，是因为它同时给到三件事：写入端去重（同一张图反复引用只存
一份）、幂等（重放安全）、以及**rehydrate 字节稳定**——同一 ref 每次还原出的 base64
完全一致，Anthropic 的 prompt cache 前缀不会被打碎。带签名的临时 URL 做不到这点。

GC 挂现有 session 生命周期钩子（`SessionScopedCapabilityProvider.deregister_session`，
`runtime.py:697` 的 finally 块）。跨 session 继承（`_copy_memory_for_inherit:100`）只
复制 ref、不复制本体，仅补一行索引。

### 5.3 `NullBlobStore`

未注册时的默认实现：`put` 原样退回 base64（`source_type` 保持 `"base64"`），`get`
返回 `None`。**保证不接 blob store 的宿主行为完全不变**，也让 §10 的分阶段推进成立。

## 6. 分层改动

### 6.1 入口签名放宽（`str` → `str | list[ContentPart]`）

| 位置 | 备注 |
|---|---|
| `SessionStartParams.user_prompt` + `.create()`（`runtime.py:302,313`） | |
| `run_single_task(user_prompt=)`（`runtime.py:618`） | 内部 `description=user_prompt[:200]` 会在 list 上切片出错，改走 `content_to_text` |
| `SessionManager.create_session / resume_session / _make_root_task_manager`（`session_manager.py:35,96,155`） | |
| `HitlManager.answer(text) / reject(message) / resolve_*` + `HitlRequest.message`（`models.py:308`） | |
| `TaskManager.reopen_task` 的 `new_prompt` / `original_user_prompt`（`task_manager.py:520-550`） | `line 522` 现为 `if isinstance(str) else ""`，多模态直接丢空 |

**`Session.user_prompt` 保持 `str`**（`models.py:141`）：只存 `content_to_text` 摘要，
全量只放 `Task.user_prompt`，避免两处真源。

**LLM 产出的 prompt 不改**：`control_capability` 的 `create_task` / `dispatch` 参数
本就是模型写的文本。

**入口只做格式校验，不做 token 准入**：校验 `media_type` 白名单、base64 合法性、
单图字节上限（防畸形/恶意输入）。**不**校验 token 总量——单条消息塞太多图的情形由
装配期 `ContextOverflowError` 兜底，理由见子设计 §3。

> **已兑现（Task 3，Phase 3a，2026-08-24）**：`validate_content`（`content.py:196`）
> 落地上述三项格式校验——`media_type` 白名单（`ALLOWED_IMAGE_MEDIA_TYPES`，Anthropic
> 与 OpenAI 都接受的交集）、base64 合法性（`base64.b64decode(..., validate=True)`）、
> 单图字节上限 `_MAX_IMAGE_BYTES = 5 * 1024 * 1024`（Anthropic 单图约 5MB 上限）；
> 并在含图时接上 §6.7 的视觉能力门控。接在 `start_session`
> （`runtime.py:302` 附近）与 `run_single_task`（`runtime.py:665` 附近）两个入口，
> **入口即拒、不落库**——校验失败不产生任何 Session/Task/事件记录，测试见
> `tests/unit/test_content_validation.py::
> test_run_single_task_rejects_image_before_persisting_anything`。
>
> **刻意不做 token 总量准入**，理由有二：其一如上文所述，超预算由装配期
> `ContextOverflowError` 兜底，入口层重复判定只会引入两套阈值互相打架的风险；
> 其二是纯文本路径的不变量——`start_session` 在改造前从不同步解析 LLM
> （解析推迟到任务真正执行时才异步发生），若为了做 token 准入而提前调用
> `_resolve_llm`，会把"LLM 解析失败"从"任务执行时才失败"变成
> "`start_session` 里同步失败"，这是本 Phase 明令禁止的行为变化（见 §13 新增条目）。
> `validate_content` 因此只在**内容含图**时才需要 `llm` 参数（`content_has_image`
> 判定，`content.py:47`），纯文本路径完全不触碰 LLM 解析。

### 6.2 持久化：事件与投影

全部走 `content_to_jsonable` / `content_from_jsonable`。

| 位置 | |
|---|---|
| `SESSION_CREATED` / `SESSION_RESUMED` payload | `session_manager.py:74,142` |
| `TASK_CREATED` / `TASK_REQUEUED` payload | `task_manager.py:1167,563` |
| HITL `payload["message"]` | `hitl_manager.py:343` |
| 投影字段类型 | `control/types.py:20,47,48` |
| 反序列化 | `reducers.py:231,257,409,427,485,508-513` + `110-117`（HITL） |
| 投影 → 运行时模型 | `converters.py:25,54,55` |

不改这一组，重启后图必丢。

### 6.3 落库路径

- **`driver.py:167-186 _persist_user_prompt`** —— 去掉 `content_to_text`，原样传
  `task.user_prompt`。这是图第一次消失的地方，最关键的一处。
- `runtime.py:1402 _inject_user_reply` —— `f"Human declined: {req.message}"` 与
  `interrupt_edit_note(prev, content)` 两处拼接改用 `content_with_prefix`。
- `runtime.py:1460 _last_user_prompt` —— `ups[-1].content or ""` 可能已是 list，
  显式 `content_to_text`。
- `protocols/memory.py` 契约文档写明：宿主 memory provider 必须能持久化
  `list[ContentPart]`（本仓 `in_memory.py:384` 只是原样持引用）。

### 6.4 装配链保 parts

- `_history.py:67` `record_to_history_block` —— 保 parts；其中
  `wrap_compact_summary` / `PROGRESS_SO_FAR_HEADING` / `annotate_assistant_summary`
  三处拼接改 `content_with_prefix/suffix`
- `agent_recall.py:94` —— 同上
- `composer.py:889` `_history_to_messages_with_sources` —— 保 parts；注意
  `if not content and not tool_calls: continue` 的空判断要 parts-aware，否则
  **纯图消息会被当空消息丢弃**
- `composer.py:571-616` 当前消息框 —— 现在是拍扁后重建纯 str 消息，改成
  "文本前缀 + 保留原 parts"
- `composer.py:816,829,854` 三个拼接器 —— 换 `content_with_*`

**不改**：`task_spec.py:35`、`knowledge.py:39`、`long_memory.py:34`（原则②的
检索 query 例外）。

### 6.5 token 估算（图片感知）

**不能直接改用 `estimate_content_tokens`。** 它在 `utils.py:176` 无条件加
`_MSG_FRAMING_TOKENS = 4`，对纯文本不是恒等变换：五个改造点会各自凭空多 4 token/条，
其中 `is_short_segment` 与 `_is_short_leaf` 现在是「整段拼成一个字符串数一次」，
换过去变成 4×N，短段阈值与 short task 判定会实质漂移；`_active_memory_tokens` 同理会
移动 compact 的升级阈值。

改为在 `utils.py` 新增一个只补图片、不碰文本与 framing 的函数：

```python
def image_tokens(content: "str | list[ContentPart] | None") -> int:
    """content 中图片 part 的 token 补偿（不含文本、不含 framing）。"""
    if not content or isinstance(content, str):
        return 0
    return _IMAGE_PART_TOKENS * sum(1 for p in content if not hasattr(p, "text"))
```

五处一律改成 `既有的文本计数 + image_tokens(content)`，**纯文本逐字节恒等**。
`estimate_content_tokens` 重构成复用 `image_tokens`，使 `_IMAGE_PART_TOKENS` 保持
单一真源（`prepare.py:74,95` / `llm_gateway.py:281` 的既有行为不变）。

五处及其不改的后果：

| 位置 | 不改的后果 |
|---|---|
| `_history.py:111` | `token_estimate` 为 0 → budget 认为没超、一条不裁 → 整份 prompt 打到 provider 400 |
| `composer.py:400` | 装配期总量失真 |
| `compact.py:205` `_active_memory_tokens` | 折叠层级测出 `freed_tokens == 0`，编排误判 |
| `background_observe.py:99` `is_short_segment` | 图片密集段误判"短段免折" → 该段 raw 永久保留 |
| `finalize.py:415` `_is_short_leaf` | 图片密集 task 误判 short |

这五处不依赖本设计其余部分，可以独立先行（见 §10 Phase 0）。

**准确地说它们是「潜伏的」而非「已在发作的」bug**：当前没有任何入口能让 `ImagePart`
进入 memory，因此五处补充项在 Phase 0 内恒为 0。Phase 0 的意义是**先把地基修对**——
它的测试以合成记录验证补充项算得准，等 Phase 1-2 打通输入后，五处同时转为生效，
无需再回头改。

其中 `composer.py:400` 一处还额外依赖 §6.4 令 composer 保 parts：**Phase 2 的计划须
显式验证该项确实从恒 0 转为生效**，否则它会永久是死代码。

> **已兑现（Task 7，2026-08-23）**：`tests/integration/test_multimodal_end_to_end.py::
> test_assembled_token_count_higher_with_image_than_text_only` 驱动完整
> `start_session` → PrepareStep → `ContextAssembler.assemble` → `DefaultComposer.compose`
> 全链路，从生产事件 `CONTEXT_ASSEMBLED.token_count` 取真实值，断言含图会话
> 严格大于同等文本会话（差值 = `_IMAGE_PART_TOKENS`）。变异验证：把
> `composer.py:420` 的 `image_tokens(m.content)` 项改成常数 0，该测试即失败，
> 证明其确有拦截力。

`_history.py:111` 一处还有一条独立的数据源错位：Task 2 的 `token_estimate` 用
`record.content`（未拍扁）算图片项，而 `budget.py` 只能读 `ContextBlock.content`。
`_history.py` 目前设 `content=text`（已拍扁），因此 `budget.py` 的图片计数**按构造恒为
0**——不只是「暂时没有图片」，而是即使 Phase 1 把真实 `ImagePart` 写入 memory 也依然
为 0，直到 `record_to_history_block` 本身停止拍扁。Phase 2 须一并对齐这两处的数据源。

> **已兑现（Task 7，2026-08-23）**：`tests/unit/test_budget_strategy.py::
> test_overflow_image_count_from_real_assembled_history_block` 不手工构造
> `ContextBlock`——用 `record_to_history_block`（Task 2 之后的真实装配产物）
> 产出含 3 个 `ImagePart` 的 block，喂给 `PriorityBudgetStrategy.apply` 驱动溢出路径，
> 断言 `ContextOverflowError.image_count == 3`，证明装配链真的把 parts 送到了
> `budget.py:89` 的 `image_part_count(b.content)`。变异验证：把
> `utils.py` 的 `image_part_count` 改成恒返回 0，该测试即失败。

`ContextOverflowError`（`errors.py:76`）默认文案补图片维度，让用户知道该删图而非删字。

### 6.6 LLM 出网

- `providers/llm/anthropic.py:343,361,377` —— `_parts_to_text` 换成真 blocks：
  `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`；
  `tool_result.content` 同样支持 blocks
- `providers/llm/openai.py:381,398,405` —— 换
  `[{"type":"text",…},{"type":"image_url","image_url":{"url":"data:…"}}]`
- `providers/llm/mock.py` 跟进，否则测试全挂
- `source_type=="ref"` 在此处 rehydrate（原则③）；`blob.get()` 返 `None` 时降级成
  `{"type":"text","text":"[image unavailable]"}`，**不抛**
- `llm_gateway.py` 不改：`_is_empty_content:125` / `_merge_message_content:172` /
  估算 `:281` 已 parts-aware

#### provider 不对称由 adapter 吸收

Anthropic 的 `tool_result` 支持 image block；OpenAI chat completions 的
`role="tool"` 消息**只接受文本**。

core 侧统一表达为 `LLMMessage(role="tool", content=[TextPart, ImagePart])`，差异全部
关在 adapter 内：

- **Anthropic**（`anthropic.py:356-375`）：`tool_result.content` 从字符串改为 block
  列表，原生承载图片
- **OpenAI**（`openai.py:376-402`）：tool 消息只发文本，**在该组连续 tool 消息之后
  追加一条 `user` 消息**承载图片。需把现有的 `for m in messages` 改成与 anthropic
  同构的 `while` 分组循环，才能定位组尾——同批多个 tool call 时，追加的消息必须在
  整组之后，夹在中间会触发 `insufficient tool messages following tool_calls message`

**这条 user 消息只存在于 wire payload，不落 memory。** 落库会制造出一个假的段边界
（`segment_fold.py:47`、`finalize.py:466`、`background_observe.py:87` 三处都用
「最后一条 role=user 回合」划段），打乱段折叠与胶囊范围。

与 `reorder_tool_results_after_calls`（`llm_gateway.py:198`）无冲突：那一步在 core
执行，adapter 在其之后，追加的消息不会再被搬动。

> **未兑现（评审 2026-08-23 fix wave，I2）**：上面这条 OpenAI 重定位**Phase 2 没有实现**。
> `openai.py:372-413` 的 `_serialize_messages` 保留了原有的 `for m in messages` 循环，
> 没有改成与 Anthropic 同构的 `while` 分组循环去定位 tool 消息组的组尾。实际行为是：
> `role="tool"` 的消息经 `_parts_to_text` 拍扁成纯文本发送（见本文件同段），其中的
> **非文本 part（图片）被静默丢弃，不产出任何占位符**——既不报错也不提示，图片就是
> 消失了。
>
> Phase 2 里 tool result 恒为文本（没有 capability provider 会通过 tool result 返图），
> 所以今日无实际损失。但 Phase 4 实现 `media:get_image` 后，`InvocationResult.content`
> 会真的携带 `ImagePart`（见 §9），届时 tool 消息就会带图——**在着手 Phase 4 之前必须
> 补上这条重定位**，否则 `media:get_image` 取回的图片会在 OpenAI 侧原样丢失。
>
> 顺带记录 `_parts_to_text`（`openai.py:416` 与 `anthropic.py:390` 各一份、逻辑同构）
> 现在的不变式，Phase 4 补重定位时会依赖它：**非文本 part 一律跳过，不产出占位符**——
> 调用方（当前是 `role="tool"` 的拍扁路径）若需要占位符（如「[image unavailable]」
> 或「[见下条工具结果]」之类的用户可见提示），需在调用方自己加，`_parts_to_text`
> 本身不做。

> **Phase 2 端到端出网验证（Task 7，2026-08-23）**：
> `tests/integration/test_multimodal_end_to_end.py::
> test_multimodal_prompt_reaches_wire_payload_as_image_block` 驱动
> `start_session(user_prompt=[TextPart, ImagePart])` 走完一个 actor 回合，用一个
> 子类化 `AnthropicAdapter`（复用真实 `_build_payload`/`_serialize_messages`，只是
> 不做真实网络请求）捕获实际 wire payload，断言其中含 `{"type": "image", ...}`
> block——证明图片真的从 memory 一路打到出网 payload，而不只是停在
> `AssembledPrompt.messages` 里。变异验证：把 `anthropic.py` 里 `_parts_to_blocks`
> 的图片分支改成退化成文本，该测试与 `test_adapter_multimodal_wire.py` 的两条
> 既有测试同时失败。

### 6.7 模型能力门控

`session.llm_model` 不支持视觉时，入口拒绝或降级成文本占位，不能让请求打到 provider
才 400。能力信息取自 `LLMClientResolver` 解析出的 client（`runtime.py:471 _resolve_llm`）。

> **已兑现（Task 2/3，Phase 3a，2026-08-24）**：能力字段落在
> `ModelConfig.supports_vision: bool = False`（`providers/llm/provider.py`）——
> **per-model 而非 per-provider**：同一账号下 `gpt-4o` 支持视觉而 `gpt-3.5-turbo`
> 不支持，声明必须挂在单个模型配置上，挂在 adapter/provider 级会让该账号下所有
> 模型被一并放行。**严格默认 `False` 是破坏性变更**——未显式配置
> `supports_vision=True` 的模型一律被视为无视觉能力，宁可入口报错也不让
> image block 打到纯文本模型后被 provider 400（`errors.py:136`）。
>
> `LLMProvider.get_client`（`provider.py:254-257`）把 `model_cfg.supports_vision`
> 透传给 `_FixedModelClient`，后者以 `supports_vision` property 暴露
> （`provider.py:104-105`）。core 侧统一约定 `getattr(llm, "supports_vision", False)`
> 读取（`content.py:216`、`protocols/llm.py:263-266`）——duck-typed、非协议必需字段，
> 缺省地对旧 client（没有该属性）也拿到严格默认 `False`。
>
> 门控接在 `validate_content`（`content.py:196`）里，`start_session` 与
> `run_single_task` 两个入口调用（见 §6.1 已兑现说明）。
>
> **本仓两家 adapter（`AnthropicAdapter` / `OpenAIAdapter`）不声明 `supports_vision`**——
> 判断依据见 Task 4 报告：本仓的真实解析路径（`LLMProvider.get_client` →
> `_FixedModelClient`）已从 `ModelConfig` 透传该字段，adapter 本身从不被 core 直接
> 持有。但 `_resolve_llm`（`runtime.py:501-514`）绕过 `_FixedModelClient` 拿到裸
> `LLMClient` 的路径**截至本 Phase 审计到两条**（措辞留白：不排除今后出现第三条），
> 均不是本仓 adapter 需要为此改动的理由：
>
> 1. `CtxWeftRuntime.__init__` 的 `llm=` 兜底参数（`runtime.py:433`，注释明写
>    "fallback for backward compat / tests"）——`_resolve_llm` 在未注册 `LLMProvider`
>    时原样返回它。
> 2. **自定义 `LLMClientResolver`**（`protocols/llm.py:298-310`，一个 `Protocol`）经
>    `register_llm_provider`（`runtime.py:288`）注册——这是本 SDK 的**主要多账号扩展
>    点**，接受任何实现了 `get_client(account, model) -> LLMClient` 的对象，*不*要求
>    经过 `_FixedModelClient`。本仓 `LLMProvider.get_client` 只是这个 Protocol 的一种
>    实现；第三方宿主注册自己的 resolver、其 `get_client()` 直接返回裸 adapter，是
>    预期用法而非边缘情况，比 llm= 兜底更容易在实际集成中出现。
>
> **两条路径都 fail-closed**：裸 adapter 没有 `supports_vision` 属性，
> `getattr(llm, "supports_vision", False)`（`content.py:216`）取到严格默认 `False`，
> 图片照样被拒——这正是「未显式配置视觉能力就整体拒绝」的预期行为，不是漏洞。
>
> 两条路径都没有 per-model 配置（`context_limit` 等窗口参数同样由调用方在自己的
> `get_client()` 实现里决定，不是本仓 `ModelConfig` 管），所以走这两条路径、且需要
> 开放视觉能力的宿主，应在自己返回的 client 对象上按需声明该 duck-typed
> `supports_vision` 属性（自定义 resolver 里包一层最直接），而不是让本仓 adapter 类
> 硬编码 `supports_vision = True`——那会让该 provider 的所有模型（含纯文本模型）在
> **本仓的主路径**上也被一并放行，绕过 per-model 的严格默认，与本节开头的破坏性
> 默认精神相悖。

### 6.8 可观测性脱敏

- `act.py:221` 与 `observe.py:121` 的 `str(m.content)` 会把整块 base64 dump 进
  `LLM_PROMPT_SENT` payload → 改 `redact_content_for_event`
- `recognize_intent.py:127` 非 str 直接 `else ""`，整条消息内容丢失 → 改
  `content_to_text`

外部化成 ref 后，`ImagePart` 的 `data` 只有几十字节，`content_to_jsonable` 直接进
事件 payload 即可，不需为 base64 单设截断。

### 6.9 折叠与回放

见子设计 [图片折叠与回放](2026-08-20-image-fold-replay-design.md)。摘要如下：

- compact 新增 **L0.5**（插在 L1 之前）：把 `ImagePart` 就地降级成文本占位，
  无 LLM、可逆、单位收益最高
- 模型经 `media:get_image(ref)` 取回，**图随 tool result 回到对话尾部**，附带
  「原本属于第几条用户消息」的位置信息
- 尾部追加是 append-only，**KV cache 前缀不动**。这是否决「装配期在历史原位还原」
  的决定性理由——原位还原会使 cache 前缀从该记录起全部失效，而被折的图往往位置很靠前
- 取回结果落在普通 `TOOL_RESULT` 记录里：跨重启天然成立、下一轮装配天然包含、
  段边界到来时自动被折走。**不需要任何额外状态或回收机制**
- `InvocationResult.content` 随之放宽为 `str | list[ContentPart]`（见 §9）
- 新模块 `core/media/`，占位格式 / blob ref 交互 / 位置描述全在墙内

## 7. 模块归属总表

| 关注点 | 归属 |
|---|---|
| `ContentPart` 类型 | `protocols/context.py` |
| 内容形态转换、拼接、序列化、脱敏 | `core/content.py` |
| 图片 token 口径 | `utils.py` |
| `BlobStore` 协议 | `protocols/filesystem.py`（与 `SpillSink` 同处） |
| 图片折叠 / 占位 / 取回 / 还原 | `core/media/` |
| wire 格式转换与 rehydrate | `providers/llm/*` |
| blob 本体存储与 GC | 宿主（`FilesystemToolsProvider` 或对象存储） |

## 8. 摘要恒为纯文本

明确约定：`MemoryKind.SUMMARY` 的 content **恒为 `str`**。

这条约束让 `_history.py:67` 那一堆 summary 包装逻辑（`wrap_compact_summary` /
`PROGRESS_SO_FAR_HEADING` / `annotate_assistant_summary`）完全不必改成 parts-aware，
省掉一大块改造面。代价是摘要里的图只能以 ref 占位的文本形式存在——与子设计 §6 的
"两段衰减"一致。

## 9. 不在本期范围

- **通用的工具返图**（浏览器截图、图表生成）：Phase 4 为 `media:get_image` 打开了
  接缝——`InvocationResult.content` 放宽为 `str | list[ContentPart]`，非文本部分经
  `metadata["content_parts"]` 由 provider 贡献，§6.6 的 adapter 改造对任意工具通用。
  **机制到位，但本期只有 `media:get_image` 使用**；让第三方 capability provider 用上
  它，需要额外的协议文档、大小限流与授权审查，独立立项。
- **音频 / 视频**：`ContentPart` 联合类型可扩，但 token 口径、折叠策略、provider
  支持面都是另一套问题。
- **摘要内含图**：见 §8，本期明确排除。

## 10. 阶段划分

依赖顺序如下，每阶段可独立发布：

**Phase 0 — token 估算修复**（§6.5）
五处换 `estimate_content_tokens`。修的是既有潜伏 bug，不依赖其余任何部分，
且是后续所有预算判断的地基。

**Phase 1 — 内容骨架**（§4、§5.1、§5.3、§6.1、§6.2）
`core/content.py` + `BlobStore` 协议 + `NullBlobStore` + 入口签名 + 事件/投影序列化。
此阶段结束时多模态内容能进能出、能持久化能重启，但还到不了模型。

**Phase 2 — 端到端打通**（§6.3、§6.4、§6.6、§6.8）
落库保 parts + 装配链保 parts + adapter wire 转换 + 事件脱敏。
**此阶段结束即多模态可用**（以 inline base64 形态，未外部化）。

**Phase 3 — 外部化**（§5.2、§6.7）
真 `BlobStore` 实现 + 入口 base64 → ref + adapter rehydrate + 能力门控。
此阶段把内存与事件体积降下来，是 Phase 4 的前提。

**Phase 4 — 折叠与回放**（子设计全文）
`core/media/` + L0.5 + `media:get_image`。

## 11. 回归保证

每个阶段都必须满足：**纯文本会话的行为逐字节不变。**

具体到实现上：

- 所有 `content_with_*` 在 `str` 输入时走原字符串拼接路径，产物与改造前一致
- `NullBlobStore` 未注册时，`normalize_content` 对纯文本是恒等变换
- 五个估算点对纯文本与改造前**逐字节同值**（`image_tokens` 对 `str` 输入恒返 0，
  见 §6.5；这是不能直接套用 `estimate_content_tokens` 的原因——它带 4 token 的
  framing 补偿，会静默移动裁剪与 compact 阈值）
- 未注册 `BlobStore` 时，L0.5 返回 0（不降级），adapter 侧无 ref 可 rehydrate，
  图保持 inline base64——即 Phase 2 的形态

## 12. 测试策略

- **归一层**：`content_with_*` / `*_jsonable` / `redact_*` 的往返与纯文本恒等
- **入口**：四个入口各自接受 `list[ContentPart]` 并落到 `Task.user_prompt`
- **持久化**：写事件 → `rebuild_view` → 图完整还原（跨重启核心保证）
- **装配**：纯图消息不被当空消息丢弃；当前消息框保留 parts；检索 query 仍是文本
- **出网**：两家 adapter 的 blocks 形态；`blob.get()` 返 `None` 时降级不抛
- **估算**：图片计入 token；budget 能因图片触发裁剪；`ContextOverflowError` 文案含图片数
- **回归**：全量既有测试在每个 Phase 后保持绿

## 13. 未决项

**Phase 3 的强制前置（Phase 2 终审后裁定，2026-08-24）：**

- **纯空白文本块仍会出网。** Phase 2 的 I1 修复只跳过了 falsy 文本（`if text:`），
  而 `TextPart("   ")` 是 truthy——它挨着 `ImagePart` 时仍产出
  `{"type":"text","text":"   "}`。Anthropic 对空/**纯空白**文本块同样返回 400。
  gateway 的 `_is_empty_content` 拦不住（图片确是内容，消息被正确保留）。
  实测确认。修法：两家 adapter 的跳过条件改为 `if text.strip():`。
  安全性：只含空白 `TextPart` 而无图的消息在上游已被 `_is_empty_content`（它 strip）
  丢弃，故不会产出空 block 列表。**Phase 3 的第一件事。**
- **`anthropic.py` assistant 分支的注释已失效。** 其陈述的「`_parts_to_blocks` 对每个
  part 恰好产出一个 block」在 I1 修复后不再成立（falsy 文本 part 会被跳过）。
  代码本身仍正确（`content_blocks or ""` 的兜底行为不变，因为 content_blocks 为空
  仍蕴含消息为空），但注释会误导后来者——它正是 Task 5 删除死代码时所依据的不变式。
  Phase 3 顺带更正。

> **已兑现（Task 1，Phase 3a，2026-08-24）**：上述两条均已修复。
> 跳过条件改为 `if text.strip():`（两家 adapter，共四处：`anthropic.py` 的
> user/assistant/tool_result 三处 + `openai.py` 的对应处），纯空白 `TextPart`
> 挨着 `ImagePart` 时不再产出空白文本 block。测试：
> `tests/unit/test_adapter_multimodal_wire.py::
> test_anthropic_whitespace_text_part_next_to_image_dropped` 与
> `test_openai_whitespace_text_part_next_to_image_dropped`（两家 adapter 各一条同构
> 用例）。`anthropic.py` assistant 分支的失效注释已同步更正为准确描述
> `_parts_to_blocks` 会跳过纯空白文本 part 的行为。

- 单图字节上限与 `media_type` 白名单的具体取值（§6.1）
- 视觉能力信息从 `LLMClient` 的哪个字段读（§6.7）——现有 duck-type 约定里
  没有对应字段，可能需要在 `protocols/llm.py` 补一个可选属性
- `_IMAGE_PART_TOKENS = 1600` 是否够保守（`utils.py:154`），需按真实计费校准
- `image_tokens` / `image_part_count` 以 `not hasattr(p, "text")` 判定非文本 part。若某个
  memory provider 把 content 作 JSON 往返后返回 `list[dict]`，则**每个** part（含文本）
  都会被计为图片。`content_to_text` 有对称的盲点（会把这类列表渲染成空串），故该失效
  模式是既有的；但方向变了——此前静默少算为 0，此后变为多算，会导致过度裁剪与虚假
  `ContextOverflowError`。今日潜伏（`in_memory.py` 存对象引用，`src/` 内无 JSON 往返），
  但 ctx-weft 是 SDK、memory 协议可插拔，第三方 provider 正是它出现的地方。**Phase 1
  须保证召回内容 rehydrate 成 `ContentPart` 对象**，或在归一层令判据 dict-aware。本
  Phase 不改判据。
- **dict-aware 分歧在 Phase 2 变宽了（评审 2026-08-23 fix wave，M2）**：两家 adapter 的
  `_parts_to_blocks` 现在是 dict-aware 的（`p.get("type") == "image"` 分支），而
  `core/utils.py` 的 `content_to_text` / `image_part_count` 仍是 dict-blind（只认
  `hasattr(p, "text")`，dict 形态的图片 part 会被误判成文本 part）。上面那条已经点出
  这个盲点是「既有的」，但 Phase 2 之前只有 adapter 与 utils 两边都 dict-blind，行为
  至少对称；现在 adapter 侧已经会正确处理 dict-shaped part、utils 侧仍不会，
  同一个 `ImagePart` 在 wire 转换与 token 估算两条路径上可能被区别对待。Phase 3/4
  触碰 `core/utils.py` 或引入会返回 dict 形态 part 的 memory provider 前，应一并
  收敛这个分歧（而不是照抄 adapter 的 dict-aware 写法只加一半）。
- **facet purpose 现已携带 inline base64（评审 2026-08-23 fix wave，I3）**：见 §3②
  勘误。三条后果，作为 Phase 3/4 的准入条件：
  - **(a) compaction 会重发所有图片。** compaction 恰在上下文超预算时触发，而它
    现在的摘要 prompt（由 composer 构建）会带着触发它的那些图片一起发出去——这与
    「compaction 是为了省 token」的目标相悖，且在图片本身就是超预算主因时可能
    无法收敛。Phase 3/4 落地前须决定：compaction 输入是否该在拍扁前先过 L0.5
    降级（§6.9），还是走独立的、真正拍扁图片的摘要输入路径。
  - **(b) 视觉能力门控未生效。** §6.7 的门控是 Phase 3 交付物，**当前没有任何东西
    阻止 image block 进入纯文本模型**的请求——纯文本模型收到 image block 大概率
    直接 400（或更差，静默忽略图片内容）。Phase 3 门控上线前，这是一个已知但
    未修复的失败模式，不是「设计遗漏」而是「按阶段划分排到了 Phase 3」，写在此处
    防止被当成新发现重复报告。
  - **(c) 字节上限无约束。** 现有 token 估算按 `_IMAGE_PART_TOKENS = 1600`/图算，
    20 张 5MB 图只算约 32k tokens、在多数 context budget 下能通过，但原始字节
    早已超过 provider 的单请求体积上限（Anthropic 约 32MB/请求）——请求会在
    provider 侧因体积被拒，而不是被 budget 提前拦下。Phase 3/4（尤其是引入真
    `BlobStore` 外部化、或允许更大 `media_type` 白名单）落地前，需要在 token
    估算之外补一条独立的字节预算，或在 §6.1 的单图字节上限之上再加一条
    「单请求总字节上限」。

**已知缺口（Task 4，Phase 3a，2026-08-24，刻意范围划定）：**

- **HITL 应答与 `reopen_task` 两个入口未接校验与门控。** §6.1 列出的四个入口
  （`SessionStartParams.user_prompt` / `run_single_task` / `HitlManager.answer/reject`
  / `TaskManager.reopen_task`）里，本 Phase 只在前两个（`start_session` /
  `run_single_task`）接了 `validate_content` 与视觉门控。`HitlManager.answer(text)` /
  `reject(message)` 与 `TaskManager.reopen_task(new_prompt)` 仍未调用
  `validate_content`——若这两个入口将来放宽为接受 `list[ContentPart]`
  （见 §6.1 表格，本 Phase 尚未实现该放宽，签名仍是 `str`），畸形/超限图片或
  纯文本模型收图会绕过入口校验直接进入 memory。这是刻意的范围划定，不是遗漏：
  Phase 3a 的目标是防 400 护栏，优先覆盖两个主入口；HITL/reopen 的校验接入
  留给后续 Phase。

- **`start_session` 的纯文本路径刻意不提前解析 LLM。** Task 3 fix round 2 的裁定：
  `validate_content` 需要 `llm` 参数才能做视觉门控，而获取 `llm` 需要调用
  `_resolve_llm`——但改造前 `start_session` 从不同步解析 LLM（解析完全推迟到任务
  真正执行时，由 `_make_task_runner` → `_SessionTaskRunner` 异步触发）。若为了给
  `validate_content` 传 `llm` 而在 `start_session` 里无条件调用 `_resolve_llm`，
  会把"没有可用 LLM"这件事从"任务执行时才失败"变成"`start_session` 里同步失败"，
  违反本 Phase 的硬约束「纯文本行为逐字节不变」——哪怕 `_resolve_llm` 本身是无副作用
  的纯查表也不行，因为可观察的失败时机变了。
  裁定：`start_session` 先用 `content_has_image`（`content.py:47`）判断内容是否
  含图，**只有含图时才调用 `_resolve_llm`** 传给 `validate_content` 做视觉门控；
  纯文本内容走 `validate_content(content)`（不传 `llm`，只做格式校验，天然
  no-op）。`run_single_task`（`runtime.py:648`）不受此约束——它改造前就已经
  无条件同步调用 `_resolve_llm`（`runtime.py:665`），所以本 Phase 直接在该处
  先行调用 `_resolve_llm` 并把 `llm` 传给 `validate_content`（`runtime.py:667-668`
  注释：「`_resolve_llm` 是纯查表，此处先行调用安全」），不需要 `content_has_image`
  分支——纯文本路径在这里本就已经解析 LLM，不存在"提前解析"的问题。
  这条裁定记在此处供 Phase 3b/4 参考：任何新入口
  校验若需要 `llm`，都必须先判断"是否真的需要它"，不能为了校验方便而破坏
  既有的惰性解析时机。测试：
  `tests/unit/test_content_validation.py::
  test_start_session_plain_text_does_not_eagerly_resolve_llm`。

**Phase 3a 防 400 护栏复审 fix wave（2026-08-25）新增缺口/裁定：**

- **工具产出的图片绕过全部三道护栏（I4，仅记录，不实现）。** §6.6 明文设计
  `LLMMessage(role="tool", content=[TextPart, ImagePart])`（Phase 4 的
  `media:get_image` 会产出这种形态）。但当前没有任何入口对工具结果调用
  `validate_content`——`start_session` / `run_single_task` 只校验的是**发起**请求的
  `user_prompt`，工具执行产出的 `LLMMessage(role="tool", ...)` 从工具 handler 直接
  进入消息序列，不经过这两个入口。也就是说：即便本 Phase 把 `user_prompt` 的图片
  护栏做得再严格，一个 text-only 模型仍可能在工具循环中间被喂 image block → 400，
  这正是本 Phase 要防的失效模式，只是从"用户输入"这扇门换到了"工具输出"这扇门。
  已记的缺口清单（本节上方，Task 4）目前只列了 HITL 与 `reopen_task`，未覆盖这条，
  在此补记。
  **门控该放在哪里，两个候选：**
  - 工具结果路径（工具 handler 产出 `LLMMessage(role="tool", ...)` 之后、拼入历史
    之前）——离"数据源头"最近，能在落库前就拒绝，但工具产出点分散（凡是能返回
    `ImagePart` 的 capability 都要接一遍），覆盖面随 capability 数量线性增长。
  - `stream_llm`（`core/loop/llm_gateway.py`，出网前最后一道关口）——是本模块
    docstring 定义的"发送前合法化"唯一关口，天然覆盖所有来源（用户输入、工具输出、
    历史回放）的图片，一处生效、不随 capability 数量增长；代价是校验发生得晚
    （消息已落库/已进入历史后才拒绝，不是"入口即拒、不落库"）。
  **倾向**：放在 `stream_llm`。理由：它已经是"发送前合法化"的唯一关口
  （`legalize_messages` 六条不变式全在这里做），新增"逐条 tool-result 图片过
  `validate_content` 视觉门控"是同一关口职责的自然延伸，且不需要在 Phase 4 每新增
  一个可能产图的 capability 时都记得补校验——这正是本 Phase 反复强调的"防 400 是
  唯一关口"架构原则（模块 docstring 开篇）在工具结果场景下的对应延伸。代价（校验晚、
  已落库）需要 Phase 4 落地时与"入口即拒不落库"的既有语义做取舍，留给到时决定。
  本条只记录判断倾向，不在本 fix wave 实现（超出 Phase 3a 范围，需要 Phase 4 的
  `media:get_image` 落地后才有真实调用点可测）。

- **dict 形态 part 被入口全数拒绝，且把纯文本拖进提前解析分支（I3，评审 2026-08-24
  fix wave，裁定 B）。** `content.py` 的 `_is_text_part`（`hasattr(part, "text")`）
  对 dict 形态 part 恒为 `False`——纯文本 dict `{"type":"text","text":"hello"}`
  会被 `content_has_image` 误判为"含图"，进而在 `start_session` 触发提前
  `_resolve_llm`（§13 上一条裁定要防的行为，从另一扇门回来了）；被 `validate_content`
  当成"无 `media_type` 的图片"直接 `InvalidContentError` 拒绝。两家 adapter
  （`anthropic.py` / `openai.py`）的 `_parts_to_blocks` 原生支持 dict 形态 part，
  但入口把它们全拒了——入口比 adapter 更严格，形成一个不对称的硬限制。
  评审给出两个选项：(A) 让归一层认识 dict（`_is_text_part` / `validate_content`
  都支持 Mapping 取值）；(B) 保持现状，钉住这个限制并更新 spec，要求宿主在
  ingest 前把 dict 形态 rehydrate 成 `ContentPart` 对象。**裁定：选 (B)。**
  理由：`_is_text_part` 的判据字面量（`hasattr(part, "text")`）与
  `core/utils.py` 的 `content_to_text` / `image_part_count` 共享——本节上方
  （评审 2026-08-23 fix wave，M2）已经点出两边一度存在 dict-aware 分歧、需要
  "一并收敛"而非"照抄一半"；`content_to_text` 还被
  `tests/unit/test_content_module.py::test_content_to_text_reexported` 钉死为
  `utils.content_to_text` 的同一个对象引用。若只在 `content.py` 本地给
  `_is_text_part` / `validate_content` 加 Mapping 支持，会立刻制造一个新的、
  比现状更难追踪的分歧：`content_has_image` / `validate_content` 认得
  dict-text，`content_to_text` / `image_part_count` 仍不认得，同一份 dict 内容
  在"是否含图判断"与"拍扁成文本"两条路径上给出不同答案。这比现状"两处对称地
  都不认识 dict"更危险，不满足"不改判据本身"的约束（哪怕只改
  `content.py` 一处，也会在两处判据之间制造语义分叉，等价于事实上改了判据的
  可观察行为）。故选 (B)：`_is_text_part` 判据字面量不变，只把这个已知限制
  钉成回归测试
  （`tests/unit/test_content_validation.py::
  test_dict_text_part_is_misclassified_as_image_known_limitation` /
  `test_dict_text_part_rejected_by_validate_content_known_limitation`），并在此
  记录：**入口目前只接受 dataclass 形态的 `ContentPart`（`TextPart`/
  `ImagePart`）**——dict-shaped part（含纯文本 dict）会被入口硬拒绝或误判，宿主
  必须在把内容喂给 `start_session`/`run_single_task` 之前，把从 JSON 往返/
  memory provider 召回的 dict 形态内容 rehydrate 成 `ContentPart` 对象
  （`content_from_jsonable` 即为此设计）。这把上面 M2 记录的"token 误计"级别的
  隐患，在入口这一层升级成了"硬拒绝"——两者同源（同一判据、同一盲点），只是
  暴露的层不同。真正收敛（选项 A，含 `core/utils.py` 一并改成 Mapping-aware）
  留给 Phase 3b/4，届时若要做，须同步改 `_is_text_part` / `content_to_text` /
  `image_part_count` 三处，不能只改一处。
