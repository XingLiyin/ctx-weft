# Phase 3c 方案——多模态遗留问题收口

> ## ✅ 状态：**已全部落地**（2026-08-27，`8cc336d..65dd606`，十个提交）
>
> 原状态「提案，未执行 / 有 3 处待裁定」已过期。三处决策点均已裁定并实现
> （裁定全文见台账 `.superpowers/sdd/2026-08-26-multimodal-phase3c/progress.md`
> 与 [设计 §14.1](../specs/2026-08-20-multimodal-design.md)）。
>
> | 任务 | commit | 状态 |
> |---|---|---|
> | A HITL 入口校验+外部化 | `ea6a87c` | ✅ |
> | A2 补 A 的三条缺口（简报之误） | `82d8081` | ✅ |
> | E MemoryRecord 边界归一 + 删 adapter dict 死分支 | `72c6a3c` | ✅ |
> | E2 归一抽成共用实现，补齐 `LLMMessage` / `MemoryEvent` | `5f2278c` | ✅ |
> | B OpenAI tool-result 图片重定位 | `76c86cd` | ✅ |
> | D 图片 token 统计体积化 | `1be2296` | ✅ |
> | C1 抽 provider 一致性测试套 | `b645781` | ✅ |
> | 🔴 C1b `load_view` 跨租户泄漏（**计划外，安全**） | `ad403da` | ✅ |
> | C2 SQLite 多模态 memory provider | `f1bb035` | ✅ |
> | C3 blob 并入 + 移除 `FilesystemBlobStore` | `65dd606` | ✅ |
> | C4 spec 收口 + 迁移清单 + 全量回归 | 本次 | ✅ |
>
> **与本方案的实质偏离（三处，均由实现者标出、controller 核实）**：
> 1. **本方案漏了 C1b。** `load_view` 的跨租户泄漏是执行中发现的，且写侧的
>    PUBLICATION 覆盖扫描比它更严重（「能销毁自己读不到的数据」）。见设计 §14.2。
> 2. **A 的「接 `resolve_answer`/`resolve_reject` 一处即可」是错的**——实为三条下层
>    （漏了 `resolve_approve`），且回调签名不能钉成 `(content, session_id)`。见 §14.7。
> 3. **C3 的回收方案有一个悬空 ref 的洞**：只给「从未被引用」的 blob 加宽限期是不够的。
>    改法是 `put` 刷新 `created_at` + 宽限期对两类一视同仁。见 §14.4。
>
> 全量回归 `1978 total / 1970 passed / 3 failed / 5 skipped`。
> **宿主怎么切换：见 [宿主迁移清单](../../host-migration-to-sql-memory.md)。**

分支 `feat/multimodal`，基线 `1706 total / 1700 passed / 3 failed / 3 skipped`
（3 条为既有环境性失败）。

---

## 0. 范围变更：新发现一条，且它比原清单里两条更该先修

移交清单原有 4 条。摸代码时发现**第 5 条，且它是安全/正确性问题，属你最初目标的范围内**：

```
$ grep -rn "validate_content(\|normalize_content(" --include=*.py src/ | grep -v "def \|core/content.py"
src/ctx_weft/core/runtime.py:668:        validate_content(user_prompt, llm=llm)
src/ctx_weft/core/runtime.py:673:        user_prompt = await normalize_content(
src/ctx_weft/core/runtime.py:771:        validate_content(
src/ctx_weft/core/runtime.py:789:                user_prompt=await normalize_content(
```

**只有 2 个入口接了校验与外部化**（`run_single_task` / `start_session`）。
而 HITL 的应答接口在 Phase 1/2 就已放宽成多模态：

```
hitl_manager.py:172   message: "str | list[ContentPart]" = ""
hitl_manager.py:199   message: "str | list[ContentPart]" = ""
hitl_manager.py:301   resolve_answer(..., message: "str | list[ContentPart]" = "")
hitl_manager.py:311   resolve_reject(..., message: "str | list[ContentPart]" = "")
```

**人类通过 HITL 递进来的图，全程不经任何校验、也不外部化。** 后果三条：

1. **无格式校验** —— 畸形 base64 / 超限尺寸 / 白名单外的 media_type 直达 adapter。
   叠加 Phase 3b Task 5 的实测发现（`b64decode` 默认 `validate=False` **不抛**、
   静默解出垃圾字节），这条是**静默损坏**而非响亮失败。
2. **无视觉门控** —— 图片会被发给不支持视觉的模型。Phase 3a 专门建的门控在此路径失效。
3. **不外部化** —— 该图以 inline base64 永久留在 memory 里，Phase 3b 在 HITL 路径上等于没做。

`reopen_task` 我一并查了，**不受影响**：它复用已校验过的 `original_user_prompt` 快照，
经 `content_with_suffix` 追加文本，不引入新的外部内容。

---

## 1. 优先级与排期建议

按「阻塞关系 + 风险」排，不按原清单顺序：

| # | 问题 | 性质 | 建议 |
|---|---|---|---|
| **A** | HITL 入口未校验/未外部化 | 安全 + 正确性，**新发现** | Phase 3c 第一件 |
| **E** | dict 判据盲点 | 正确性根因，且 A/B 都会碰到 part 判形态 | 紧随 A |
| **B** | OpenAI tool-result 图片静默丢失 | **阻塞 Phase 4** | Phase 3c |
| **D** | 单请求总字节无上限 | 可用性 | Phase 3c，需决策 |
| **C** | blob 永不回收 | 运维 | Phase 3c 末，需决策 |

E 放在 A 之后 B 之前是有意的：A 和 B 都要判 part 形态，先把判据修对，
后两件就不必各自绕开盲点。

---

## 2. 各问题的方案

### A. HITL 入口接上校验与外部化

**接线点**：`resolve_answer` / `resolve_reject`（`hitl_manager.py:301,311`）——
它们是 `answer`/`reject` 两个公开方法的共同下层，也覆盖冷应答路径。接在这里一处即可，
不必在每个公开方法各接一遍。

**难点**：`HitlManager` 拿不到 `blob_store`，也拿不到 llm（构造签名只有
`timeout_sec / event_bus / max_resolved / on_cold_resolve`）。

**方案**：沿用该类**已有的注入惯例**。runtime 现在已经用两个 setter 往里注入回调：

```python
self.hitl_manager.set_cold_resolve_handler(self._resume_after_cold_hitl)
self.hitl_manager.set_cold_decision_lookup(self._cold_hitl_decision)
```

加第三个同形态的 setter：

```python
# runtime 侧
self.hitl_manager.set_content_normalizer(self._validate_and_normalize_content)
```

回调签名 `async (content, session_id) -> content`，内部按与两个入口**完全相同的顺序**
跑 `validate_content` → `normalize_content`。这样 `HitlManager` 不必 import
`BlobStore` / LLM 任何类型，保持解耦；未注入时（纯单测）是恒等变换、行为不变。

**顺便消除一个真源分歧**：`runtime.py:668/673` 与 `771/789` 现在各写了一遍
「validate 然后 normalize」。抽成 `_validate_and_normalize_content` 后三处共用，
顺序不会再各自漂移。

**测试要点**：
- HITL 递畸形 base64 → 被拒，且**未落库、未写 blob**（计数器 stub）
- HITL 递图给无视觉模型 → 被拒（与入口行为一致）
- HITL 递合法图 + 真 BlobStore → memory 里是 ref，wire 上是可解码回原始字节的 base64
- 未注入 normalizer 时（单测构造）行为逐字节不变

---

### B. OpenAI tool-result 图片重定位

**现状**（`openai.py` `_serialize_messages`）：

```python
elif m.role == "tool":
    content = m.content if isinstance(m.content, str) else _parts_to_text(m.content)
```

`_parts_to_text` 对图片 `continue` —— **静默丢弃，无占位符**。
Anthropic 侧对照：tool_result 走 `_parts_to_blocks`，**原生支持图片块**。所以这是
OpenAI 单边的能力缺口。

**方案**（即你最初定的「多加一条 user message，放在 openai 适配里」）：

1. 把 OpenAI 的 `role=="tool"` 分支改成**批处理连续 tool 消息**，与 Anthropic 侧
   `while i < len(messages) and messages[i].role == "tool"` 同构。
2. 每条 tool 消息仍只发文本；**若它含图，文本尾部追加确定性标记**
   （形如 `\n\n[图片见后一条消息]`）。
3. **整个连续 tool 段结束后**，把这一段收集到的图片合并成**一条**
   `{"role": "user", "content": [<image blocks>]}` 追加。

**第 3 步的"段结束后"是硬约束，不是风格选择**：OpenAI 要求 assistant 的每个
`tool_calls` 都由紧随其后的 `tool` 消息应答。若在 tool 消息之间插入 user 消息，
配对被打断 → 400。所以必须攒到整段结束再 flush。

**视觉门控**：模型 `supports_vision` 为 False 时**不重定位**，改用 Task 4 已有的
`downgrade_images_to_text` 收成 `[image {media_type}]` 文本占位。复用现成函数，
不新写一套。

**确定性**：标记文本与占位文本都必须逐字节固定（同裁定 D2 的理由——否则砸缓存前缀）。

**测试要点**：
- tool 结果含图 → wire 上 tool 消息为文本 + 其后**恰有一条** user 消息载图
- **多个连续 tool 消息各含图** → 只产出一条合并的 user 消息，且位置在整段之后
  （这条是上面那个硬约束的守卫，必须有）
- 无视觉模型 → 不产生额外 user 消息，退化为文本占位
- 纯文本 tool 结果 → wire 逐字节不变
- Anthropic 侧不受影响（回归）

---

### E. dict 形态内容 —— 在边界归一，**不动判据**（裁定已改）

**先纠正我原方案的错误。** 我原来提「解冻判据、三处原子替换」，那是错的——
等于给协议违规兜底，把约束往下腐蚀。用户指出这本质是适配边界的问题，核实后确认：

`protocols/memory.py` 已经把话说死了：

```python
content: str | list[ContentPart]     # 类型声明就是 dataclass
```

契约第 2 条「召回时原样返回——形态必与入库时相同」；只有第 3 条的**序列化机制**是"推荐"。
**所以返回 dict 的 provider 是在违反协议**——在声明为 `list[ContentPart]` 的位置
返回了 `list[dict]`。判据对合规 provider 完全正确，adapter 里那些
`isinstance(p, dict)` 分支是在给违规做防御。

**方案：`MemoryRecord.__post_init__` 归一。**

`MemoryRecord` 是纯 `@dataclass`、目前**没有** `__post_init__`（`protocols/memory.py:201`）。
加一个，把 dict 形态的 content 还原成 dataclass（复用 `content_from_jsonable`）。

为什么落在类型上而不是包装 provider：core 读 memory 有 **11 个调用点**
（`act` / `compact` / `finalize` / `prepare` / `reconcile` / `segment_fold` /
`background_observe` / `runtime` / `agent_recall` / `blackboard` / `long_memory`），
逐个包装既漏又会随新调用点腐化。而**任何 provider 都必须构造 `MemoryRecord`**，
所以类型自己把住 = 结构性覆盖，含所有未来的第三方 provider。

`reducers.py` 在事件重放侧已经是同一套做法（`content_from_jsonable`），照抄即可。

**连带收益**：dict 在 core 内部结构性消失后，
`anthropic.py:413,436` 与 `openai.py:422,445` 的 dict 分支成为死代码。
**本任务顺手删掉**——留着会让后来者以为 dict 是受支持的形态。

**性能**：`__post_init__` 在热路径上。快路径 `isinstance(content, str)` 直接返回，
覆盖绝大多数记录；list 形态的扫描对 1-3 个 part 可忽略。要在报告中实测确认无回归。

**测试要点**：
- provider 返回 dict 形态 content → `MemoryRecord` 构造后即为 dataclass
- 纯文本记录逐字节不变、且**不产生额外开销**（快路径）
- 归一后 `image_part_count` / `content_to_text` 对文本 part 结果正确
  （即 Task 4 实测的那两个坏值消失）
- adapter dict 分支删除后全量回归不变

### D. 图片 token 统计 —— 改成体积相关，让 prepare 报错（裁定已改）

**先纠正我原方案。** 我原来提「composer 按张数设上限」，那是外挂第二套机制。
用户裁定：**根因是图片的 token 统计完全失效，应当修统计本身，让超限在 prepare 报错。**

**现状**：

```python
def image_tokens(content): return _IMAGE_PART_TOKENS * image_part_count(content)   # 1600，平的
```

5MB 截图与 50KB 缩略图都算 1600——**对唯一真正变化的维度（体积）毫无反应**。

**改成体积相关后，整条链自愈**，不需要外挂机制：

```
图变多/变大 → token 估算上升 → 触发 compact → fold 掉旧记录
           → superseded → load_view 不再返回 → act 不再重发那些图 → 请求缩小
```

地板仍超限时 `budget.py:91` 已经抛 `ContextOverflowError`，
**且已带 `image_count` 字段**，spec §6.5 也已写明此时「用户该做的是删图而非删字」——
机器全都在，只是喂给它的数字是坏的。

**一个必须在 spec 里写明的取舍**：模型侧真实 token 成本其实是封顶的
（provider 会降采样，一张图大约就是 1600），所以按字节折算在**计费口径**上不准。
但预算机制的职责是「判断这个请求能不能发出去」，而那由**字节**决定。
让估算建模真正约束的维度是对的——**但必须写明它建模的是字节压力而非计费 token**，
否则后来者会拿它去算成本。

**⚠️ 决策点 D3：换算系数**（实现时定，需在报告中给出依据）
建议以「单图上限 5MB / base64 后约 6.7MB」为锚，标定成
**若干张满额图即可超出典型预算**。不要拍脑袋，给出计算过程。

**测试要点**：
- 大图的 `image_tokens` 显著高于小图（钉住"不再是平的"）
- 纯文本路径 `image_tokens` 恒为 0（既有不变量，不可破）
- 累积大图 → 触发 compact → fold 后 act 装配的图片数下降（自愈链的端到端证明）
- 地板超限 → `ContextOverflowError` 且 `image_count` 正确

### C. blob 生命周期归 memory —— C-merge（按你的裁定）

**裁定**：blob 整合进 memory 机制（字节也一并），理由是 core 不该背这件事，
留给业务实现方。落地手段是把宿主侧那份 SQLAlchemy memory provider 挪进来。

#### 依据：memory 本来就是图片字节的唯一持有者

`act.py:222` / `observe.py:122` 都走 `redact_content_for_event`，事件里存的是
`[image image/png ref:blob:deadbe…]` 这样的短标记，**从不存字节**。
所以事件库在任何情况下都重建不出一张图——所有权归 memory 不是权宜。

#### 现成的软删语义：`fold`

`fold` 标 `is_superseded = True` 而非移除，`load_view` 按 `not is_superseded` 过滤。
墓碑齐备（SQL 侧同样有 `is_superseded` 列），且 fold **已经是原子的**（单事务）。

#### 落地方式：core 仓新写，不做跨仓搬迁（按你的裁定）

**不搬**宿主那份文件，而是在 ctx-weft 里**新实现一个 SQLite 多模态 memory provider**；
宿主后续切过来用，各自按节奏走。这样原方案里最大的一块工程摩擦
（`MemoryEventModel` 与宿主事件库共用同一个 `Base`、波及 `session_export/import`）
**整个不存在了**——宿主的 event model 反正在它自己做多模态时也要改。

参考实现：`IpMasterCoworkPy/src/ipmastercowork/providers/memory/postgres.py`（462 行）。
已核实它**是对着当前 ctx-weft 协议写的**（`from ctx_weft.core.utils import generate_id`），
v2 全部 8 个协议方法齐备 —— 是很扎实的起点，但我们是照着它重写，不是复制粘贴。

> Loome-01 下那份同名文件 import 的是 `ctx_wefta`，是旧的，**别参考那份**。

**⚠️ 「平滑切换」给新 provider 加了一条硬要求**：宿主切过来时，
**必须能直接读它已有的 `memory_events` 存量行**。否则不叫平滑切换，叫数据迁移。

这条要求直接决定了两件事：

1. **表名与既有列必须保持一致**（`memory_events` / `memory_subscriptions`，
   列名列型照旧），新增列一律 nullable。
2. **用 SQLAlchemy，不要用裸 aiosqlite。** 参考实现的 docstring 写明
   「postgres/sqlite 天然满足」，即同一份代码两种后端。宿主实际跑哪个后端我没核实，
   走 SQLAlchemy 则两种都保得住，切换不受后端影响。默认与测试都用 SQLite。

依赖上无障碍：ctx-weft 现为 4 个核心依赖 + 按能力分 extras
（`builtin` / `mcp` / `skills` / `llm`），加一个同形态的 extra：

```toml
sql = ["sqlalchemy>=2.0", "aiosqlite>=0.19"]
```

#### 表结构改动

**(1) content 列**：现为 `content: Mapped[str] = mapped_column(Text)`，存纯字符串。
多模态要存 `content_to_jsonable` 的 JSON。

**不要就地改语义**——加一个判别列。这不是「顺手做的向后兼容」，
而是上面那条平滑切换要求的**直接推论**：宿主的存量行全是纯文本，
新 provider 必须原样读得懂它们。

```python
content_format: Mapped[str | None] = mapped_column(String(16), nullable=True)
# None / "text" → content 是纯文本（全部存量行）
# "parts"       → content 是 content_to_jsonable 的 JSON
```

这与该 provider **已有的设计取向一致**——它的「词汇双读」就是靠读侧归一实现零数据迁移的。

**(2) blob 表**（C-merge 的字节落点）：

```python
class MemoryBlobModel(Base):
    __tablename__ = "memory_blobs"
    sha: Mapped[str]        = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes]     = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**(3) 引用索引表**（回收的关键）：

```python
class MemoryBlobRefModel(Base):
    __tablename__ = "memory_blob_refs"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sha: Mapped[str]      = mapped_column(String(64), primary_key=True, index=True)
```

在 `ingest` 的**同一个事务**里写入。这就是 spec §5.2 当初设想的 side index，
但现在它长在 memory 内部——所以能与 ingest/fold 事务性地一致，这正是原方案做不到的。

#### 回收查询

有了引用表，回收不需要在 SQL 里解 JSON，也不需要 store 提供枚举能力：

```sql
DELETE FROM memory_blobs WHERE sha NOT IN (
    SELECT r.sha FROM memory_blob_refs r
    JOIN memory_events e ON e.id = r.event_id
    WHERE e.is_superseded = 0
)
```

内容寻址的去重天然被覆盖：一张图被三条记录引用、其中两条 fold 掉，
它仍出现在活引用里，不会被删。跨会话继承同理——`_copy_memory_for_inherit`
给子会话 ingest 的是含相同 ref 的**新记录**，那条新记录是活的。

#### ⚠️ 一个必须处理的时序风险（我原方案里没料到）

**`put` 发生在 ingest 之前。** 入口的顺序是
`validate → normalize(put 字节，拿 ref) → …一路往下… → 最终 ingest 进 memory`。
所以存在一个窗口：**blob 行已写入，但还没有任何 event 行引用它**。
上面那条回收查询会把它判为 dead 并删掉——图在还没用上时就没了。

而且若会话在这个窗口内失败，该 blob 永远不会被引用，成为真正的孤儿。

**方案**：回收查询区分两类，且都偏保守（**宁可漏删，不可误删**）：

1. **在引用表里、但无活引用** → 删（就是上面那条查询）
2. **不在引用表里**（从未被任何 event 引用）→ **仅当超过宽限期才删**
   （例如 created_at 早于 N 小时）。这类是失败会话的孤儿。

宽限期这一条不是可选的优化，是上述时序窗口的**正确性要求**。

#### 纯内存 provider：不支持多模态（按你的裁定）

`memory_blackboard/in_memory.py` 不实现 blob 存储 → `can_externalize` 为 `False`
→ 走 Phase 3b Task 2 已建的「原样返回、图片保持 inline base64」路径。

**这条路径已经实现且已被测试覆盖**（Task 2 的 `NullBlobStore` 原样返回 + 变异验证），
所以你说的「刚好测试两种模式的兼容性」几乎是免费的——只需补一组对照测试，
证明同一个会话在两种 memory provider 下都能跑通、只是一个外部化一个不外部化。

#### 顺带补上一个缺失的结构：面向协议的一致性测试套

现状：`tests/unit/test_memory_layers.py` 等**直接 `InMemoryMemoryProvider()` 实例化**，
绑死实现。所以新 provider 没有现成的验收手段。

建议抽一套 **parametrize 到 provider 的一致性测试**，两个实现同跑：

```python
@pytest.fixture(params=["in_memory", "sqlite"])
def memory(request): ...
```

这一步有三重收益，性价比很高：
- 新 provider 的「是不是真的可替换」有了结构性保证，而不是逐个手搓测试
- **你要的「两种模式兼容性对照」自然落在这套里**，不必另写
- 存量测试里那些实际在验协议行为的用例可以迁进来，减少绑死

多模态相关的差异用 marker 区分：SQLite 版外部化成 ref，纯内存版保持 inline base64
（走 `can_externalize=False` 的既有路径），**两者都必须能跑通同一个会话**。

#### ✅ 已裁定：移除现有的 `FilesystemBlobStore`

Phase 3b Task 1 把 `BlobStore` 实现在了 `FilesystemToolsProvider` 上。
C-merge 之后 blob 归 memory，这份实现就重复了。

**裁定：移除。** 理由：两个 blob 实现、且生命周期语义不同（一个有回收、一个永不删），
正是最容易出错的那种歧义——而且留着它就等于把「永不回收」这条继续留在门里。
`ProviderRegistry.get_blob_store()` 改为解析到 memory provider（若它实现了 BlobStore），
否则回落 `NullBlobStore`。

代价是回退 Phase 3b Task 1 的实现。**测试留下、改挂到 SQL provider 上**——
那 9 条测试（含 Windows UNC 外连护栏那条）验的是 BlobStore 契约本身，与实现无关。

#### ⚠️ 迁移摩擦：模型文件是与宿主共用的

`MemoryEventModel` 现在住在
`IpMasterCoworkPy/src/ipmastercowork/persistence/postgres/models.py`，
**与 `SessionModel` / `TaskModel` / `EventModel` 共用同一个 `Base`**，
而后三者是宿主的事件库、不属于 memory，不该跟着挪。

拆开意味着两个 `Base` 注册表（ctx-weft 一个、宿主一个），
`create_all` 要各建各的；宿主的 `observability/session_export.py`
与 `session_import.py` 都直接 import 了 `MemoryEventModel`，改动会波及它们。

这是本项里最大的一块工程摩擦，**不在 ctx-weft 仓内**，需要与宿主侧协同改。
建议把它单列一个任务，并且**先做只读核对**（列清楚宿主侧哪些文件会被波及）
再动手。

## 3. F —— 已裁定：维持现状，observe 不看图

**裁定原话**：「observe 不需要图片，observe 的作用是看 actor 有没有干活，
不是替代 actor 决策。」

这条要写进 spec 作为**裁定理由**而不只是结论，否则「observer 看不到图」会被
反复重提。职责边界：observe 是执行性检查（有没有干活），不是内容性仲裁
（干得对不对）。后者是 actor 自己和用户的事。

`_IMAGE_BEARING_PURPOSES = frozenset({"act"})` 保持不变。

## 4. 执行方式

沿用 Phase 3b 的 subagent-driven 流程：任务简报 → 实现 → 变异验证 → controller 评审 → 台账。
仍然绑定那几条硬规则：禁 `git stash`、禁 `git add -A`（本仓有并发会话）、
全量测试单跑不并发、精确计数走 `--junit-xml`（本环境不输出计数行）。

**建议拆 6 个任务**：A / E / B / D / C / spec 收口。
E 若你决定推迟，A 与 B 各自加一个绕开盲点的局部守卫即可，代价不大。

---

## 5. 已裁定汇总

| 项 | 裁定 |
|---|---|
| C（回收） | 整合进 memory（C-merge）；core 仓新写 SQLite 多模态 provider，宿主后续平滑切换 |
| C2 | 移除 `FilesystemBlobStore`，测试改挂 SQL provider |
| D | 修图片 token 统计本身（体积相关），让超限在 prepare 报错；不外挂张数上限 |
| E | `MemoryRecord.__post_init__` 边界归一；**判据不解冻**；顺手删 adapter dict 死分支 |
| F | 维持现状，observe 不看图；裁定理由写进 spec |
| 顺序 | **A → E → B → D → C** |

## 6. 任务拆分

| # | 任务 | 说明 |
|---|---|---|
| 1 | **A** HITL 入口校验+外部化 | 抽 `_validate_and_normalize_content` 三处共用 |
| 2 | **E** MemoryRecord 边界归一 | + 删 adapter dict 死分支 |
| 3 | **B** OpenAI tool-result 图片重定位 | 批处理 + 段末 flush（硬约束） |
| 4 | **D** 图片 token 统计体积化 | 含换算系数标定依据 |
| 5 | **C1** 抽 provider 一致性测试套 | parametrize，先于新 provider |
| 6 | **C2** SQLite 多模态 memory provider | 表名列名兼容宿主存量行 |
| 7 | **C3** blob 并入 + 移除 FilesystemBlobStore | 含宽限期回收 |
| 8 | **C4** spec 收口 + 全量回归 | |
