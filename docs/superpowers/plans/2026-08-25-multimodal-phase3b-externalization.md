# 多模态 Phase 3b：外部化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把图片从 memory / 事件 / 装配链里挪出去——core 全程只搬几十字节的 ref，只在出网前最后一刻还原成 base64。顺带修掉「compaction 超预算时重发所有图片」。

**Architecture:** 入口 `normalize_content` 把 base64 经 `BlobStore.put` 外部化成 `source_type="ref"`；`FilesystemToolsProvider` 新增 `BlobStore` 实现（内容寻址、sha256）；`llm_gateway.stream_llm` 在出网前 rehydrate；`composer` 按 purpose 决定非 act 场景降级成文本占位。

**Tech Stack:** Python 3.11+，pytest + pytest-asyncio，`uv run pytest`。

**Spec:** `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§3③、§5、§6.7、§10 Phase 3、§13）

**Prior phases:** Phase 0（`e8cb02c`）、Phase 1（`65978b7`）、Phase 2（`69e416d`）、Phase 3a（`c9fa1c6`）。

---

## 架构裁定（Ruling T0，写计划时确定，实现者照做）

spec §3③ 原文是「只有 LLM adapter 拼 wire payload 时才换回 base64」。**这条在字面上做不到**，controller 已核实：

- `_build_payload` / `_serialize_messages` / `_parts_to_blocks` **全是同步函数**
- `BlobStore.get()` 是 **async**

同步函数里没法 await。因此 rehydrate 落在 **`llm_gateway.stream_llm`**（`llm_gateway.py:377`）——它是 async、是出网前最后一站、已经在跑 `legalize_messages`，且**一处改动三家 adapter 都受益**（放 adapter 里要写三遍）。

这偏离了 §3③ 的字面表述（gateway 属 core），但**保住了它的实质**：core 的 memory / 事件 / 装配链全程只见 ref，只在出网前最后一刻还原。

第二条裁定：**per-purpose 策略不能放 gateway**——`LLMRequest` 没有 `purpose` 字段（已核实）。但 composer **知道** purpose。所以：

| 关注点 | 落点 |
|---|---|
| per-purpose 降级（compact / recognize_intent 不带图） | **composer** |
| ref → base64 rehydrate | **llm_gateway** |

---

## Global Constraints

- **纯文本行为逐字节不变。** 每个任务都要有断言证明。
- **不接 `BlobStore` 时行为完全不变。** 未注册 blob store 的宿主必须与 Phase 3a 结束时**逐字节一致**——图片仍以 inline base64 走全链路。这是本 Phase 最重要的兼容性约束。
- **`normalize_content` 不得 try/except `NullBlobStore.put` 的 `NotImplementedError`**（Phase 1 终审留下的契约）。那会把「响亮失败」变成控制流、抵消其设计意图。**必须先探询 store**（`isinstance(store, NullBlobStore)`，或给 `BlobStore` 加一个 `can_externalize` 属性）再决定是否外部化。
- **非文本判据 `not hasattr(p, "text")` 仍冻结**（spec §13）。若本 Phase 确需让归一层认识 dict，**必须同时**修 `core/utils.py` 的 `content_to_text` / `image_part_count`——三者共用同一字面量，只改一处会制造新分歧。**默认不改**；确需改时先在报告中论证。
- 摘要恒为纯文本（spec §8）；`_history.py` 三个包装器不得动。
- 不得新增 PytestWarning；**不得用模块级 `pytestmark`**。
- **绝对禁止 `git stash`**（本分支已因此误弹过用户其它分支的 stash）。对比旧版用 `git checkout <sha> -- src/`，跑完 `git checkout HEAD -- src/` 复原。
- 测试运行器 `uv run pytest`；本环境 `-q` 配合大量 warning 时**不输出终结汇总行**，用 `-v` 或不带 `-q`。
- **跑全量单跑、勿并发**——本仓 `test_bash_exec_liveness` / `test_script_runner` 等 subprocess 类测试对资源竞争敏感，**已三次观察到并发时报出 5-8 条额外失败、单跑则全绿**。
- 全量基线：`3 failed / 1616 passed / 3 skipped`。三条为既有环境失败（`test_compact_flow_e2e::test_multiround_retry_accumulates_then_l3_collapses_e2e`、`test_golden_conformance::test_golden_dir_present`、`test_observe_outcomes::test_default_role_prompt_uses_two_fields`），**不要试图修**。出现第四条即为本 Phase 引入。
- **⚠️ 测试写法陷阱**：`assert any(not hasattr(p, "text") for p in content)` 在 `content` 是 `str` 时**恒为 True**（字符串每个字符都没有 `.text`），是重言式。凡断言「图片存活」必须先钉 `assert isinstance(content, list)` 或用完整相等。本分支已四次因此返工。

## 读取方枚举（Phase 1 教训的应用）

本 Phase 让 `ImagePart.source_type == "ref"` 首次成为**可产出**的形态。凡是读 `ImagePart.data` 的地方，都必须问一句「拿到 ref 会怎样」：

| 读取点 | 处理 |
|---|---|
| `anthropic._parts_to_blocks`（`data` → wire） | **Task 3 之后拿到的必是 base64**（gateway 已 rehydrate）。不改 |
| `openai._parts_to_blocks`（`data` → `data:` URL） | 同上。不改 |
| `content.validate_content` 的 base64 解码 | **Task 2 改**——ref 形态跳过解码/尺寸校验（已在 blob 落库时校验过） |
| `content.redact_content_for_event` | 已用 `data[:12]`，ref 更短，天然安全。不改 |
| `utils.image_tokens` / `image_part_count` | 只数个数、不读 `data`。不改 |
| `content_to_jsonable` / `from_jsonable` | 已带 `source_type` 往返（Phase 1）。不改 |

**字符串操作扫描**：全仓 grep `\.data\.` / `\.data\[` —— 实现者须自行跑一遍确认无遗漏，结果写进 Task 1 报告。

---

### Task 1: `FilesystemBlobStore` —— 真的 BlobStore 实现

**Files:**
- Modify: `src/ctx_weft/providers/capability_filesystem/provider.py`
- Test: `tests/unit/test_filesystem_blob_store.py`

**Interfaces:**
- Produces: `FilesystemToolsProvider` 新增实现 `BlobStore`——`put(data, media_type, ctx) -> str` / `get(ref, ctx) -> tuple[bytes, str] | None`

**为什么落在这里：** `FilesystemToolsProvider` 已经实现三个面向 core 的契约（`ToolCapabilityProvider` / `SpillSink` / `SessionScopedCapabilityProvider`，见 `provider.py:585`），并持有 per-session workspace（`workspace_for(ctx)`，`provider.py:634`）。`SpillSink.spill()` 就是「拿 workspace → 写文件 → 返回路径」，`BlobStore` 是同一形状。复用同一套登记与清理机制。

**契约要点（spec §5.1/§5.2）：**
- `put` **必须内容寻址且幂等**：同样的 `data` 返回同样的 ref，重复调用不重复存。用 `sha256`。这给到三件事：写入端去重、重放安全、**rehydrate 字节稳定**（同一 ref 每次还原出的 base64 完全一致，Anthropic 的 prompt cache 前缀不会被打碎）
- `get` 对不存在 / 已回收的 ref **返回 `None`，不得 raise**——blob 过期、宿主换机、GC 误删都会发生
- ref 形态：`f"{BLOB_REF_PREFIX}{sha256}"`（`BLOB_REF_PREFIX = "blob:"` 已在 `protocols/filesystem.py` 定义）
- 落盘布局：`<workspace>/blobs/<sha[:2]>/<sha[2:4]>/<sha>`（两级分目录，避免单目录几万文件）；`media_type` 与本体一起存（例如同目录的 `<sha>.meta`，或文件名带扩展）——`get` 要能返回它

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_filesystem_blob_store.py`。先读 `tests/unit/` 下既有的 filesystem provider 测试，**复用它们建 workspace 的 fixture 惯例**（`tmp_path` + `register_workspace` 之类）。覆盖：

1. `put` 返回的 ref 以 `blob:` 开头
2. **内容寻址**：同样的 bytes 两次 `put` 返回**同一个 ref**
3. **幂等**：两次 `put` 后磁盘上只有一份文件
4. `get(ref)` 返回 `(原始 bytes, media_type)`
5. `get("blob:nonexistent")` 返回 `None`（**不抛**）
6. 未登记 workspace 时 `put` 抛（与 `spill` 的既有行为一致）
7. 不同 `media_type` 但相同 bytes：ref 相同（内容寻址只看内容）——**同时断言 `get` 返回的 media_type 是哪一个**，并在实现里明确这个语义（先写入者胜 / 后写入者覆盖，你定，但要一致且有注释）

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_filesystem_blob_store.py -v`
Expected: FAIL（`FilesystemToolsProvider` 尚无 `put` / `get`）

- [ ] **Step 3: 实现**

`FilesystemToolsProvider` 的类声明加 `BlobStore`：

```python
class FilesystemToolsProvider(ToolCapabilityProvider, SpillSink, BlobStore,
                              SessionScopedCapabilityProvider):
```

实现两个方法，**参照 `spill` 的既有形态**（`provider.py:638-650`）：workspace 缺失时 raise、写文件用 `asyncio.to_thread`（不要在事件循环里同步 IO）。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_filesystem_blob_store.py -v`
Expected: 全部 passed

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "filesystem or spill or provider" -v`
Expected: 全部 PASS

- [ ] **Step 6: 跑读取方扫描并记录**

```bash
grep -rn "\.data\." --include="*.py" src/ | grep -v "\.data\.get"
grep -rn "\.data\[" --include="*.py" src/
```
把结果写进报告——确认除计划表格里列出的之外，无其它读 `ImagePart.data` 的地方。

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/providers/capability_filesystem/provider.py tests/unit/test_filesystem_blob_store.py
git commit -m "feat(filesystem): BlobStore 实现——内容寻址、幂等、get 不抛"
```

---

### Task 2: `normalize_content` —— 入口外部化

**Files:**
- Modify: `src/ctx_weft/core/content.py`
- Modify: `src/ctx_weft/protocols/filesystem.py`（`BlobStore.can_externalize`，见下）
- Modify: `src/ctx_weft/core/runtime.py`（两个入口）
- Test: `tests/unit/test_normalize_content.py`

**Interfaces:**
- Consumes: Task 1 的 `BlobStore`
- Produces: `async def normalize_content(content, *, blob_store, ctx) -> str | list[ContentPart]`

**⚠️ Phase 1 终审留下的硬性契约：**

> `normalize_content` **不得** try/except `NullBlobStore.put` 的 `NotImplementedError`——那会把"响亮失败"变成控制流、抵消其设计意图。必须先探询 store 再决定是否外部化。

所以给 `BlobStore` 加一个可探询的属性：

```python
class BlobStore(ABC):
    @property
    def can_externalize(self) -> bool:
        """本 store 是否真的能存——`NullBlobStore` 返回 False。

        调用方据此**先探询、再决定**，而不是调用 put 并捕获 NotImplementedError：
        后者会把「响亮失败」降级成控制流，让真正的接线错误也被静默吞掉。
        """
        return True
```

`NullBlobStore` 覆写为 `return False`。`put` 的 `raise NotImplementedError` **保持不动**——它仍是接线错误的响亮信号。

`normalize_content` 的语义：

```python
async def normalize_content(content, *, blob_store, ctx):
    """把 base64 图片外部化成 ref。返回新内容；不改原对象。

    blob_store 不能外部化（NullBlobStore）时**原样返回**——不接 blob 的宿主
    行为与 Phase 3a 逐字节一致。
    """
    if not content or isinstance(content, str):
        return content
    if not blob_store.can_externalize:
        return content                     # 原样返回，零改动
    out: list[ContentPart] = []
    for part in content:
        if _is_text_part(part) or getattr(part, "source_type", "base64") != "base64":
            out.append(part)               # 文本 / url / 已是 ref → 原样，不重复外部化
            continue
        raw = base64.b64decode(getattr(part, "data", "") or "", validate=True)
        ref = await blob_store.put(raw, getattr(part, "media_type", ""), ctx)
        out.append(dataclasses.replace(part, data=ref, source_type="ref"))
    return out
```

**注意 `b64decode` 这里不再需要 try/except**——`validate_content` 已在本函数之前跑过（入口顺序见下），畸形 base64 到不了这里。若你认为仍需防御，**先说明什么路径能绕过 `validate_content` 到达此处**，再加。

**逐条要求：**
- 只处理 `source_type == "base64"` 的 `ImagePart`；`url` / `ref` 原样保留
- 外部化后产出 `ImagePart(data=<ref>, media_type=<原值>, source_type="ref")`
- **不改原列表**（返回新列表，与 `content_with_prefix` 的既有约定一致）
- 纯文本 / `None` / 全 `TextPart` → 原样返回，**不碰 blob store**

**M5 的 ref 处理**：`validate_content` 当前对 `source_type != "base64"` 是 `raise`（Phase 3a 的 M5 刻意为之，强制本 Phase 显式处理）。改为：`ref` 形态**跳过** base64 解码与尺寸校验（那些在 `put` 时已经做过），但**仍校验 media_type 白名单**。`url` 形态维持 raise（本 Phase 不支持）。

**入口接线顺序（关键）**：`validate_content` **必须在** `normalize_content` **之前**——先拒绝畸形/超限/无视觉能力的内容，再花代价写 blob。顺序反了会让被拒的内容也在 blob store 里留下垃圾。

- [ ] **Step 1-5: RED → GREEN**

测试覆盖（新建 `tests/unit/test_normalize_content.py`）：
1. `NullBlobStore` → 内容**原样返回**（`is` 同一对象或完全相等，且 blob store 未被调用——用计数器 stub）
2. 真 store → `ImagePart` 变成 `source_type="ref"`、`data` 以 `blob:` 开头
3. 原列表未被修改
4. 纯文本 / `None` / 全 `TextPart` → 原样返回且**未调用 blob store**（计数器断言）
5. `url` / 已是 `ref` 的 part → 原样保留、不重复外部化
6. `validate_content` 对 `ref` 形态：跳过解码/尺寸、仍校验 media_type
7. 入口顺序：畸形内容被拒时 **blob store 未被写入**（计数器断言）

**每条都要能真的红。** 尤其第 1、4、7 条用计数器 stub——它们断言的是"没有发生某件事"，最容易写成永真。

- [ ] **Step 6: 变异验证**

至少对第 1 条（`NullBlobStore` 原样返回）与第 7 条（拒绝时不写 blob）做变异验证：撤销对应逻辑 → 确认转红 → 复原。逐字证据进报告。

- [ ] **Step 7: 回归 + 提交**

Run: `uv run pytest tests/unit -k "content or normalize or runtime or session" -v`

```bash
git commit -m "feat(content): normalize_content 入口外部化，先探询 store 再决定"
```

---

### Task 3: gateway rehydrate

**Files:**
- Modify: `src/ctx_weft/core/loop/llm_gateway.py`（`stream_llm`）
- Modify: `src/ctx_weft/core/content.py`（`rehydrate_content`）
- Test: `tests/unit/test_gateway_rehydrate.py`

**Interfaces:**
- Consumes: Task 1 的 `BlobStore.get`
- Produces: `async def rehydrate_content(content, *, blob_store, ctx) -> str | list[ContentPart]`；`stream_llm` 在 `legalize_messages` 之后、`llm.complete` 之前调用它

**见架构裁定 T0：** rehydrate 落在 gateway 而非 adapter，因为 adapter 的序列化链是同步的、而 `BlobStore.get` 是 async。

**逐条要求：**
- `source_type == "ref"` 的 `ImagePart` → `get(ref)` → 换回 `ImagePart(data=<base64>, media_type=..., source_type="base64")`
- **`get` 返回 `None` 时降级、不抛**（spec §5.1）：换成 `TextPart(text=f"[image unavailable: {media_type}]")`。blob 过期 / 宿主换机 / GC 误删都会发生，**绝不能因取图失败中断 loop**
- 无 blob store（`NullBlobStore`）或内容里没有 ref → **原样返回，零开销**
- 纯文本消息完全不受影响

**`stream_llm` 的接线（controller 已核实，照此做）：**

现状（`llm_gateway.py:377-383`）：

```python
async def stream_llm(llm, request, *, stream: bool = True):
    request.messages = legalize_messages(request.messages)
    async for chunk in llm.complete(request, stream=stream):
        yield chunk
```

**它拿不到 blob store。** `LoopContext`（`driver.py:109-133`）也没有该字段——它有 `assembler` / `llm` / `memory` / `event_bus` / `provider_ctx` / `capability_gateway` 等，但无 blob store。

两个调用方**都有 `ctx`**：
- `llm_gateway.py:476`（在 `stream_llm_resilient(ctx, state, request)` 内）
- `recognize_intent.py:142`（`stream_llm(ctx.llm, llm_request)`，函数内有 `ctx`）

所以接线是两步：

**(a)** `LoopContext` 新增字段（**必须有默认值** `None`，保证既有构造点全不受影响）：

```python
    # blob store：出网前 rehydrate ref→base64 用（Phase 3b）。None → 不 rehydrate。
    blob_store: "BlobStore | None" = None
```

由 `runtime._build_loop_ctx` 从 `self.providers.get_blob_store()` 注入——**照它注入 `memory` 的同一形态**。

**(b)** `stream_llm` 加两个**带默认值**的关键字参数：

```python
async def stream_llm(llm, request, *, stream: bool = True,
                     blob_store=None, provider_ctx=None):
    request.messages = legalize_messages(request.messages)
    if blob_store is not None:
        request.messages = [
            dataclasses.replace(m, content=await rehydrate_content(
                m.content, blob_store=blob_store, ctx=provider_ctx))
            for m in request.messages
        ]
    async for chunk in llm.complete(request, stream=stream):
        yield chunk
```

两个调用方各传 `blob_store=ctx.blob_store, provider_ctx=ctx.provider_ctx`。

默认 `None` 保证：任何其它调用方 / 既有测试**行为完全不变**。**不要硬塞全局状态或模块级单例。**

- [ ] **Step 1-5: RED → GREEN**

测试覆盖：
1. 含 ref 的消息经 `stream_llm` 后，adapter 收到的是 **base64** 形态
2. `get` 返回 `None` → 降级成 `[image unavailable: ...]` 文本，**不抛**，loop 继续
3. `NullBlobStore` → 消息**原样**、`get` 未被调用（计数器）
4. 纯文本消息 → 逐字节不变、`get` 未被调用
5. **rehydrate 字节稳定**：同一 ref 两次 rehydrate 产出**完全相同**的 base64（这是 prompt cache 前缀稳定的前提）

- [ ] **Step 6: 变异验证 + 回归 + 提交**

对第 2 条（`None` 降级不抛）做变异验证——把降级改成抛，确认测试转红。

```bash
git commit -m "feat(gateway): 出网前 rehydrate ref→base64，取不到则降级不抛"
```

---

### Task 4: per-purpose 降级（兑现 Phase 2 的遗留义务）

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`
- Test: `tests/unit/test_purpose_image_policy.py`

**Interfaces:**
- Produces: 非 `act` 的 compose purpose 不再携带图片

**背景（Phase 2 终审的 I3）：** 探针实测五个 compose purpose（`act` / `compact` / `observe` / `recognize_intent` / `background_observe`）**全部**携带 inline base64。后果之一：**compaction 恰在上下文超预算时触发，而它会重发所有图片**——在最贵的时刻多打一发最大的请求。

**策略（controller 裁定）：**

| purpose | 带图？ | 理由 |
|---|---|---|
| `act` | **是** | 模型要真看图 |
| `compact` | **否** | 恰在超预算时触发；产出按 spec §8 恒为纯文本 |
| `recognize_intent` | **否** | 填元数据，几乎不需要看图 |
| `observe` / `background_observe` | **否** | 判任务成败，依据主要是 actor 产出与工具结果；同样是超预算时的高频调用 |

降级形态：`ImagePart` → `TextPart(text="[image {media_type}]")`，**不是直接删掉**——保留"这里曾有一张图"的信息，摘要器才能写出"用户提供了一张图"而不是完全无感。

**实现位置（controller 已核实）**：`composer.py:429` 的 `return AssembledPrompt(...)` 是**所有 purpose 的唯一出口**——五条分支最终都汇到这一处。在它之前对 `messages` 做一次统一降级即可，不必在每个 purpose 分支各改一遍。

注意 `token_count` 的计算在该 return 之前（`composer.py:420` 附近，含 `+ image_tokens(m.content)`）。**降级必须发生在 token 计数之前**——否则报出的 token 数含图、而实际发出的 prompt 已无图，两者对不上，会让 budget 与 compact 的判断基于错误的数字。**这一点请在实现时确认，并加一条测试钉住**：`purpose="compact"` 且内容含图时，`AssembledPrompt.token_count` 不应包含 `_IMAGE_PART_TOKENS`。

- [ ] **Step 1-5: RED → GREEN**

测试覆盖：
1. `purpose="act"` → 图片**保留**（`isinstance(content, list)` + 含 `ImagePart`）
2. `purpose="compact"` → 图片被替换成 `[image image/png]` 文本占位
3. `recognize_intent` / `observe` / `background_observe` 同 2
4. **纯文本会话**：五个 purpose 的产出**逐字节不变**（降级逻辑对无图内容零影响）
5. 降级后的消息仍是合法的 `LLMMessage`（不产生空 content）

- [ ] **Step 6: 提交**

```bash
git commit -m "feat(composer): 非 act purpose 降级图片为文本占位，compaction 不再重发所有图"
```

---

### Task 5: 端到端验证

**Files:**
- Test: `tests/integration/test_multimodal_end_to_end.py`（追加）

覆盖三条，每条都要能真的红：

1. **ref 全链路**：注册真 `BlobStore` → `start_session(user_prompt=[TextPart, ImagePart])` → 断言
   - memory 里存的 `ImagePart.source_type == "ref"`（**不是 base64**，证明外部化生效）
   - 送到 adapter 的 wire payload 里是**完整 base64**（证明 rehydrate 生效）
2. **不接 blob store 时行为不变**：不注册 `BlobStore` → 同样的会话 → memory 里仍是 `source_type="base64"`，wire 里也是 base64。**与 Phase 3a 的既有 e2e 测试结果一致**
3. **compaction 不再带图**：驱动一次 compact purpose 的装配，断言其 messages 里**无 `ImagePart`**

```bash
git commit -m "test(multimodal): ref 全链路 + 不接 blob 行为不变 + compaction 不带图"
```

---

### Task 6: spec 更新 + 全量回归

- [ ] **Step 1: spec 更新**（只追加，不删原文）

1. **§3③**：记录架构裁定 T0——rehydrate 实际落在 `llm_gateway` 而非 adapter，写明理由（adapter 序列化链是同步的、`BlobStore.get` 是 async），并说明这保住了 §3③ 的实质（core 全程只见 ref）
2. **§5**：`FilesystemBlobStore` 已落地，写明内容寻址 / 幂等 / `get` 不抛 / 落盘布局
3. **§6.1**：`normalize_content` 已落地，写明**先探询 `can_externalize` 再决定**（不 try/except），以及 `validate_content` 先于 `normalize_content` 的顺序理由
4. **§13**：把「compaction 超预算时重发所有图片」标为**已兑现**（Task 4），注明测试函数名；把 M5 的 ref 处理标为已兑现
5. **§13 新增**：per-purpose 策略的当前取值表，以及"若将来要让 observe 看图，改这一处"的指引

- [ ] **Step 2: 全量回归（单跑）**

Expected: 失败数**必须仍是 3**，且正是那三条既有环境失败。

- [ ] **Step 3: ruff 增量核对**

与 Phase 3b 基线逐条比对（窄 select `I001,F401,F811`），期望零新增。比对完 `git checkout HEAD -- src/` 复原。

- [ ] **Step 4: 人工确认四条不变量**

1. **不接 blob store 行为不变**：`NullBlobStore` 路径上，`normalize_content` 与 `rehydrate_content` 都是恒等变换
2. **`put` 的 `raise NotImplementedError` 未被 try/except 吞掉**（grep 确认）
3. **判据未被改**：`not hasattr(p, "text")` 在 `content.py` / `utils.py` 三处仍一致
4. **纯文本 wire 形态未变**

- [ ] **Step 5: 提交**

```bash
git commit -m "docs(spec): Phase 3b 落地说明 + 兑现 compaction 与 M5 两条遗留"
```

---

## Phase 3b 完成标准

- `FilesystemBlobStore` 就位：内容寻址、幂等、`get` 不抛
- 入口 `normalize_content` 把 base64 外部化成 ref，**先探询 `can_externalize`**
- gateway 在出网前 rehydrate，取不到则降级不抛
- 非 `act` purpose 不再携带图片——**compaction 不再重发所有图**
- **不接 blob store 的宿主行为与 Phase 3a 逐字节一致**
- 纯文本行为逐字节不变；全量失败数仍为 3

## 后续

**Phase 4（折叠与回放）**：`core/media/` 模块、L0.5 图片降级、`media:get_image` 取回。见子设计 `2026-08-20-image-fold-replay-design.md`。

Phase 4 必须携带的两条：
- spec §13 的 dict-shaped part 隐患（判据仍冻结）
- Phase 2 §6.6 的 OpenAI tool-result 图片重定位**尚未实现**——`media:get_image` 依赖它
