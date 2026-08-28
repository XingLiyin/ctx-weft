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

> **订正 + 已兑现（Phase 3b Task 3，2026-08-25，架构裁定 T0）**：上面这句
> 「**只有 LLM adapter 拼 wire payload 时才换回 base64**」**字面上做不到**，实现落点
> 与之偏离一层，特此订正（原文保留以存上下文）。
>
> 核实结论：adapter 的整条序列化链——`_build_payload` / `_serialize_messages` /
> `_parts_to_blocks`——**全是同步函数**，而 `BlobStore.get` 是 **async**，同步函数里
> 没法 await。
>
> **裁定：rehydrate 落在 `core/loop/llm_gateway.py` 的 `stream_llm`**——出网前最后一个
> async 关口、已在跑 `legalize_messages`，且**一处覆盖三家 adapter**（放 adapter 里要
> 写三遍）。落点在 `legalize_messages` 之后、`llm.complete` 之前。
>
> 这偏离 §3③ 的字面（gateway 属 core），但**保住了它的实质**：core 的 memory / 事件 /
> 装配链全程只见 ref，只有出网前那一瞬间内存里才存在 base64。
>
> **代价（已实测在生产路径上不成立）**：若将来出现绕过 gateway 直调 adapter 的路径，
> 那条路上的 ref 不会被还原。Task 5 的端到端测试从**真实 wire payload** 断言
> （`tests/integration/test_multimodal_end_to_end.py::`
> `test_ref_externalized_in_memory_but_full_base64_on_the_wire`），已确认本仓真实
> `start_session` 的两次 LLM 调用（`recognize_intent` + `act`）都经 `stream_llm`。
>
> **⚠️ 附带的静默损坏风险（Task 5 实测）**：`base64.b64decode("blob:<sha>")`
> **不抛异常**——默认 `validate=False` 会静默跳过非字母表字符，解出一串垃圾字节：
>
> ```
> base64.b64decode('blob:d4735e3a265e16ee')                -> b'nZ\x1bw\x8e\xf7\xe5\xed\xda\xdb\xae^\xd7\xa7\x9e'   # 垃圾字节，不报错
> base64.b64decode('blob:d4735e3a265e16ee', validate=True) -> binascii.Error
> ```
>
> 即：**ref 检测一旦失败，后果是「发出一堆垃圾图片字节」而不是报错**——静默损坏而非
> 响亮失败。两个直接推论，务必保留：
> 1. `_is_ref_part`（`core/content.py`）里那条「`data` 以 `blob:` 开头即判为 ref、
>    **无视**声明的 `source_type`」的兜底，是**防静默损坏的安全护栏**，不是整洁性
>    代码。该判据零假阳性：base64 字母表不含 `:`。
> 2. 任何端到端断言都**必须校验「解码回原始字节」**，不能用「不以 `blob:` 开头」这种
>    弱判据——后者杀不掉「还原出别的字节」这类损坏（Task 5 的 M2 变异体正是靠解码
>    断言才被抓住的）。

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

> **订正（Phase 3b Task 2）**：`NullBlobStore.put` 实际是 `raise NotImplementedError`，
> **不是**「原样退回 base64」——Phase 1 终审刻意如此，让接线错误立刻暴露，而不是静默
> 产出一个假 ref。「行为完全不变」这个结论仍然成立，但成立的机制是
> **调用方先探询 `can_externalize` 属性再决定是否 put**（见 §6.1 已兑现说明），
> 而不是靠 put 返回原值。

### 5.4 已兑现（Phase 3b Task 1，commit `490af28` + `2951d7c`）

`FilesystemToolsProvider`（`providers/capability_filesystem/provider.py`）新增第四个
契约实现 `BlobStore`（此前已实现 `ToolCapabilityProvider` / `SpillSink` /
`SessionScopedCapabilityProvider`）。

- **内容寻址**：`sha256(data).hexdigest()`，ref 形态 `blob:<sha>`。
- **幂等**：`_write_blob_if_absent` —— `content_path.exists()` 即直接返回，不重写。
- **落盘布局**：`<workspace>/blobs/<sha[:2]>/<sha[2:4]>/<sha>`，`media_type` 存在伴生
  文件 `<sha>.meta`（两级目录分片，避免单目录文件数爆炸）。
- **IO 走 `asyncio.to_thread`**，不阻塞事件循环。
- **`media_type` 冲突取「先写入者胜」**：`put` 本就是「已存在则跳过」的幂等写，
  `media_type` 沿用同一条规则最省心——不必新增分支决定「谁能覆盖谁」，也避免
  「同一份数据的 media_type 取决于调用顺序」这种难复现的行为。
- **`get` 恒不抛**（这条契约是 gateway rehydrate 的前提——rehydrate 在出网主路径上，
  抛异常会掀掉整个 LLM 请求）。三条返回 `None` 的分支：workspace 未登记 /
  ref 前缀不是 `blob:` / 内容文件不存在。`.meta` 缺失时 `media_type` 兜底为
  `application/octet-stream`。
- **安全发现（变异验证暴露）**：去掉「ref 前缀必须是 `blob:`」的检查后，
  `http://example.com/x.png` 会被当作 sha 去拼路径，**Windows 的 `pathlib` 把它解释成
  UNC 路径并真的发起了网络访问**（`WinError 64`）。这条前缀检查是**安全护栏**而非
  整洁性代码，已记入测试 docstring。
- 测试：`tests/unit/test_filesystem_blob_store.py`。

**GC / 生命周期：本 Phase 只做「不删」（用户裁定 D3）。** blob 追加写、永不回收，
清理责任留给宿主。§5.2 提到的 side index 与 `deregister_session` 挂钩**未实现**，
本 Phase 不做任何 GC / 引用计数 / TTL。

> **订正（Phase 3c Task C3，2026-08-27，commit `65dd606`）**：本节整段描述的
> `FilesystemBlobStore` **已按用户裁定 D5 移除**（全仓 `src/` 零引用），原文保留以存
> 上下文——它仍是理解「内容寻址 / 幂等 / `get` 恒不抛 / 前缀检查是安全护栏」这几条
> `BlobStore` 契约的完整样本，那些契约**一条未变**，只是换了实现。
>
> 现在的落点是 `SqlMemoryProvider` 的 `memory_blobs` / `memory_blob_refs` 两张表
> （裁定 D4：blob 并入 memory），引用边在 `ingest` 的**同一个事务**里写；GC 由
> `collect_blobs()` 做（延迟、幂等、**不在协议里、宿主须自己定时调**）。
> 「本 Phase 只做不删」这条**已不再成立**。**详见 §14.4。**

### 5.5 ⚠️ 宿主接线的隐式契约（Phase 3b Task 5 实测，最容易踩的一条）

**接了真 BlobStore 的宿主，必须显式传 `SessionStartParams.session_id`，并在调用
`start_session` 之前用 `register_session(session_id, workspace)` 预先登记 workspace。**

原因链：`FilesystemToolsProvider.put` 通过 `workspace_for(ctx)` 定位落盘根目录，
该映射由 `register_session` 建立；而 `session_id` 默认是在 `start_session` **内部**
生成的——宿主不显式传，就没有机会提前登记。

不这么做的后果：`put` 抛 `RuntimeError: no workspace registered for session ...`。
它是在**入口归一层**抛的，会让 `start_session` 整个失败。

这条契约此前**只在 `SessionStartParams` 的 docstring 里被暗示过**（「so the host can
pre-register session-scoped resources, e.g. the filesystem provider's workspace」），
没有任何文档把它和 BlobStore 串起来。Task 5 的端到端测试按简报字面写会直接
`RuntimeError`，正是踩到了这一条。**这是「距离真正接线还差什么」的直接答案之一。**

> **订正（Phase 3c Task C3）**：这条隐式契约来自 `FilesystemBlobStore` 的
> `workspace_for(ctx)`，**随该实现移除而不再适用**于 blob-in-memory 形态——
> `SqlMemoryProvider` 不需要 workspace。原文保留：它对仍在用
> `FilesystemToolsProvider` 的 `SpillSink` 路径依然成立，也是「实现专有的接线契约
> 会怎样咬人」的样本。**Phase 3c 之后的接线契约见 §14.4 与
> [宿主迁移清单](../../host-migration-to-sql-memory.md)。**

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

> **已兑现（Task 2，Phase 3b，2026-08-25，commit `5573272`）**：入口外部化落地为
> `core/content.py` 的 `normalize_content(content, *, blob_store, ctx)`——把
> `source_type == "base64"` 的 `ImagePart` 写进 BlobStore、换成
> `ImagePart(data="blob:<sha>", source_type="ref")`；文本 / `url` / 已是 `ref` 的 part
> 原样保留（不重复外部化）。**不改原对象**，返回新列表。
>
> **先探询 `can_externalize`，再决定——不 try/except（Phase 1 终审契约）。**
> `BlobStore.can_externalize` 是新增的只读属性，基类默认 `True`，只有 `NullBlobStore`
> 覆写为 `False`；`NullBlobStore.put` 的 `raise NotImplementedError` **保持不动**。
> 若改成「调用 put 再捕获 NotImplementedError」，会把「响亮失败」降级成控制流——真正的
> 接线错误（宿主注册了一个尚未实现 put 的 store）也会被静默吞掉。不能外部化时
> `normalize_content` **原样返回同一对象**（`is` 相同），这是「不接 BlobStore 逐字节
> 不变」这条硬约束的直接实现手段。
>
> **顺序：`validate_content` 严格先于 `normalize_content`。** 两个理由：
> (a) 被拒的内容不该在 blob store 里留下垃圾——校验失败必须发生在任何 `put` 之前；
> (b) `normalize_content` 里的 `base64.b64decode(..., validate=True)` **刻意不加
> try/except**——validate 先行已把畸形 base64 拦成 `InvalidContentError`，此处再包一层
> 只会造出一条永不执行、也永不被测试的分支；顺序若被后来者接反，这里抛出的
> `binascii.Error` 正好是响亮的信号（变异验证实证：反转顺序后
> `test_rejected_content_never_reaches_blob_store` 因 `binascii.Error` 转红）。
>
> 接线在两个入口：`start_session` 与 `run_single_task`，均为 **validate → normalize**。
> `start_session` 在**能外部化时**把 `session_id` 提前定下并透传给 `create_session`
> ——否则外部化所锚定的 session 与真正创建出来的 session 会是两个不同 id；不能外部化时
> 整段是 no-op（不提前生成 id、不替换 params），行为逐字节不变。
>
> **M5 遗留已兑现**：`validate_content` 对 `source_type == "ref"` 的处理从 Phase 3a 的
> 无差别 `raise` 改为——**跳过 base64 解码与单图尺寸校验**（那两项在 `put` 之前的那次
> validate 里已经把过关，且此时 `data` 是 `blob:<sha>` 而非 base64，再解码必然失败），
> 但**仍逐条校验 `media_type` 白名单与视觉门控**（ref 可能来自记录回放或宿主构造，
> 不能假定必然合法）。`url` 形态维持 `raise`（本 Phase 不支持）。
> 测试：`tests/unit/test_normalize_content.py`。
>
> **⚠️ 两层互相遮蔽的冗余守卫（已知，刻意保留）**：`start_session`
> （`runtime.py:781` 附近）外层有一个 `if blob_store.can_externalize:`，
> `normalize_content` 内部还有一个同样的守卫。`NullBlobStore` 时外层已经短路、
> 内层根本不被调用，因此**任一单独删除都不可观测**——Task 2 的变异 C（删外层）与
> Task 5 的变异 M3（删内层）双双存活，互为镜像实证，且已查明**不是测试薄弱**：
> 真正承重的语义由「改判据源头」的变异（`NullBlobStore.can_externalize → True`）覆盖，
> 那类变异会让端到端测试立刻转红。**裁定保留两层**（防御性、零成本）。
> 写在这里是为了防止后来者发现「删掉其中一个没有任何测试转红」而误以为它是死代码。

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

> **订正（Phase 3c Task D，2026-08-27，commit `1be2296`，用户裁定 D2）**：上面这个
> 「每张图恒 `_IMAGE_PART_TOKENS`」的函数体**已被推翻**，现行实现是逐 part
> `max(_IMAGE_PART_TOKENS, image_byte_size(part) // 128)`，体积未知时回落旧常数。
> 原文保留以存上下文（「不能直接改用 `estimate_content_tokens`」那条理由仍然成立、
> 仍是现行设计的一部分）。**系数 128 的完整推导、以及「它建模的是字节压力而不是
> 计费 token」这条要紧的界定，见 §14.3。**

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

> **订正（Phase 3b Task 3，2026-08-25）**：上面这组条目里有两条已被实现推翻，
> 原文保留以存上下文：
>
> 1. 「`source_type=="ref"` **在此处**（adapter）rehydrate」——**订正为：rehydrate 落在
>    `llm_gateway.stream_llm`**，理由见 §3③ 的架构裁定 T0（adapter 序列化链全是同步
>    函数，`BlobStore.get` 是 async）。降级语义不变且已兑现：`get()` 返回 `None` 时换成
>    文本 part `[image unavailable: <media_type>]`，**不抛**。
> 2. 「`llm_gateway.py` 不改」——**订正为：改了**。`stream_llm` 新增两个**带默认值**的
>    kwarg（`blob_store=None`、`provider_ctx=None`），在 `legalize_messages` 之后、
>    `llm.complete` 之前对每条消息 `dataclasses.replace(m, content=await
>    rehydrate_content(...))`。不传时整段不执行——既有调用方零影响。接线：
>    `LoopContext.blob_store`（默认 `None`）由 `runtime._build_loop_ctx` 从
>    `providers.get_blob_store()` 注入，两个调用方（`stream_llm_resilient` /
>    `recognize_intent`）用 `getattr(ctx, "blob_store", None)` 取值——既有测试里大量
>    ctx 桩不是真正的 `LoopContext`，没有这些字段。
>
> **两家 adapter 里「`source_type` 恒为 `base64`」的陈述同样已订正**（Task 3 已在
> `anthropic.py` / `openai.py` 的 `_parts_to_blocks` docstring 里改掉，此处同步）：
> 到达 `_parts_to_blocks` 时 `source_type` 确实恒为 `"base64"`，但这
> **不再是数据模型的固有属性，而是来自上游 gateway 的保证**——Phase 3b 起图片在入口
> 就被外部化成 ref，是 `stream_llm` 在出网前还原回 base64 的。
> **绕过 gateway 直调 adapter 的路径会丢掉这个保证**，那条路上的 ref 会被当 base64
> 写进 payload（且不会报错，见 §3③ 的 `b64decode` 静默损坏）。
>
> **`rehydrate_content` 的三条零开销短路**（都**原样返回同一对象**）：纯文本
> （`str` / `None` / 空）、`can_externalize` 为假的 store、内容里根本没有 ref。
> dict 形态的 part 也被正确处理（**dict 进 dict 出**）——`getattr(dict, "source_type",
> "base64")` 在 dict 上取不到属性、会落回默认值 `"base64"`，于是 dict 形态的 ref 会被
> 静默当 base64 塞进 wire payload。这是三种结局里最差的一种（不可观测的损坏 vs
> 可观测的降级），故 rehydrate 路径专用的字段读取器 `_part_field` 是 dict-aware 的。
> **被冻结的判据 `not hasattr(p, "text")` 未被改动**（`core/utils.py` 两处原封不动）。
>
> **⚠️ 已知的语义借用：`rehydrate_content` 用 `can_externalize`（一个「**写**」能力）
> 来决定是否「**读**」。** 对当前仅有的两个实现是正确的（`NullBlobStore` 是唯一返回
> `False` 的，而它的 `get` 恒返回 `None`），并且这正是保住「不接 BlobStore 时逐字节
> 不变」的手段——若走 `get → None → 降级` 的路，不接 blob 的宿主会把 inline base64 图
> 降级成 `[image unavailable]`，那就是行为变化。**但一个假想中的「只读 store」
> （能 get、不能 put）会被这条短路误伤：它的图永远取不回来。** 已在代码里内联注明；
> 若将来真的出现只读 store，须把判据换成一个独立的「可读」能力位，而不是删掉短路。
>
> 测试：`tests/unit/test_gateway_rehydrate.py`（14 测），变异验证 6 个变异体全部被杀死。

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
>
> > **已兑现（Phase 3c Task B，2026-08-27，commit `76c86cd`）**：这条重定位**已实现**，
> > 「Phase 4 之前必须补上」的前置条件因此解除。`_serialize_messages` 已改成
> > `while` 索引循环、批处理整段连续 tool 消息、**段末 flush 一条合并的 user 消息**
> > （段末是 OpenAI `tool_calls` 配对要求带来的**硬约束**）。视觉门控另落在
> > **gateway**（`_gate_tool_images`，在 `legalize_messages` 之后、rehydrate 之前）
> > 而非 adapter，因为 `supports_vision` 是 duck-typed、不在 `LLMClient` 协议上。
> > **详见 §14.6。** `_parts_to_text` 的「非文本 part 一律跳过、不产出占位符」这条
> > 不变式**未变**，占位仍由调用方（现在是 tool 分支自己）加。

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

> ⚠️ **本节已作废（2026-08-28）**：入口视觉门控与 `supports_vision` 严格默认已删除，
> 模态能力回归 adapter。见 `2026-08-28-multimodal-adapter-dispatch-design.md`。

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

> **订正（Phase 3c，裁定 D4/D5）**：末行已变——blob 本体与引用表归
> **memory provider**（`providers/memory_sql/`，`memory_blobs` + `memory_blob_refs`），
> GC 是它的 `collect_blobs()`（**不在协议里，宿主定时调**）。
> 「内容形态转换」一行新增一条：**边界归一** `core/content.normalize_content_parts`，
> 由 `MemoryRecord` / `MemoryEvent` / `LLMMessage` 三处 `__post_init__` 共用
> （§14.5）。其余各行不变。

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

> **已兑现（2026-08-25）**：Phase 3 实际分两批落地。
> **Phase 3a**（`c9fa1c6`）：防 400 护栏——`validate_content` 格式校验 + §6.7 视觉能力
> 门控 + 两家 adapter 的空白文本块修复。
> **Phase 3b**（`490af28` → `02fdfc6`）：真 `BlobStore` 实现（§5.4）+ 入口
> `normalize_content` 外部化（§6.1）+ **gateway**（不是 adapter，见 §3③ 裁定 T0）
> rehydrate + per-purpose 图片降级（§13 附表）。
> 与原文的两处偏差：rehydrate 落点是 gateway 而非 adapter；§5.2 的 side index / GC
> **未实现**（用户裁定 D3：本 Phase 只做「不删」）。

> **新增：Phase 3c — 遗留收口（2026-08-27，`8cc336d..65dd606`）**。不在原阶段划分里，
> 是 Phase 3b 终审后从遗留清单里长出来的一段：HITL 入口接校验/外部化（§14.7）、
> 内容边界归一（§14.5）、OpenAI tool-result 重定位（§14.6，**解除 Phase 4 的前置**）、
> 图片 token 体积化（§14.3）、memory 一致性测试套 + 🔴 跨租户泄漏修复（§14.2）、
> SQLite 多模态 provider + blob 并入 memory（§14.4）。**详见 §14。**

**Phase 4 — 折叠与回放**（子设计全文）
`core/media/` + L0.5 + `media:get_image`。

> **已兑现（Phase 4，2026-08-27，`5a6f5e3..4867795` 九个提交）**：
> `core/media/`（`refs` / `policy` / `fold` / `capability`）+ L0.5 接进
> `escalating_compact`（L1 之前）+ §6.1 的 L1/L3 前置降级 + `InvocationResult.content`
> 放宽与 `CONTENT_PARTS_KEY` 通用通道 + `MediaCapabilityProvider` 无条件注册。
> 端到端已验证：L0.5 真降级落库 → 模型**从 wire payload 的文本里扫出占位**、
> 把 ref 抄进工具参数 → gateway → `media:get_image` → 图随 tool result 回到对话尾部
> → 下一轮装配 → adapter wire 上 `b64decode(...) == 原始字节`。
> **子设计有四处表述被实现推翻，已就地加注「订正（Phase 4）」**：
> §2（「图片一律算 0」是 Phase 0 之前的现状）、§4.1（位置靠 `timestamp` 保住，
> **不是** `seq_no`）、§5（与 §6 矛盾，裁定 R2 以 §6 为准）、
> §6.1（对 L1 的顺序错，Task 5b 修）。**落地全貌与遗留处置见子设计 §13。**

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

> **补记（Phase 4）**：上面最后一条落地时收紧了一层——L0.5 与 §6.1 的三处调用点全部
> 由 `compact._media_enabled(ctx)`（`blob_store is not None and can_externalize`）
> **短路**，未接 BlobStore 时**连一次多余的 memory 读都不发**，而不只是「返回 0」。
> 即便闸被绕开，`policy.demotable_ref` 的 `source_type == "ref"` 判据也会让选中集为空。
> **两条已知例外，都不由 BlobStore 门控**：`media:get_image` 无条件出现在工具面
> （子设计 §9 即如此）；`openai.py::_TOOL_IMAGE_NOTICE` 的文案由中文改英文
> （L6 收口，wire-only，且不接 BlobStore 时工具结果不会带图，实际不可达）。

## 12. 测试策略

- **归一层**：`content_with_*` / `*_jsonable` / `redact_*` 的往返与纯文本恒等
- **入口**：四个入口各自接受 `list[ContentPart]` 并落到 `Task.user_prompt`
- **持久化**：写事件 → `rebuild_view` → 图完整还原（跨重启核心保证）
- **装配**：纯图消息不被当空消息丢弃；当前消息框保留 parts；检索 query 仍是文本
- **出网**：两家 adapter 的 blocks 形态；`blob.get()` 返 `None` 时降级不抛
- **估算**：图片计入 token；budget 能因图片触发裁剪；`ContextOverflowError` 文案含图片数
- **回归**：全量既有测试在每个 Phase 后保持绿

> **补充（Phase 3c）**：多模态的持久化保证已不能只靠「某个 provider 的测试」——
> memory 协议是可插拔的，而**多模态无损存取**与**多租户隔离**都是 provider 必须
> 满足的契约。新增 `tests/unit/test_memory_conformance.py`：**面向协议、不面向实现**
> 的一致性套（80 条 × 每个 provider），只经协议声明的 8 个方法操作 provider，
> 不碰任何实现内部字段，能力差异一律用**探测**表达（`describe()` /
> `isinstance(m, BlobStore) and m.can_externalize`）而**绝不写
> `if provider_name == "sqlite"`**——那种写法在第三方 provider 接进来时立刻失效。
> 新 provider 接入只需在 `_PROVIDER_FACTORIES` 加一行。

## 13. 未决项

**Phase 3a 终审复审遗留（2026-08-25 裁定）：**

> **已兑现（2026-08-25，commit `06a2795`）**：下述第一条「dict 形态纯文本会翻转
> `start_session` 的失败类型」已修复，**且刻意未改被冻结的判据**。修法是调整顺序 +
> 惰性解析：`validate_content` 改为「格式校验先行 → 才轮到视觉门控」，并新增
> `llm_resolver` 惰性参数；`start_session` 改传 resolver、不再用 `content_has_image`
> 做闸。三条路径现均正确：纯文本早返回（resolver 不被调用）／dict 内容报
> `InvalidContentError`（resolver 不被调用）／合法图片才解析并门控。
> 顺带修掉一个同源缺陷：原实现**先门控后校验格式**，导致畸形内容被报成
> `VisionNotSupportedError`、掩盖真正的问题。
> 守卫：`tests/unit/test_content_validation.py`（32 passed，含两条 resolver 计数断言）。
> **判据本身（`not hasattr(p, "text")`）仍冻结**——若 Phase 3b 要让归一层认识 dict，
> 仍须同时修 `core/utils.py` 的 `content_to_text` / `image_part_count`。


- **dict 形态纯文本会翻转 `start_session` 的失败类型。** I3 选定方案 (B)（入口只接受
  dataclass 形态 part）后，`content_has_image([{"type":"text","text":"hello"}])` 仍返回
  `True`——因为 `_is_text_part` 用 `hasattr(part,"text")`，dict 永不满足。后果：**dict 形态
  的纯文本内容会触发 `start_session` 的提前 `_resolve_llm`**，而 S2 的修复本意正是让纯文本
  不走这条路。已实测：无 LLM 注册时，`start_session(user_prompt=[{"type":"text","text":"hello"}])`
  抛 `RuntimeError: No LLM available` 而非 `InvalidContentError`。
  内容最终都会被拒（两种错误都是拒绝），但**错误类型对宿主不可预期**。
  Phase 3b 若让归一层认识 dict（方案 A），须同时修正 `core/utils.py` 的
  `content_to_text` / `image_part_count`——三者共用同一判据字面量，只改一处会制造新分歧
  （这正是 Phase 3a 选 (B) 而非 (A) 的理由）。

- **中文注释持续增加 `RUF002/003` 噪音。** 本仓的 ruff 完整口径（含 RUF00x）在本 Phase
  触及的三个文件上从 271 → 334（+63），全部来自新增中文注释里的全角括号。
  各 Phase 的核验口径一直是窄 select（`I001,F401,F811`），在该口径下始终零新增——
  两个数字都对，只是量的不是一回事。若将来要把 RUF00x 纳入 CI 门禁，需先做一次全仓清理，
  否则新代码会被既有的 ~1855 条同类噪音淹没。

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
  > **已裁定（Phase 3c Task D，裁定 D2）**：**不按计费校准**——`1600` 降级为
  > **地板值**（同时是体积未知时的回落值），主口径改成按字节体积算
  > （`// 128`）。这条口径**刻意不建模计费**，它建模的是 provider 请求体的字节压力。
  > 见 §14.3。
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

    > **已兑现（Task 4，Phase 3b，2026-08-25，commit `c00283d`）**：选了第三条路——
    > **不改摘要输入路径，改 composer 的出口**。`compose` 在
    > `return AssembledPrompt(...)` 之前、**`token_count` 计算之前**，对
    > `purpose not in _IMAGE_BEARING_PURPOSES`（= `frozenset({"act"})`）的请求统一调
    > `downgrade_images_to_text`，把 `ImagePart` 换成确定性文本占位
    > `[image {media_type}]`。五条 purpose 分支都汇到这一个出口，故只需一处。
    > 详见下方「§13 附：per-purpose 图片策略（Phase 3b 落地）」。
    > 测试：`tests/unit/test_purpose_image_policy.py`（35 测）与
    > `tests/integration/test_multimodal_end_to_end.py::
    > test_compact_assembly_carries_no_image_while_act_still_does`。
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

    > **已兑现（Phase 3c Task D，用户裁定 D2）**：选的是**第三条路**——既不外挂
    > 独立的字节预算，也不加「单请求总字节上限」，而是**把字节压力折进 token 口径
    > 本身**，让超限在 prepare 的 `ContextOverflowError` 报出来，只留一套机制。
    > 实测新口径下 4 张满额图 = 95% 预算、5 张即超限（旧口径 5 张才 8_000 tok，
    > 不到预算 5%）。见 §14.3。

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

  > **已兑现（Phase 3c Task A/A2，2026-08-27，`ea6a87c` + `82d8081`）**：HITL 已接，
  > 接线点是**三条下层**（`resolve_answer` / `resolve_reject` / **`resolve_approve`**，
  > 第三条最容易漏）。`reopen_task` 经核实**不受影响**——它复用已校验过的
  > `original_user_prompt` 快照、只追加文本，不引入新的外部内容。
  > 回调传**整个 `HitlRequest`** 而非 `session_id`（否则视觉门控判的是默认 client
  > 而非本次应答真正要用的模型，且 tenant 固定 `"default"`）。
  > **详见 §14.7，含仍未闭合的半个洞（L1）。**

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

  > **半兑现（Phase 3c Task B）**：上面这个「放在 `stream_llm`」的倾向**已被采纳并
  > 兑现了一半**——`_gate_tool_images` 就在 `stream_llm` 里（`legalize_messages`
  > 之后、rehydrate 之前），对 `role == "tool"` 的消息做**视觉门控**。
  > 另一半（对 tool result 做完整 `validate_content` 格式校验：畸形 base64 /
  > 超限尺寸 / media_type 白名单）**仍未做**，留给 Phase 4 的 `media:get_image`
  > 落地时一并接。见 §14.6。

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

  > **已裁定并兑现（Phase 3c Task E/E2，用户裁定 D1）**：**选项 A 已废弃，判据不解冻。**
  > 裁定理由：dict 形态是**协议违规**（三个字段的类型声明都是
  > `str | list[ContentPart]`，`MemoryProvider` 契约第 2 条要求原样返回），
  > 修法不该是「给违规形态兜底」而是**在边界把它归一回 dataclass**。
  > `core/content.normalize_content_parts` 是唯一实现，三处 `__post_init__`
  > （`MemoryRecord` / `MemoryEvent` / `LLMMessage`）共用；两家 adapter 的 dict
  > 死分支已删。上面说的「三处判据须同步改」因此**不再是待办**——三处判据保持
  > 逐字节冻结（Phase 3c 已用 AST 比对复核）。**详见 §14.5。**


---

## §13 附：per-purpose 图片策略（Phase 3b Task 4 落地，用户裁定 D2）

用户裁定原话：「observe 也不是很需要看图。除了 act 以外的步骤对图片没有需求」。

**当前取值表**（`core/assembler/composer.py`，`_IMAGE_BEARING_PURPOSES = frozenset({"act"})`）：

| compose purpose | 携带真实图片 | 理由 |
|---|---|---|
| `act` | ✅ 是 | 唯一需要模型真看图的步骤 |
| `compact` | ❌ 降级 | 恰在超预算时触发；产出按 §8 恒为纯文本 |
| `observe` | ❌ 降级 | 判任务成败靠 actor 产出与工具结果（**见下方功能回退风险**） |
| `background_observe` | ❌ 降级 | 同上 |
| `recognize_intent` | ❌ 降级 | 只是填元数据 |

**若将来要让某个 purpose 重新看图，改这一处**：`composer.py` 的
`_IMAGE_BEARING_PURPOSES` 集合加上该 purpose 名即可，别处无需改动——五条 purpose 分支
都汇到 `compose` 末尾同一个出口。（例如要让 observe 看图：
`frozenset({"act", "observe"})`。）

**降级实现**：`core/content.py` 的 `downgrade_images_to_text` 把图片 part 换成
`[image {media_type}]`。放在归一层而非 composer 内联，遵 §3① 单一归一层；纯文本
（`str` / `None` / 空 / 全文本 part）**原样返回同一对象**。

### 落点必须在 `token_count` 之前（架构裁定 T1）

降级插在 `compose` 的 `token_count` 计算**之前**。若挪到之后，报出的 token 数含图、
实际发出的 prompt 已无图，**budget 与 compact 会基于错误的数字判断**。
两层测试各钉一次：
`tests/unit/test_purpose_image_policy.py::test_token_count_excludes_image_tokens_for_downgraded_purposes`
与端到端的
`tests/integration/test_multimodal_end_to_end.py::test_compact_assembly_carries_no_image_while_act_still_does`
（两处变异体「把降级挪到 token_count 之后」均被杀死）。

**策略为什么不能放 gateway**：`LLMRequest` **没有 `purpose` 字段**（已核实），
gateway 无从区分；composer 知道 purpose。这与 rehydrate 落 gateway 并不矛盾——
rehydrate 是 purpose-无关的形态还原，降级是 purpose-相关的策略。

### 降级占位必须逐字节确定性（用户裁定 D2 的附带硬约束）

占位文本对同一张图**必须恒定**：`[image {media_type}]`，
**不得含 blob sha / 随机 id / 时间戳 / 计数器**；同一条消息里多张图共用同一占位、
**不加序号**。`rehydrate_content` 取不到图时的
`[image unavailable: {media_type}]` 受同一条约束。

理由（controller 核实、用户认可）：五种 purpose 各自发送**不同的 tools 集合**，而
tools 排在缓存前缀最前面，所以各 purpose 之间本就从不共享缓存条目——降级伤不到别的
purpose。但若占位文本每次不同，**该 purpose 自己的前缀每次都变**，会砸掉它自己的
自动前缀缓存；而 compact 恰在上下文超预算时触发，正是最需要命中缓存的时刻。

（核实附注：本仓当前**未启用** Anthropic 显式缓存——全仓无 `cache_control` /
`anthropic-beta` 头，只有 DeepSeek 方言读 `prompt_cache_hit_tokens` 做统计。
故此约束今日不影响行为，是为将来启用时的正确性兜底。变异「占位掺 sha」杀死 9 条测试。）

### ⚠️ compact 的两套口径（设计意图，不是 bug）

`_active_memory_tokens`（`core/loop/steps/compact.py:205`）读的是 **memory 记录**、
**不经 composer**，因此 compact 的**折叠层级判断仍按「含图」口径**（memory 里图还在，
每张按 `_IMAGE_PART_TOKENS = 1600` 计）；而 compact **自身发出去的 prompt 已不含图**
（经上表降级）。

**两套数字并存是设计意图**：折叠判断要反映「memory 里实际压着多少东西」（图还在那儿，
Phase 4 的 L0.5 才会真正折走它），而 prompt 只需要文字。**明写在此以免后来者把它当
bug「修」成一套口径**——统一到「不含图」会让折叠层级低估 memory 的真实体积，
统一到「含图」会让 compact 自己的 prompt 白白带上图（正是本 Phase 刚消除的问题）。

### ⚠️ 冻结判据对 dict 形态**文本** part 的误判（Task 4 实测）

§13 上方已记「dict 形态 part 会被 `image_part_count` 全数计成图片」。Task 4 实测把它
钉成逐字确凿的一对数字：

```
image_part_count([{"type": "text", "text": "hello world"}])  ->  1    # 被计成 1600 token
content_to_text([{"type": "text", "text": "hello world"}])   ->  ''   # 摘要器完全看不见
```

即：**同一个 dict 形态的纯文本 part，在 token 估算里被当成图片多算 1600，在拍扁成
文本时又被当成图片跳过、渲染成空串。** 两个盲点同源（同一判据
`not hasattr(p, "text")`），方向相反，叠加起来是「既多算 token 又丢内容」。

**判据仍冻结**（改它必须同时改 `core/content.py::_is_text_part` /
`core/utils.py::content_to_text` / `core/utils.py::image_part_count` 三处，
`test_content_module.py::test_content_to_text_reexported` 还把 `content_to_text` 钉死为
同一对象引用）。此处只是把盲点写明。

**这个盲点正是 Task 3 与 Task 4 在 dict 形态上刻意分歧的原因**，两者都是对的：

- `rehydrate_content`（Task 3）：**dict 进 dict 出**——它只是把 ref 换回 base64，形态
  不变，adapter 的 dict 分支照常工作。若改成「判不出形态就降级成占位」，会把**今天
  工作正常的 dict 形态 base64 图片**也一并降级，对已接 BlobStore 的宿主构成新的能力回退。
- `downgrade_images_to_text`（Task 4）：**dict 进、dataclass `TextPart` 出**——因为它
  把图**转成文本**，若回吐 dict 文本，占位会被摘要器渲染成空串（信息白留）**且**仍被
  计成 1600 token，本任务的两个目的双双落空。
- 两者判 dict 形态是不是图片都走显式的 `part.get("type") == "image"`（与两家 adapter
  的 `_parts_to_blocks` 一致），**不套冻结判据**——dict 上永远取不到 `.text`，套冻结
  判据会把 `{"type":"text","text":"hi"}` 换成 `[image image]`，属实打实的内容损坏。

### ⚠️ observe 看不到图：本次降级面里唯一的真实功能回退风险

裁定 D2 原话是「observe 也不是很需要看图」，实现照做无误。但若某个 task 的成败判据
**就是**「图里有没有那个东西」（例如 actor 的产出本身是一张渲染图），observer 只会
看到 `[image image/png]`，只能靠 actor 的文字自述来判定。

四个被降级的 purpose 里，只有 `observe` / `background_observe` 存在这个风险
（`compact` 产出恒为纯文本、`recognize_intent` 只填元数据）。缓解手段已就位：改
`_IMAGE_BEARING_PURPOSES` 一处即可放开（见上表）。Phase 4 的 `media:get_image` 落地后
还有第二条路——让 observer 主动取回它需要看的那张图。

> **用户裁定 D3（2026-08-26）：维持现状，observe 不看图。** 原话：
> 「observe 的作用是看 actor 有没有干活，不是替代 actor 决策。」
> 即职责边界是**执行性检查**而非**内容性仲裁**。上面记的功能回退风险仍然如实，
> 但它是**已知且被接受的**代价，不是待修缺陷——**不要再当新发现重提**。
> 裁定理由的完整表述见 §14.1 的 D3。

---

## 14. Phase 3c 收口（2026-08-27）

Phase 3c 是「多模态遗留问题收口」，十个提交（`8cc336d..65dd606`）：

| 任务 | commit | 内容 |
|---|---|---|
| A / A2 | `ea6a87c` / `82d8081` | HITL 三条下层接上校验与外部化 |
| E / E2 | `72c6a3c` / `5f2278c` | 内容边界归一，三处共用一份实现 |
| B | `76c86cd` | OpenAI tool-result 图片重定位 + gateway 视觉门控 |
| D | `1be2296` | 图片 token 改为体积相关 |
| C1 | `b645781` | 面向协议的 memory 一致性测试套（57 条） |
| C1b | `ad403da` | 🔴 `load_view` 跨租户泄漏（安全修复） |
| C2 | `f1bb035` | SQLite/SQLAlchemy 多模态 memory provider |
| C3 | `65dd606` | blob 并入 memory + 延迟回收 + 移除 `FilesystemBlobStore` |

全量回归 `1978 total / 1970 passed / 3 failed / 5 skipped`
（3 failed 为既有环境性失败：缺 ROLE.md / 缺 golden 目录 / `test_compact_flow_e2e`）。

**宿主怎么切**：见独立一篇 [宿主迁移清单](../../host-migration-to-sql-memory.md)。

---

### 14.1 用户裁定（2026-08-26）

**D1 —— 判据不解冻。** `not hasattr(p, "text")` 三处（`core/content.py::_is_text_part`
/ `core/utils.py::image_part_count` / `core/utils.py::content_to_text`）保持 §13
的冻结状态。理由不是「懒得改」而是**dict 形态本身就是协议违规**：`MemoryEvent.content`
/ `MemoryRecord.content` / `LLMMessage.content` 的类型声明都是 `str | list[ContentPart]`，
`MemoryProvider` 的多模态契约第 2 条要求原样返回。修法在**边界归一**（§14.5），
不是给违规形态兜底。§13 原文里「Phase 3b/4 若要收敛须同步改三处」的方案**已废弃**。

**D2 —— 修图片 token 统计本身**（体积相关），让超限在 prepare 的
`ContextOverflowError` 报出。**不**外挂「composer 按张数设限」那套第二机制。详见 §14.3。

**D3 —— observe 不看图，维持现状。** `_IMAGE_BEARING_PURPOSES = frozenset({"act"})` 不变。

> **裁定理由（用户原话，写在此处以免被反复重提）**：
> 「observe 的作用是看 actor 有没有干活，不是替代 actor 决策。」
>
> 即：observe 的职责边界是**执行性检查**（干没干活），不是**内容性仲裁**（干得对不对）。
> 后者是 actor 自己和用户的事。§13 附末尾记的「observe 看不到图是唯一的真实功能回退
> 风险」仍然成立、仍然如实——但那是**已知且被接受的**代价，不是待修缺陷。真需要放开时
> 改 `_IMAGE_BEARING_PURPOSES` 一处即可；Phase 4 的 `media:get_image` 落地后还有第二条
> 路（让 observer 主动取回它要看的那张图）。

**D4 —— blob 并入 memory（C-merge）。** core 仓新写 SQLite 多模态 provider
（`providers/memory_sql/`），宿主后续平滑切换。**故表名列名必须兼容宿主存量行，
新增列一律 nullable**。

**D5 —— 移除 `FilesystemBlobStore`。** 其 9 条测试改挂 SQL provider——验的是
`BlobStore` 契约而非某个实现。

**D6 —— 纯内存 provider 不支持多模态**，走 `can_externalize=False` 既有路径。
这不是遗漏，而是**双模式兼容性对照**：同一套 conformance 断言对「支持 blob 的
provider」与「不支持的 provider」都成立，后者的 blob 用例按能力探测 skip。

---

### 14.2 🔴 多租户隔离契约（Phase 3c 最重要的安全条目）

**已实证的泄漏**（Task C1b 之前）：

```
tenantB load_view 读到: ['TENANT-A-SECRET']
```

成因：`_address_match` 只比 session/task/agent，`load_view` 拿了 `ctx` 却从不读
`ctx.tenant_id`。触发条件是两租户 `session_id` 相同——而 `start_session`
**支持宿主自带 session_id**，故非理论问题。

**同型的另外三处，其中第一处比原洞更严重**：

1. **写侧的隐式扫描**：`ingest` 的 PUBLICATION 覆盖按 topic 扫全表标 superseded、
   不看 tenant。**只修读侧会造成比修前更差的状态**——B 一发布就把 A 同 topic 的行标
   superseded，而 B 自己也读不到那行。即「**能销毁自己读不到的数据**」，
   一种比泄漏更差的不对称。
2. **订阅身份串号**：`subscribe_topic` 的幂等键 `(session, task, topic)` 不含 tenant，
   B 的订阅**撞进 A 的幂等分支**、拿到 A 的订阅对象与游标。不只是列表泄漏。
3. **`recall_recent` 家族的 tenant 比较本就是空转**——目标键与被比键都用**读侧**的
   `ctx.tenant_id`，tenant 在等式两边约掉了。这类「看起来有隔离」的代码比明摆着没有
   更危险。

**取向：归一到 `"default"`，写读两侧同一个函数**（`normalize_tenant(t) = t or "default"`；
SQL 侧对应 `COALESCE(tenant,'default')`）。三条理由：

- 既有数据与既有调用点全部落在 `"default"`，归一后单租户路径逐字节不变；
- 严格比较只在两侧都给出明确非默认值时才生效，避开了「一处 `""`、一处 `None`、
  一处 `"default"` → `load_view` 静默返空 → **会话失忆**」这个比泄漏更难诊断的失败模式；
- 与仓内既有惯例同形（`reducers.py:495` 的 `ev.tenant_id or "default"`）。

**⚠️ 划线原则（哪些刻意没修）**——已作为隔离契约第 5 条写进 `MemoryProvider` docstring：

> **可见性相关的隐式跨行扫描**随读侧一起按租户分区（`load_view` / `recall_topic` /
> `recall_semantic` / PUBLICATION 覆盖 / 订阅表）；
> **调用方显式给 id 的操作**保持**全局 id 命名空间**不动（`ingest` 按 id 幂等、
> `fold` 按 id 遗忘、`BlobStore.get(ref)`）。

它们不是「扫出别人的行」，而是「调用方指名了一个 id」。要改得先定「record id 是全局
唯一还是租户内唯一」——那是协议级决定。**这条划线是记录在案的取舍，不是默认安全**：
in_memory 自生成的 id 是 `mev_%08d` 顺序号、**可猜**（遗留 L17）。

**对任何新 provider 的硬约束**：tenant 必须是**列 + 索引/唯一键的一部分**
（`events(tenant, session_id, …)`、`subscriptions` 唯一键
`(tenant, session_id, task_id, topic)`），不能只在应用层过滤。存量表没有 tenant 列时
「新增 nullable 列 + 读侧 `COALESCE`」正好与归一规则一致，**存量数据不迁移即落默认分区**。

**守卫**：`tests/unit/test_memory_conformance.py` 的 23 条隔离用例对**所有** provider
生效。其中一条变异值得记——把分区键退化成「按 `ProviderContext` 对象身份」
（`load_view` 恒返空，「修过头」的典型形态）会让 **44 条转红**，即「返空」在这套里
不可能悄悄通过。

---

### 14.3 图片 token 统计：体积相关（裁定 D2）

**订正 §6.5**：`image_tokens` 不再是 `_IMAGE_PART_TOKENS × 张数`，改为逐 part

```python
max(_IMAGE_PART_TOKENS, image_byte_size(part) // _IMAGE_BYTES_PER_TOKEN)
```

`_IMAGE_BYTES_PER_TOKEN = 128`；体积未知（`byte_size` 为 None 的 ref/url）时回落旧常数
`_IMAGE_PART_TOKENS = 1600`。§6.5 原文的函数体保留以存上下文，但那不再是现行实现。

**新增 `ImagePart.byte_size: int | None = None`**（可选、有默认，既有构造点零改动）。
存在理由：外部化后 `data` 是 `blob:<sha>`（长度恒约 69），体积信息就此丢失，而
`image_tokens` 是同步函数、不能回 BlobStore 做 IO 取回来。`image_byte_size(part)` 两条
来源：`byte_size` 字段 → inline base64 的 `len(data)*3//4 − padding`；**刻意不解码**
（为估算 b64decode 一张 5 MiB 图在装配热路径上不可接受）。

**系数 128 B/tok 的完整推导**：

1. 约束的真实上限是 **provider 请求体**（Anthropic 约 32 MB）。base64 膨胀 4/3 →
   可容纳原始字节 ≈ 24 MiB = 25_165_824 B。
2. 仓内典型预算实测取值：`context_limit = 180_000`（`core/state/models.py:132` /
   `core/control/types.py:32` / `core/control/reducers.py:241,417` 四处默认值一致）、
   `reserved_output_tokens = 8_192` → `effective_limit = 171_808`。
3. 令「预算耗尽」与「请求体触顶」对齐：`25_165_824 / 171_808 ≈ 146.5 B/tok`。
4. 向下取到 2 的幂 **128**：取整方向使估算**偏高**（与 `estimate_tokens` 的「保证单边
   高估」同向——低估触发 provider 400，高估只浪费窗口），且 128 是移位、纯整数。

校验：单张满额图（`_MAX_IMAGE_BYTES = 5 MiB`）= 40_960 tok；4 张 = 163_840 = 95% 预算
（早过 `compact_token_ratio = 0.8` 的触发比）；5 张 = 204_800 > 171_808 → 地板仍超限时
`budget.py:91` 抛 `ContextOverflowError`。旧口径下 5 张满额图账面才 8_000 tok
（不到预算 5%）——这正是 §13 (c)「字节上限无约束」那条缺陷的成因。

> **⚠️ 这个数建模的是「字节压力」，不是「计费 token」。** 后来者极可能拿它去算成本——
> **不能**。模型侧对一张图的真实 token 成本是**封顶**的（provider 会先降采样到自己的
> 最大边长，Anthropic 与 OpenAI 都如此），一张 5 MiB 图不会真的计 40_960 个计费 token。
> 本口径存在的唯一目的，是让「请求体积会打爆 provider」这件事在 **prepare 阶段**就以
> `ContextOverflowError` 的形式报出来，而不是等到 provider 侧 400。要做成本核算，
> 请另立一套按 provider 计费规则的口径，不要复用 `image_tokens`。

**地板保留 `_IMAGE_PART_TOKENS = 1600`**（< 200 KiB 的图仍按它计）：① 模型侧对任意一张
图的固定开销本就在这个量级，往下折算会低估；② 它同时是体积未知时的回落值，保证存量
数据与不接 BlobStore 的宿主不劣化。副作用：既有测试（小图 == 1600）全部原样通过。

**一个新引入的可踩点**：`image_tokens` **不再是 1600 的整数倍**，任何「对 image_tokens
整除反推张数」的写法都会错（当前无此类调用点，`budget.py` 走 `image_part_count`）。

**一条测试方法论**（Task D 的 M9 首轮存活）：padding 修正的 1~2 字节误差经
`image_tokens` **不可观测**——地板吞掉小图、`// 128` 的整除吞掉大图。即
**「只经聚合函数断言」对精度型缺陷天然不敏感**，必须直接断言底层函数才杀得掉。
（同型的还有 Task C1 的 M18：`len(got) <= 5` 这种「不超过上界」型断言对「什么都没做」
返空不敏感。）

---

### 14.4 blob 生命周期（裁定 D4/D5）

**订正 §5.2 / §5.4 / §7 的模块归属**：blob 字节的持有者不再是
`FilesystemToolsProvider`（该实现已按 D5 移除，全仓 `src/` 零引用），而是
`SqlMemoryProvider` 的两张表：

- `memory_blobs(sha PK, media_type, data, created_at)` —— **没有 tenant 列**；
- `memory_blob_refs(event_id PK, sha PK+index)` —— §5.2 设想的 side index，
  但**长在 memory 内部**。

**引用边在 `ingest` 的同一个事务里写。** 这正是「blob 并入 memory」才做得到的事：
原方案里字节与引用索引分居两处，无论如何都存在「事件已落库、引用还没记上」的窗口。
ref 的提取复用归一层（`core/content.extract_blob_refs()`，判据直接调既有的
`_is_ref_part`），与 `rehydrate_content` 会去 `BlobStore.get` 的那批 part 逐一对应。

**回收查询**（`SqlMemoryProvider.collect_blobs(now=None) -> int`，延迟、幂等、
不在写路径上）：

```sql
DELETE FROM memory_blobs
WHERE created_at < :cutoff                       -- 宽限期（正确性要求，见下）
  AND sha NOT IN (SELECT r.sha FROM memory_blob_refs r
                  JOIN memory_events e ON e.id = r.event_id
                  WHERE e.is_superseded = 0)     -- 活引用，**不按 tenant 过滤**
```

**三道判断题的裁定与理由**（也写在 `SqlMemoryProvider` 类 docstring 的【blob 与租户】段）：

1. **回收侧不按 tenant 过滤活引用（看全表）——这是安全要求，不是选择。**
   内容寻址跨租户去重（同字节 → 同 sha → 同一行），只看本租户的活引用，
   A 的一次 fold 就会删掉 B 仍在引用的那一行。
2. **`get` 不校验 tenant。** (a) 与 §14.2 契约第 5 条**同一条划线**——`get(ref)` 正是
   「调用方显式给 id」的操作。(b) 必须与第 1 条**自洽**：既然一份字节跨租户共享同一行，
   行上就没有「属于谁」可校验；硬记 owner 再校验会让第二个租户取不回自己合法引用的图
   → `get` 返 None → rehydrate 降级成 `[image unavailable]` → **图永久丢失**
   （变异 M10 实证：加上校验后跨租户用例立刻红）。(c) sha 是 SHA-256 内容哈希，
   能说出 sha 意味着已持有该内容。**残余风险**是一条 sha 可探测的存在性侧信道（L26）。
3. **`put` 跨租户同 sha 共享一行。** 内容寻址去重的全部价值在此，且是第 2 条自洽的前提。
   `media_type` 冲突沿用 filesystem 实现的既有语义：**先写入者胜**。

> **⚠️ 宽限期是正确性要求，不是优化。** 原方案把回收分两类、只对「从未被引用」的 blob
> 加宽限期，「在引用表里但无活引用」的立即删——**照做会留一个悬空 ref 的洞**：
> `put` 命中一份**旧**字节时（其引用者已全部 fold），新的 put→ingest 窗口照样打开，
> 按第 1 类判定会在新 ingest 落地前把它删掉。
>
> 修法两条：① **`put` 刷新 `created_at`**（含命中已有行的幂等 put），语义从
> 「首次出现时间」改成「**最后一次有人声称要用它**」；② **宽限期对两类一视同仁**。
> 结果严格更保守，代价只是「fold 之后字节多留一个宽限期」——泄漏磁盘，是安全方向。

**宽限期默认 24 小时**（`SqlMemoryProvider(..., blob_grace_period=...)` 可覆盖）。
下界由真实窗口定：put→ingest 在进程内是毫秒级，但中间隔着 **HITL park（可等人数小时）**、
重试、宿主重放；上界由泄漏成本定（孤儿最多堆积一天的上传量）。`created_at` 与 `now`
可能来自不同机器的时钟，小时级窗口对分钟级漂移免疫。且这一侧**错误不对称**：
删早了图永久丢失，删晚了只是多占一天磁盘。

**永不在 `fold` 里同步删**（`test_fold_does_not_delete_bytes_synchronously` 钉住）。

> **⚠️ `collect_blobs` 不在 `MemoryProvider` 协议里**，是 `SqlMemoryProvider` 的自有方法，
> **宿主必须自己定时调**。core 里没有任何调用点——刻意的：回收时机是运维决策，且 core
> 不该在任何写路径上触发删字节。宿主永不调用的后果是「blob 只涨不删」（泄漏磁盘），
> **不会**产生悬空 ref。见迁移清单第 5 步。

**⚠️ 行为变更：`ProviderRegistry.get_blob_store()` 改为三级自动解析**——
**显式注册 > memory provider（若 `isinstance(BlobStore)` 且 `can_externalize`）>
`NullBlobStore`**。即宿主一旦注册 `SqlMemoryProvider` 作 memory，**图片外部化自动开启**
（此前须显式 `register_blob_store`）。这是 D4 的本意，但对存量宿主是「换 provider 顺带
打开了新行为」，已在迁移清单第 4 步点名。回落结果**不缓存**进 `_blob_store`——缓存会让
「先 `get_blob_store()`、后 `register_memory()`」的接线顺序静默拿不到 memory。

**订正 §5.5**：那条「接了真 BlobStore 的宿主必须显式传 `session_id` 并预先
`register_session(session_id, workspace)`」的隐式契约，是 `FilesystemBlobStore` 的
`workspace_for(ctx)` 带来的，**随该实现移除而不再适用**于 SQL 形态。原文保留——
它对仍在用 `FilesystemToolsProvider` 的 `SpillSink` 路径依然是对的，也是理解
「实现专有的接线契约会怎样咬人」的样本。

**那条 Windows UNC 安全护栏测试没有删**，改挂 SQL provider 后**保留为纯契约条**：
SQL 侧不构造任何路径、sha 只作绑定参数进 WHERE，该攻击面确实不存在；但
「ref 前缀不对必须返 `None` 而不是抛」仍是 `BlobStore` 契约，删掉会让这条契约在本
provider 上失去覆盖。

---

### 14.5 内容边界归一（裁定 D1，订正 §13 的 dict 方案）

**dict 形态的 part 是协议违规，不是需要兼容的形态。** 三个字段的类型声明都是
`str | list[ContentPart]`。但违规输入现实存在（JSON 往返的第三方 memory provider、
宿主直构 `LLMMessage`），且后果**全是静默的**：

```
image_part_count([{"type":"text","text":"hello"}])  ->  1     # 多算 1600 token
content_to_text([{"type":"text","text":"hello"}])   ->  ''    # 摘要器完全看不见
_parts_to_blocks([{"type":"image",...}])            ->  []    # Task E 实测：整个丢掉
```

**修法落在类型自己的边界、且只此一份**：`core/content.py::normalize_content_parts`
是**唯一实现**，三处 `__post_init__` 共用——`MemoryRecord`（读侧）/ `MemoryEvent`
（写侧）/ `LLMMessage`（出网侧）。在三处各写一遍 `isinstance` 分支，正是 §3① 单一
归一层要防的散点；`test_three_boundaries_call_the_one_shared_implementation`
专门钉住「共用而非三份复制」（变异「让 `MemoryEvent` 自己抄一份行为等价的实现」
只有这一条会红，行为测试全绿）。

**两家 adapter 的 dict 分支已删**（`_parts_to_text` / `_parts_to_blocks` 各两处）。
入参来源已核实：只有 `LLMMessage.content`，而它现在在构造时就被归一。

**为什么不在 adapter 里 raise**：adapter 在同步出网主路径上，抛异常会掀掉整个 LLM
请求（同 Phase 3b 对 `BlobStore.get` 恒不抛的取向）。归一是正解。

**顺序硬约束**：`MemoryEvent.__post_init__` 的归一放在**全部既有校验之后**——
`content=None` 等报错路径不得被归一抢先（变异「把归一提到校验之前」会杀死
`test_memory_event_validation_still_runs`）。

**快路径与对象同一性**（都返回**同一对象**，不重建）：`str` / `None` / 空立即返回；
已合规的 dataclass 列表只多一次 `isinstance` 扫描。扫描刻意用 `for/else` 而非
`any(genexpr)`——实测生成器创建开销比扫描本身还大（691.7 ns vs 512.1 ns）。

**热路径开销与 import 形态**（本 Phase 最大的一处实现分歧，记以备后来者）：三处
`__post_init__` 无条件调归一。若按「沿用函数级 import」写 `from ctx_weft.core.content
import ...` 进 `__post_init__`，**每次构造**都要跑一遍 `__import__` +
`_handle_fromlist`——实测该语句本身 **361 ns**，把 `LLMMessage` 构造从 189 ns 抬到
**682 ns**（3.6 倍）。Task E 之所以没吃到这个成本，是因为它的 import 在**快路径之后**。
改用 `protocols/context.py` 的**惰性绑定**（缓存**模块对象**而非函数，属性查找留在调用
时 → monkeypatch 仍生效、不留陈旧绑定的坑）后降到 **302 ns**。层序理由：protocols 是比
core 低的层，模块级导入 core 会把依赖反向；现状是「运行时反向、导入期不反向」。
**若将来要彻底摆脱，正解是把 `normalize_content_parts` 下沉进 protocols 层**
（它只依赖 TextPart/ImagePart），而不是继续加绑定。

**`rehydrate_content` 本身仍是「dict 进 dict 出」**，Phase 3b Task 3 的裁定未被推翻
（旁边两条直调它的用例仍钉着）。变的是**上游**：`LLMMessage(content=[dict])` 在构造时
就被归一成 dataclass，dict 到不了 rehydrate。

---

### 14.6 OpenAI tool-result 图片重定位（兑现 §6.6 的「未兑现」条目）

§6.6 里那条「Phase 2 没有实现、着手 Phase 4 之前必须补上」的重定位，**Phase 3c Task B
已兑现**（commit `76c86cd`）。`_serialize_messages` 的 `for m in messages` 改成
`while i < len(messages)` 索引循环，`role == "tool"` 分支**批处理整段连续 tool 消息**
（与 `anthropic.py` 的 tool 分支同构）。

> **段末 flush 是硬约束，不是整洁性选择。** 每条 tool 消息仍只发文本，含图时把图
> `extend` 进段级 `relocated` 并给文本尾部追加标记，**整段 while 退出后**才
> `result.append({"role": "user", "content": _parts_to_blocks(relocated)})`。
> 挪到「每条 tool 之后」会触发 OpenAI 的
> `insufficient tool messages following tool_calls message`——同批多个 tool call 时，
> 追加的 user 消息夹在中间会打断 `tool_calls` 的配对要求。
> 守卫：`test_consecutive_tool_messages_flush_once_after_whole_segment`。

标记 `_TOOL_IMAGE_NOTICE = "\n\n[图片见后一条消息]"` 是**模块常量、逐字节确定**
（无 sha / 随机 id / 时间戳 / 计数器）——同 §13 附「降级占位必须逐字节确定性」的缓存
约束。纯图结果（文本为空）用 `_TOOL_IMAGE_NOTICE.lstrip("\n")` 当非空占位：**OpenAI 拒
空 content**（同 Anthropic 侧 `_EMPTY_TOOL_RESULT_CONTENT` 的理由）。
`str` 形态 content 原样透传（纯文本 wire 逐字节不变）；**无图的 parts 形态不追加标记**。

**视觉门控落在 gateway 而不是 adapter**（新增 `_gate_tool_images(llm, messages)`，
在 `legalize_messages` 之后、**rehydrate 之前**调用）。两个理由：

1. **`supports_vision` 是 duck-typed、不在 `LLMClient` 协议上**——core 侧统一约定
   `getattr(llm, "supports_vision", False)`（§6.7）。adapter 是被解析出来的那个对象本身，
   在 adapter 内部读自己的能力位没有意义；gateway 持有的 `llm` 才是**真正会被发到的
   那个 client**（经 `stream_llm_resilient` 时是 `_FixedModelClient`，带 per-model 声明）。
2. 门控放 **rehydrate 之前**：注定被降级的图不必先去 BlobStore 取一趟回来。
   `downgrade_images_to_text` 对 ref 形态同样只读 `media_type`，占位文本不变。
   守卫：`test_no_vision_skips_blob_fetch_for_tool_images`。

这补上了 §13「工具产出的图片绕过全部三道护栏（I4）」那条的**一半**——门控确实落在了
`stream_llm`（§13 当时的倾向判断被兑现且被证明是对的）。另一半（对 tool result 做完整
`validate_content` 格式校验）仍未做，留给 Phase 4 的 `media:get_image`。

**残余**：宿主若把裸 `OpenAIAdapter` 直塞 `ctx.llm`，`supports_vision` 恒 `False` →
工具图一律降级。这与 §6.7 的 `validate_content` 严格默认同源、fail-closed，非新增；
但「门控落 gateway 就拿得到真正的 llm 对象」在裸 adapter 形态下不成立（L5）。

**gateway 层测 tool 消息的坑**（值得所有后续任务知道）：`stream_llm` 第一步
`legalize_messages` 里，`drop_orphan_tool_results` 会把「前面没有配对 assistant
tool_calls」的 tool 消息**整条丢掉**，`ensure_leading_user` 又会砍掉首条非 user 消息。
所以单条 `LLMMessage(role="tool", ...)` 进 `stream_llm` 会因空消息列表 `IndexError`——
**红是红了，但红的原因不是被测逻辑缺失**。必须构造完整回合
`user → assistant(tool_calls) → tool…`，断言按 role 选取而非下标。

---

### 14.7 HITL 入口（兑现 §13「已知缺口」的 HITL 一半）

§13「已知缺口（Task 4，Phase 3a）」里那条「HITL 应答与 `reopen_task` 两个入口未接
校验与门控」，**HITL 部分已兑现**（`ea6a87c` + `82d8081`）。`reopen_task` 经核实
**不受影响**：它复用已校验过的 `original_user_prompt` 快照，经 `content_with_suffix`
追加文本，不引入新的外部内容。

**接线点是三条下层，不是一条**：`resolve_answer` / `resolve_reject` /
**`resolve_approve`**。第三条容易漏——approval 备注同样是 `str | list[ContentPart]`。

**回调签名传整个 `HitlRequest`**（`async (content, req) -> content`），不是
`(content, session_id)`。理由是把签名钉死在 session_id 上会挡住两件必需的事：

- **视觉门控必须判本次应答真正要用的模型**（`req.resume_llm_account/model`），
  而不是默认 client `_resolve_llm(None, None)`——否则 Phase 3a 建的 per-model 门控在
  这条路径上判的是另一个模型的能力。
- **`tenant_id`** 否则固定 `"default"`，多租户 blob 落错锚点。

沿用该类**已有的注入惯例**（第三个 setter `set_content_normalizer`），`HitlManager`
不 import `BlobStore` / LLM 任何类型；**未注入时是恒等变换**（`return content`，
同一对象）。

**由 session_id 解 tenant 走三级回落**：活 `TaskManager` 持有的 `Session.tenant_id`
（热应答主路径，纯内存查表）→ 事件日志第一条（**每条 `Event` 都带 `tenant_id`**）→
`"default"`。**不用 `rebuild_view`**——它要折叠整个投影才拿一个字符串。
整段 **best-effort 不抛**（HITL 应答路径抛错会卡住人类应答），并加**纯文本 / blob store
不能外部化时根本不解 tenant** 的短路（冷路径要读事件日志，代价不小）。

> **⚠️ L1 洞只堵了一半。** `_stash_resume_llm` 只在 `answer` / `approve` / `reject`
> 三个公开方法里调，而 `resolve_answer` / `resolve_reject` / `resolve_approve`
> **同为公开方法却不调它**。宿主直接调 `resolve_*` 时 `resume_llm_*` 为 `None` →
> 门控退回默认 client。**非回归**（本来如此），但上面那条「按本次应答的模型门控」
> 在该调用形态上未真正关闭。根治应在 `HitlRequest` 补 `tenant_id` / 让
> `HITL_REQUIRED` 投影在 `request()` 时就带上这些（那时已知），需动事件 payload 与
> reducer、并处理存量事件回放，Phase 3c 判为越界。

---

### 14.8 遗留清单 L1–L30 的处置

Phase 3c 的十个任务共记录 30 条遗留。逐条判定如下（**不是「待办列表」，是分类**）：

#### 已闭合（4 条：L8 / L15 / L16 / L21）

| # | 内容 | 闭合方式 |
|---|---|---|
| L8 | `image_tokens` 不再是 1600 的整数倍 | 已核实无「整除反推张数」调用点；写进 docstring 与 §14.3 的「可踩点」 |
| L15 | `load_view` 无租户隔离 | C1b 修复（§14.2），并升级为 conformance 断言 |
| L16 | 黑板覆盖语义不在协议 docstring 里 | 已补进 `recall_topic` docstring + conformance 用例 |
| L21 | core 侧 tenant 串接是否有不一致 | 15 个 `ProviderContext` 构造点逐点核实：喂给 memory 的全部取自 `session.tenant_id` / `params.tenant_id`；**不存在「写 A 读 default」的仓内路径** |

#### 移交 Phase 4（12 条：L1 / L2 / L3 / L4 / L10 / L12 / L13 / L14+L24 / L17 / L25 / L29）

| # | 内容 | 建议做法 |
|---|---|---|
| L1 | `resolve_*` 三个公开方法不调 `_stash_resume_llm`（§14.7 的半个洞） | 与 L2 一起做 |
| L2 | `HitlRequest` 无 `tenant_id`，根治要动 `HITL_REQUIRED` payload/reducer + 存量回放 | 与 L1 一起做，一次动事件 payload |
| L3 | 冷路径解 tenant 要全量拉一次事件，`EventStore` 缺窄查询 | 给 `EventStore` 加一个窄查询，**不要**在 runtime 侧堆缓存 |
| L4 | `test_dispatch_boundary_recap_e2e` 曾在一次全量跑中失败，其后 15+ 次未复现 | 观察项。留在台账，别当新发现重报。**→ Phase 4 再次出现并查明机制（P4-L12）：不是超时，是 `_RouterLLM` 把 `tools=[]` 的摘要调用误路由进 act 分支；底下可能盖着一个真的折叠缺口。完整诊断与处置建议见子设计 §13.3，仍未闭合** |
| L10 | `_IMAGE_BYTES_PER_TOKEN` 硬编码（32 MB 是 Anthropic 的数） | 要按 provider 调，正解是挂到 LLM 客户端上（同 `context_limit`），不在 utils 堆分支 |
| L12 | 「重复订阅保留游标」在纯协议面**不可观测**（8 个方法里没有推进游标的），SQL provider 上该断言 skip | 补 `advance_subscription(...)`，或让 `recall_topic` 接受订阅身份并落库游标 |
| L13 | `archives_superseded` 声明面未被验证（协议未定义「已归档」的可观测行为） | 先定义可观测行为，再补 conformance |
| L14 / L24 | `fold` 的崩溃原子性未验（单进程内只能验「两个效果同时可见」） | 需进程级用例；当前实现把 supersede 与 replacements 放在同一个 `db.begin()` 内 |
| L17 | 记录 id 命名空间仍全局，且 in_memory 的 `mev_%08d` **可猜** | 先定「record id 全局唯一还是租户内唯一」（协议级），再改 `ingest`/`fold` 的按 id 分支 |
| L25 | `recall_topic` 不比 `session_id`（两实现一致、协议未定），即同租户内跨 session 的同名 topic 共享 | 属**待定语义**，先裁定再改；改动会打到宿主自定的 `long_term_*` topic |
| L29 | 并发 `put` 的 `IntegrityError` 补偿路径未被覆盖（SQLite 串行化，单进程造不出竞态） | postgres 上是真实路径，需集成环境测 |

#### 移交宿主（7 条：L5 / L9 / L18 / L22 / L23 / L27 / L30）

| # | 内容 | 落点 |
|---|---|---|
| L5 | 裸 `OpenAIAdapter` 塞 `ctx.llm` 时 `supports_vision` 恒 False | §6.7 已写明：应在自己返回的 client 上声明该 duck-typed 属性 |
| L9 | `byte_size` 只有经 `normalize_content` 才被填；宿主**直接构造 ref 形态**时恒 None → 回落 1600，预算对该路径失明 | 协议上无强制手段，只能靠文档 |
| L18 | legacy 非协议方法（`recall_recent` 家族 / `supersede`）仍无隔离 | 迁移清单第 7 步：切换前清零调用点。日落时随文件删除 |
| L22 | 切换需先 DROP 旧的 3 列唯一索引 `ix_subscriptions_session_task_topic` | 迁移清单 **2.1**（唯一一条破坏性 DDL） |
| L23 | 存量行 `content_format=NULL` 仍走启发式（正文恰为 JSON 数组的存量纯文本会被误读成 parts） | 迁移清单 **2.4**：建议回填 |
| L27 | `collect_blobs` 不在协议里，宿主必须自己定时调 | 迁移清单 **第 5 步**。不调 = 只涨不删，不产生悬空 ref |
| L30 | blob 表无大小上限/配额（单条 `LargeBinary` 无长度约束） | 要限体积需在入口（`validate_content` 的 `_MAX_IMAGE_BYTES`）或 DB 侧另加 |

#### 明确不做（7 条：L6 / L7 / L11 / L19 / L20 / L26 / L28，附理由）

| # | 内容 | 为什么不做 |
|---|---|---|
| L6 | 占位文案无统一真源：`[图片见后一条消息]`（中）vs `[image {media_type}]`（英） | 两者**各自逐字节确定**即满足缓存约束，正确性无损。Phase 4 若还要加第三条占位，**先收口再加**。**→ 已在 Phase 4 收口（裁定 R1）**：`core/media/refs.py` 的模块 docstring 是全仓占位清单，且是**唯一有解析语义**的那种（L0.5）；另外三种保持在原处（归一层 / wire 层各归其位）并统一成英文，各加一行注释指向清单 |
| L7 | token 估算不知道重定位——多出那条 user 消息的 framing 开销未计 | 偏小几十 token，落在 margin 内；重定位不改张数/字节，总量一致 |
| L11 | 存量 `metadata["token_count"]` 是旧口径写的，对大图整体偏小 | 随 fold/compact 自然淘汰；做数据迁移的收益不抵风险 |
| L19 | `subscribe_topic` 返回的 id 不含 tenant，两租户的两条独立订阅拿到同一个 id 字符串 | 协议没说该 id 全局唯一，仓内无人拿它做键（返回值全被丢弃）。改格式可能打到宿主 |
| L20 | `_topic_seq` 计数器跨租户共享 → A 的 cursor 会因 B 的发布跳号 | **不影响正确性**（过滤在后，A 不会漏读自己的行）。是一条弱侧信道（可推断别的租户在同名 topic 上的活动频次）。要治须把 topic seq 按租户分区，**会改变已有 cursor 的语义** |
| L26 | `BlobStore.get` 不校验 tenant → sha 可探测的存在性侧信道 | 要治只能放弃跨租户去重（blob 表加 tenant 列并进主键），代价是去重失效 + `get` 侧重新出现「第二个租户取不回图」（**图永久丢失**，见 §14.4 第 2 题） |
| L28 | `memory_blob_refs` 里指向已 superseded 事件的边不清理 | 事件永不删除，这些边一直留着（小、且是幂等重跑的正确前提）。宿主若真删事件行，它们会变成悬空边——JOIN 判定下不算活引用，**方向是安全的** |

---

### 14.9 本 Phase 的四条不变量（已逐条动手验证）

1. **纯文本行为逐字节不变**（对比 Phase 3c 起点 `8cc336d`）：把 `8cc336d` 的 `src/`
   导出成第二棵树，同一份 69 项探针（拍扁/token 口径/归一层/三处边界构造/NullBlobStore/
   两家 adapter 的 wire payload（含多 tool_call 段）/in_memory 的 ingest·load_view·
   fold·recall_topic·subscribe·describe）在两棵树上各跑一次 → **输出 md5 相同**。
   探针的敏感性用两个反向变异证伪（去掉「已合规不重建」守卫 → 3 项翻转；
   让无图的 parts 形态 tool 结果也追加标记 → wire payload 翻转）。
2. **不接 SQL memory / 不接 blob 的宿主行为不变**：同一份探针覆盖
   `get_blob_store()` 在「什么都没注册」与「只注册 in_memory」两种接线下的解析结果
   （均为 `NullBlobStore` / `can_externalize=False`），以及 `normalize_content` /
   `rehydrate_content` 在 `NullBlobStore` 下的三条零开销短路。
3. **判据三处仍一致且未被改动**：`_is_text_part` / `image_part_count` /
   `content_to_text` 三个函数体与 `8cc336d` **逐字节相同**（AST 取函数体后比对）。
4. **`FilesystemBlobStore` 已彻底移除**：`src/` 内零命中；`_blob_paths` /
   `_write_blob_if_absent` / `_read_blob` 零命中；`FilesystemToolsProvider` 的基类回到
   三个（`ToolCapabilityProvider` / `SpillSink` / `SessionScopedCapabilityProvider`），
   `hashlib` import 已去。
