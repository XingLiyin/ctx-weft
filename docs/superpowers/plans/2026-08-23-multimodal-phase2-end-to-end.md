# 多模态 Phase 2：端到端打通 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让图片一路走到模型——装配链保 parts、adapter 转成 wire blocks、事件脱敏。**本阶段结束即多模态可用**（inline base64 形态，未外部化）。

**Architecture:** 拆掉装配链上的三处拍扁（`_history` 建块、`agent_recall` 召回、`composer` 建消息），把 `composer` 的三个字符串拼接器与「当前消息框」改成保 parts，两家 LLM adapter 把 `ContentPart` 转成各自的 wire blocks，事件 payload 用 `redact_content_for_event` 脱敏。

**Tech Stack:** Python 3.11+，pytest + pytest-asyncio，`uv run pytest`。

**Spec:** `docs/superpowers/specs/2026-08-20-multimodal-design.md`（§6.3、§6.4、§6.6、§6.8、§10 Phase 2）

**Prior phases:** Phase 0（`e8cb02c`，token 估算图片感知）、Phase 1（`65978b7`，内容骨架）。

## Global Constraints

- **纯文本行为逐字节不变。** 每个任务都必须有一条断言证明：`str` 输入下新旧产物相等。
- **Phase 2 不做外部化。** `ImagePart.source_type` 在本 Phase 内恒为 `"base64"`；adapter 直接把 base64 写进 wire payload。`"ref"` 的 rehydrate 是 Phase 3。
- **摘要恒为纯文本**（spec §8）。`MemoryKind.SUMMARY` 记录的 content 永远是 `str`，因此 `_history.py` 里 `wrap_compact_summary` / `PROGRESS_SO_FAR_HEADING` / `annotate_assistant_summary` 三个包装器**保持字符串拼接、不必改**。只有非摘要记录需要保 parts。
- **只允许两类地方拍扁**（spec §3②）：语义检索 query（`knowledge.py` / `long_memory.py` / `task_spec.py`）与摘要输入（`compact.py` / `finalize.py` / `background_observe.py`）。这些**保持现状、不要改**。
- 非文本 part 判据保持 `not hasattr(p, "text")`（Phase 2 仍冻结，见 spec §13）。
- 测试运行器 `uv run pytest`。**已知**本环境下 `pytest -q` 配合大量 warning 时终结汇总行不输出——用不带 `-q` 的调用或 `-v`。
- **不得新增 PytestWarning。** 需要 asyncio 时用 `@pytest.mark.asyncio` 装饰**具体的 async 测试**，**不得**用模块级 `pytestmark`。
- 仓库有约 12000 条既有 ruff 违规，**不得**跨仓库跑 `ruff --fix`；如需使用，限定单文件单规则。
- 全量套件基线：`3 failed, 1525 passed, 3 skipped`。三条失败为既有环境问题（`test_compact_flow_e2e.py::test_multiround_retry_accumulates_then_l3_collapses_e2e`、`test_golden_conformance.py::test_golden_dir_present`、`test_observe_outcomes.py::test_default_role_prompt_uses_two_fields`），**不要试图修**。出现第四条即为本 Phase 引入。

## 读取方枚举（Phase 1 教训的应用）

Phase 1 的最终评审发现一个系统性缺口：计划枚举了被放宽字段的**写入方**却漏了**读取方**，导致两处 Critical 崩溃。本 Phase 让 `list[ContentPart]` 可达 `ContextBlock.content` 与 `LLMMessage.content`，故**已预先枚举全部读取方**：

**`ContextBlock.content` 读取方（5 处，已全部核查）：**

| 位置 | 处理 |
|---|---|
| `budget.py:89` `image_part_count(b.content)` | **无需改**——本 Phase 令其从恒 0 转为生效（兑现 Phase 0 遗留义务，见 Task 7） |
| `composer.py:739` capability 渲染 | 拍扁正确，不改 |
| `composer.py:890` history → LLMMessage | **Task 3 改** |
| `composer.py:1008` body 渲染 | 拍扁正确，不改 |
| `composer.py:1059` description 渲染 | 拍扁正确，不改 |

**`LLMMessage.content` 读取方（全仓核查）：**

| 位置 | 处理 |
|---|---|
| `composer.py:400` token 计数 | **无需改**——已含 `+ image_tokens(m.content)`，本 Phase 令其生效（Phase 0 义务） |
| `composer.py:817/830/855` 三个拼接器 | **Task 1 改** |
| `llm_gateway.py:118,148,245,281` | **无需改**——已 parts-aware（`_is_empty_content` / `_merge_message_content` / `estimate_content_tokens`） |
| `prepare.py:95` | **无需改**——已用 `estimate_content_tokens` |
| `act.py:221` / `observe.py:121` | **Task 6 改**（`str(m.content)` 会把 base64 dump 进事件） |
| `recognize_intent.py:127` | **Task 6 改**（`else ""` 丢内容） |
| `providers/llm/{anthropic,openai,mock}.py` | **Task 5 改** |

**字符串操作扫描**：全仓 grep `.content.{startswith,endswith,split,strip,replace,lower,upper,find,splitlines}`、`.content[`、`in ....content` —— 只命中 `llm_gateway.py:148` 的 `_is_empty_content`，而它已 parts-aware。**无其它字符串操作风险**。

---

### Task 1: `composer` 三个拼接器保 parts

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（`_prepend_to_first_user` ~816、`_append_to_user_at` ~829、`_append_to_last_user` ~854）
- Test: `tests/unit/test_composer_splicers_parts.py`

**Interfaces:**
- Consumes: `ctx_weft.core.content.content_with_prefix` / `content_with_suffix`（Phase 1）
- Produces: 三个拼接器在 `LLMMessage.content` 为 part 列表时保留图片。后续 Task 3/4 依赖它们。

**为什么先做这个：** 它们是装配链上离 LLM 最近的一层。先让它们保 parts，后面的 Task 3/4 送进来的 parts 才不会在这里被拍掉。

三处的现有形态相同：

```python
        base = m.content if isinstance(m.content, str) else content_to_text(m.content)
        out[i] = dataclasses.replace(m, content=f"{text}\n\n---\n\n{base}")   # _prepend_to_first_user
        out[idx] = dataclasses.replace(m, content=f"{base}\n\n{text}")        # _append_to_user_at
        out[-1] = dataclasses.replace(last, content=f"{base}\n\n{text}")      # _append_to_last_user
```

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_composer_splicers_parts.py`：

```python
from ctx_weft.core.assembler.composer import PromptComposer
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


def _c():
    return PromptComposer()


# ── 纯文本：与改造前逐字节相同 ────────────────────────────────────────────

def test_prepend_str_unchanged():
    out = _c()._prepend_to_first_user([LLMMessage(role="user", content="body")], "head")
    assert out[0].content == "head\n\n---\n\nbody"


def test_append_at_str_unchanged():
    out = _c()._append_to_user_at([LLMMessage(role="user", content="body")], 0, "tail")
    assert out[0].content == "body\n\ntail"


def test_append_last_str_unchanged():
    out = _c()._append_to_last_user([LLMMessage(role="user", content="body")], "tail")
    assert out[-1].content == "body\n\ntail"


def test_empty_text_is_noop():
    msgs = [LLMMessage(role="user", content="body")]
    assert _c()._append_to_last_user(msgs, "") is msgs


# ── 多模态：图片必须存活 ──────────────────────────────────────────────────

def _parts_msg():
    return LLMMessage(role="user", content=[TextPart(text="body"), _img()])


def test_prepend_keeps_image():
    out = _c()._prepend_to_first_user([_parts_msg()], "head")
    assert any(not hasattr(p, "text") for p in out[0].content), "图片不得丢失"
    assert out[0].content[0].text == "head\n\n---\n\nbody"


def test_append_at_keeps_image():
    out = _c()._append_to_user_at([_parts_msg()], 0, "tail")
    assert any(not hasattr(p, "text") for p in out[0].content)
    assert out[0].content[-1].text == "tail", "尾部为图片时应新插一个 TextPart"


def test_append_last_keeps_image():
    out = _c()._append_to_last_user([_parts_msg()], "tail")
    assert any(not hasattr(p, "text") for p in out[-1].content)


def test_append_last_creates_user_when_tail_not_user():
    """末条非 user 时新建一条 user 回合——既有行为，不得改变。"""
    out = _c()._append_to_last_user([LLMMessage(role="assistant", content="x")], "tail")
    assert out[-1].role == "user" and out[-1].content == "tail"
```

**若 `PromptComposer` 的类名或构造签名与上面不符**，以 `composer.py` 实际为准调整测试，不要改源码迁就测试。三个方法都是实例私有方法，直接调用即可。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_composer_splicers_parts.py -v`
Expected: 四条 `str` 用例与 `test_append_last_creates_user_when_tail_not_user` PASS（改造前已正确），三条多模态用例 FAIL（图片被拍扁）

- [ ] **Step 3: 实现**

在 `composer.py` 第 104 行的 import 加入 `content_with_prefix` / `content_with_suffix`：

```python
from ctx_weft.core.content import content_with_prefix, content_with_suffix
```

（`content_to_text` 与 `image_tokens` 保持既有 import 来源不变。）

`_prepend_to_first_user` 的：

```python
                base = m.content if isinstance(m.content, str) else content_to_text(m.content)
                out[i] = dataclasses.replace(m, content=f"{text}\n\n---\n\n{base}")
```

改为：

```python
                # 保 parts：拍扁会丢图。content_with_prefix 对 str 走朴素拼接、逐字节等价。
                out[i] = dataclasses.replace(
                    m, content=content_with_prefix(m.content, f"{text}\n\n---\n\n"))
```

`_append_to_user_at` 的：

```python
        base = m.content if isinstance(m.content, str) else content_to_text(m.content)
        out[idx] = dataclasses.replace(m, content=f"{base}\n\n{text}")
```

改为：

```python
        out[idx] = dataclasses.replace(m, content=content_with_suffix(m.content, f"\n\n{text}"))
```

`_append_to_last_user` 的：

```python
            base = last.content if isinstance(last.content, str) else content_to_text(last.content)
            out[-1] = dataclasses.replace(last, content=f"{base}\n\n{text}")
```

改为：

```python
            out[-1] = dataclasses.replace(
                last, content=content_with_suffix(last.content, f"\n\n{text}"))
```

**注意**：三处的空 `text` 早退分支（`if not text: return messages`）保持不变。末条非 user 时新建 user 回合的分支也不变。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_composer_splicers_parts.py -v`
Expected: 8 passed

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "composer or assembl or framing or guidance" -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/assembler/composer.py tests/unit/test_composer_splicers_parts.py
git commit -m "feat(composer): 三个拼接器保 parts，装配期不再拍扁图片"
```

---

### Task 2: `_history` 与 `agent_recall` 建块保 parts

**Files:**
- Modify: `src/ctx_weft/core/assembler/sources/_history.py`（`record_to_history_block`）
- Modify: `src/ctx_weft/core/assembler/sources/agent_recall.py:94`
- Test: `tests/unit/test_history_block_parts.py`

**Interfaces:**
- Produces: `ContextBlock.content` 在非摘要记录上保留 `list[ContentPart]`。Task 3 依赖它。

**关键简化（务必理解后再动手）：** spec §8 规定 **`MemoryKind.SUMMARY` 的 content 恒为纯文本**。而 `record_to_history_block` 里那三个文本包装器（`wrap_compact_summary`、`PROGRESS_SO_FAR_HEADING` 前缀、`annotate_assistant_summary`）**只在 `etype == MemoryEventType.TASK_COMPACT_SUMMARY` 时执行**。因此：

- **摘要记录**：走原有纯字符串路径，三个包装器**一个字都不用改**
- **非摘要记录**（`CONVERSATION_TURN` 等）：`ContextBlock.content` 直接用 `record.content` 原值，保 parts

不要把三个包装器改成 parts-aware——那是多余的改造面，且与 spec §8 相悖。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_history_block_parts.py`：

```python
from datetime import datetime, UTC
from types import SimpleNamespace

from ctx_weft.core.assembler.sources._history import record_to_history_block
from ctx_weft.protocols import (
    ImagePart, MemoryAddress, MemoryKind, MemoryRecord, MemoryScope, TextPart,
)

_BASE = datetime(2026, 8, 23, tzinfo=UTC)


def _record(content, kind=MemoryKind.CONVERSATION_TURN, role="user"):
    return MemoryRecord(
        id="mem_1", type=None, kind=kind, scope=MemoryScope.TASK,
        address=MemoryAddress(session_id="s", task_id="t1", agent_id="a"),
        content=content, timestamp=_BASE, role=role, metadata={"task_id": "t1"},
    )


def _request():
    return SimpleNamespace(token_counter=len, task=SimpleNamespace(id="t1"))


def _blk(rec):
    return record_to_history_block(rec, "task_conversation", 0,
                                   request=_request(), current_task_id="t1")


def test_plain_text_block_content_unchanged():
    assert _blk(_record("hello")).content == "hello"


def test_conversation_turn_keeps_parts():
    content = [TextPart(text="看这张图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    blk = _blk(_record(content))
    assert blk.content == content, "非摘要记录必须原样保留 parts"


def test_summary_record_stays_text():
    """spec §8：SUMMARY 恒为纯文本，包装器仍走字符串路径。"""
    blk = _blk(_record("段落摘要", kind=MemoryKind.SUMMARY, role="assistant"))
    assert isinstance(blk.content, str)


def test_token_estimate_still_counts_images():
    """Phase 0 的图片补充项不得被本次改动破坏。"""
    content = [TextPart(text="look"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    assert _blk(_record(content)).token_estimate == len("look") + 1600
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_history_block_parts.py -v`
Expected: `test_conversation_turn_keeps_parts` FAIL（content 是拍扁的字符串），其余 PASS

- [ ] **Step 3: 改 `_history.py`**

现有代码（约 67 行起）：

```python
    text = content_to_text(record.content) if not isinstance(record.content, str) else record.content
    role = record.role or "user"
    etype = record.type or legacy_type_of(record.kind, record.scope, record.role)
    if etype == MemoryEventType.TASK_COMPACT_SUMMARY and role == "user":
        text = wrap_compact_summary(text)
    elif etype == MemoryEventType.TASK_COMPACT_SUMMARY and role == "assistant":
        if current_task_id is not None and record.metadata.get("task_id") == current_task_id:
            text = f"{PROGRESS_SO_FAR_HEADING}\n{text}"
        text = annotate_assistant_summary(text)
```

在这段之后、构造 `ContextBlock` 之前，插入 content 的选择逻辑，并把 `ContextBlock(content=text)` 改为 `content=block_content`：

```python
    # 摘要恒为纯文本（spec §8），三个包装器只作用于它，故走原字符串路径；
    # 非摘要记录原样保留 record.content —— 拍扁会丢图（spec §6.4）。
    _is_summary = etype == MemoryEventType.TASK_COMPACT_SUMMARY
    block_content = text if _is_summary else record.content
```

`ContextBlock(...)` 里：

```python
        content=text,
```

改为：

```python
        content=block_content,
```

`token_estimate` 那一行**保持不变**（它已用 `record.content` 算图片补充项）。

- [ ] **Step 4: 改 `agent_recall.py`**

第 94 行附近：

```python
            text = content_to_text(s.content) if not isinstance(s.content, str) else s.content
```

先读该行的上下文，确认 `text` 之后被用于什么。若它只是喂给 `ContextBlock(content=text)` 与 `token_estimate`，则同 Task 2 Step 3 的处理：`token_estimate` 用 `request.token_counter(text) + image_tokens(s.content)`（若尚未如此），`content` 用 `s.content` 原值。若 `text` 还被用于别的字符串操作（如拼接、判空），**在报告中说明并保守处理**——那一处需要保留文本。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_history_block_parts.py tests/unit/test_history_block_image_tokens.py -v`
Expected: 全部 passed

- [ ] **Step 6: 回归**

Run: `uv run pytest tests/unit -k "history or recall or assembl or budget" -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/core/assembler/sources/ tests/unit/test_history_block_parts.py
git commit -m "feat(assembler): 非摘要记录建块保 parts，摘要仍走纯文本路径"
```

---

### Task 3: `composer._history_to_messages_with_sources` 保 parts

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（约 889-910）
- Test: `tests/unit/test_history_to_messages_parts.py`

**Interfaces:**
- Consumes: Task 2 的 `ContextBlock.content` 保 parts
- Produces: `LLMMessage.content` 承载 parts。这是图片进入 LLM 消息的那一步。

**关键陷阱：空判断。** 现有代码：

```python
            content = content_to_text(b.content)
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            if not content and not tool_calls:
                continue  # 空文本且无 tool_call 才跳过
```

一条**纯图片消息**（`[ImagePart(...)]`，无 TextPart）的 `content_to_text` 结果是 `""`，会被这个 `continue` **静默丢弃**。必须改成 parts-aware 的判空。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_history_to_messages_parts.py`：

```python
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.assembler.composer import PromptComposer
from ctx_weft.protocols import ImagePart, TextPart


def _blk(content, role="user", **md):
    return ContextBlock(
        id="blk_1", source="task_conversation", kind="history", target="messages",
        content=content, priority=5, token_estimate=1,
        metadata={"role": role, "type": "user_prompt", "timestamp": "2026-08-23T00:00:00",
                  "seq_no": 0, "task_id": "t1", **md},
    )


def test_plain_text_message_unchanged():
    out = PromptComposer()._history_to_messages([_blk("hello")])
    assert len(out) == 1 and out[0].content == "hello"


def test_parts_preserved_into_message():
    content = [TextPart(text="看图"), ImagePart(data="ZGF0YQ==", media_type="image/png")]
    out = PromptComposer()._history_to_messages([_blk(content)])
    assert out[0].content == content


def test_image_only_message_is_not_dropped():
    """纯图片消息的 content_to_text 是空串——旧的判空会把它静默丢掉。"""
    out = PromptComposer()._history_to_messages(
        [_blk([ImagePart(data="ZGF0YQ==", media_type="image/png")])])
    assert len(out) == 1, "纯图片消息不得被当成空消息丢弃"


def test_truly_empty_message_still_dropped():
    """空串仍应被丢弃——既有行为不得改变。"""
    assert PromptComposer()._history_to_messages([_blk("")]) == []


def test_empty_parts_list_dropped():
    assert PromptComposer()._history_to_messages([_blk([])]) == []
```

**若 `ContextBlock` 的构造签名与上面不符**，以 `assembler.py` 实际为准调整测试。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_history_to_messages_parts.py -v`
Expected: `test_parts_preserved_into_message` 与 `test_image_only_message_is_not_dropped` FAIL，其余 PASS

- [ ] **Step 3: 实现**

`composer.py` 的：

```python
            content = content_to_text(b.content)
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            if not content and not tool_calls:
                continue  # 空文本且无 tool_call 才跳过（保留仅含 tool_call 的 assistant 回合）
```

改为：

```python
            content = b.content
            tool_calls = b.metadata.get("tool_calls") or [] if role == "assistant" else []
            # 判空必须 parts-aware：纯图片消息的 content_to_text 是空串，
            # 旧写法会把它当空消息静默丢弃（spec §6.4）。
            if _is_blank_content(content) and not tool_calls:
                continue  # 真空且无 tool_call 才跳过（保留仅含 tool_call 的 assistant 回合）
```

并在 `composer.py` 的模块级（靠近其它模块级 helper 处）新增：

```python
def _is_blank_content(content) -> bool:
    """内容是否真的为空。

    与 llm_gateway._is_empty_content 同义但更严格地只判「有无内容」：
    str 看是否空；列表看是否为空、或是否只含空 TextPart。
    非文本 part（图片）一律视为有内容——纯图片消息不是空消息。
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content
    for p in content:
        if not hasattr(p, "text"):
            return False        # 图片 = 有内容
        if p.text:
            return False
    return True
```

其余三处 `LLMMessage(role=..., content=content, ...)` 的构造保持不变——它们现在自然承载 parts。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_history_to_messages_parts.py -v`
Expected: 5 passed

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "composer or assembl or reconstruction or framing" -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/assembler/composer.py tests/unit/test_history_to_messages_parts.py
git commit -m "feat(composer): history 建消息保 parts，纯图片消息不再被当空消息丢弃"
```

---

### Task 4: 「当前消息框」保 parts

**Files:**
- Modify: `src/ctx_weft/core/assembler/composer.py`（`_decorate_current_*`，约 571-616）
- Test: `tests/unit/test_current_message_framing_parts.py`

**Interfaces:**
- Consumes: Task 1 的 `content_with_prefix`、Task 3 的 parts 消息

现有代码把消息拍扁后**重建成纯 str**：

```python
            raw = content_to_text(messages[anchor].content)
            messages[anchor] = LLMMessage(
                role="user", content=f"{prefix}## Opening Message\n{raw}"
            )
        raw_latest = content_to_text(messages[latest].content)
        framed = (
            f"{prefix if anchor == latest else ''}## Current Message\n{raw_latest}\n\n"
            "（Reply in the same language as the Current Message above.）"
        )
        messages[latest] = LLMMessage(role="user", content=framed)
```

这是图片在装配链上最后一次消失的地方——**用户贴的图恰好就在这条消息里**。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_current_message_framing_parts.py`。先读 `tests/unit/test_current_message_framing.py`，**复用它的 fixture 惯例**（该文件已有构造 blocks/messages 的辅助函数）。新测试要覆盖：

1. 纯文本时框架文案与改造前**逐字节相同**（先跑改造前的用例抄下真实值，不要凭想象写期望）
2. 多模态时 `## Current Message` 前缀存在**且图片仍在**
3. 首条 ≠ 末条（interactive 多轮）时，两个框各自贴上且各自的图片都在

**必须先在改动前跑一次拿到真实文案**，这是「逐字节不变」唯一可信的验证方式。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_current_message_framing_parts.py -v`
Expected: 纯文本用例 PASS，多模态用例 FAIL（图片被拍扁）

- [ ] **Step 3: 实现**

两处 `LLMMessage(role="user", content=<f-string>)` 改为用 `content_with_prefix` / `content_with_suffix` 在**原 content 上**加框：

```python
            messages[anchor] = LLMMessage(
                role="user",
                content=content_with_prefix(
                    messages[anchor].content, f"{prefix}## Opening Message\n"),
            )
        framed = content_with_prefix(
            messages[latest].content,
            f"{prefix if anchor == latest else ''}## Current Message\n",
        )
        framed = content_with_suffix(
            framed, "\n\n（Reply in the same language as the Current Message above.）")
        messages[latest] = LLMMessage(role="user", content=framed)
```

**核对文案等价性**：旧的 `f"...\n{raw}\n\n（Reply...）"` 与新的「前缀 `...\n` + 原文 + 后缀 `\n\n（Reply...）`」对 `str` 原文产出完全相同的字符串。**请自行核对一遍**，不要只信这句话。

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/unit/test_current_message_framing_parts.py tests/unit/test_current_message_framing.py -v`
Expected: 全部 passed（既有那份是纯文本守卫，必须仍绿）

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "composer or framing or assembl" -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/assembler/composer.py tests/unit/test_current_message_framing_parts.py
git commit -m "feat(composer): 当前消息框保 parts，用户贴的图不再在加框时丢失"
```

---

### Task 5: LLM adapter 转 wire blocks

**Files:**
- Modify: `src/ctx_weft/providers/llm/anthropic.py`（`_serialize_messages`，约 333-382）
- Modify: `src/ctx_weft/providers/llm/openai.py`（`_serialize_messages`，约 370-408）
- Modify: `src/ctx_weft/providers/llm/mock.py`（约 101）
- Test: `tests/unit/test_adapter_multimodal_wire.py`

**Interfaces:**
- Produces: 图片真正出网。**本任务完成即多模态端到端可用。**

**Anthropic**（原生支持）—— 三处 `_parts_to_text` 改为产出 blocks：

```python
{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
```

assistant 分支（343）：文本仍作一个 `{"type":"text"}` block，图片各作一个 image block，然后才是 tool_use blocks。
tool 分支（361）：`tool_result` 的 `content` 从字符串改为 block 列表。
其它分支（377）：`{"role": m.role, "content": [blocks]}`。

**OpenAI** —— `role="tool"` 消息**只接受文本**（spec §6.6）。本 Phase 的处理：

- assistant / user 分支：`content` 用 `[{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:{media_type};base64,{data}"}}]`
- **tool 分支：拍扁为文本**（`_parts_to_text` 保留）。图片随 tool result 回来是 Phase 4 的 `media:get_image` 才会出现的形态，本 Phase 的 tool result 恒为文本，无损失。

**Mock**（`mock.py:101`）—— `(m.content if isinstance(m.content, str) else "")` 会让多模态消息在 mock 下变空。改为 `content_to_text(m.content)`，保证 mock 驱动的测试仍能看到文本部分。

**纯文本必须逐字节不变**：三家 adapter 在 `content` 为 `str` 时的 wire 形态**不得改变**（Anthropic 仍发字符串或单 text block——以改造前的实际形态为准，先抄下来）。

**改造前的纯文本 wire 形态（controller 已实测捕获，作为「逐字节不变」的基准）：**

```python
# Anthropic —— 注意 user 分支对 str 发的是**裸字符串**，assistant 才是 blocks
[{"role": "user", "content": "hello"},
 {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tc1", "content": "result"}]}]

# OpenAI —— 全部裸字符串
[{"role": "system", "content": "sys"},
 {"role": "user", "content": "hello"},
 {"role": "assistant", "content": "hi"},
 {"role": "tool", "tool_call_id": "tc1", "content": "result"}]
```

**这意味着 Anthropic 的非 assistant/非 tool 分支必须保持「`str` → 裸字符串」**，只有 parts 才转成 block 列表。把 `str` 也改成单元素 block 列表会破坏逐字节不变。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_adapter_multimodal_wire.py`：

```python
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart
from ctx_weft.providers.llm.anthropic import _serialize_messages as anth
from ctx_weft.providers.llm.openai import _serialize_messages as oai


def _img():
    return ImagePart(data="ZGF0YQ==", media_type="image/png")


def _plain():
    return [LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi", tool_calls=[]),
            LLMMessage(role="tool", content="result", tool_call_id="tc1")]


# ── 纯文本 wire 形态逐字节不变（基准为 controller 实测值）────────────────

def test_anthropic_plain_text_wire_unchanged():
    assert anth(_plain()) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tc1", "content": "result"}]},
    ]


def test_openai_plain_text_wire_unchanged():
    assert oai("sys", _plain()) == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "tc1", "content": "result"},
    ]


# ── Anthropic 多模态 ─────────────────────────────────────────────────────

def test_anthropic_user_image_becomes_image_block():
    out = anth([LLMMessage(role="user", content=[TextPart(text="看图"), _img()])])
    blocks = out[0]["content"]
    assert isinstance(blocks, list), "parts 输入必须产出 block 列表"
    assert {"type": "text", "text": "看图"} in blocks
    assert {"type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": "ZGF0YQ=="}} in blocks


def test_anthropic_tool_result_accepts_blocks():
    out = anth([LLMMessage(role="tool", content=[TextPart(text="r"), _img()],
                           tool_call_id="tc1")])
    tr = out[0]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "tc1"
    assert isinstance(tr["content"], list)
    assert any(b.get("type") == "image" for b in tr["content"])


# ── OpenAI 多模态 ────────────────────────────────────────────────────────

def test_openai_user_image_becomes_image_url():
    out = oai("", [LLMMessage(role="user", content=[TextPart(text="看图"), _img()])])
    parts = out[0]["content"]
    assert isinstance(parts, list)
    assert {"type": "text", "text": "看图"} in parts
    url = next(p["image_url"]["url"] for p in parts if p["type"] == "image_url")
    assert url == "data:image/png;base64,ZGF0YQ=="


def test_openai_tool_message_flattens_to_text():
    """OpenAI 的 role="tool" 只接受文本——本 Phase 刻意拍扁，不得抛。"""
    out = oai("", [LLMMessage(role="tool", content=[TextPart(text="r"), _img()],
                              tool_call_id="tc1")])
    assert isinstance(out[0]["content"], str)
    assert "r" in out[0]["content"]
```

Mock 的验证加进同一文件：读 `mock.py` 确认其消息处理入口后，断言多模态输入下文本部分可见、且不抛。**若 `mock.py` 的入口不是可直接调用的纯函数**，改为跑一条既有的 mock 驱动测试并断言其仍通过即可，在报告中说明。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/unit/test_adapter_multimodal_wire.py -v`
Expected: 纯文本三条 PASS，多模态各条 FAIL

- [ ] **Step 3-5: 分别实现三家 adapter，每家改完立即跑对应测试**

- [ ] **Step 6: 回归**

Run: `uv run pytest tests/unit -k "adapter or anthropic or openai or mock or stream or gateway" -v`
Expected: 全部 PASS

- [ ] **Step 7: 提交**

```bash
git add src/ctx_weft/providers/llm/ tests/unit/test_adapter_multimodal_wire.py
git commit -m "feat(llm): adapter 把 ContentPart 转成 wire blocks，图片真正出网"
```

---

### Task 6: 事件脱敏

**Files:**
- Modify: `src/ctx_weft/core/loop/steps/act.py:221`
- Modify: `src/ctx_weft/core/loop/steps/observe.py:121`
- Modify: `src/ctx_weft/core/loop/steps/recognize_intent.py:127`
- Test: `tests/unit/test_event_redaction.py`

**Interfaces:**
- Consumes: `ctx_weft.core.content.redact_content_for_event`（Phase 1 建好、至今无调用方）

`act.py` 与 `observe.py` 现有：

```python
            {"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
```

`str(m.content)` 会把整个 dataclass repr（含完整 base64）dump 进 `LLM_PROMPT_SENT` 事件——一张图几万字符，会把事件库撑爆。改为：

```python
            {"role": m.role, "content": redact_content_for_event(m.content)}
```

`recognize_intent.py:127` 的 `else ""` 会把整条多模态消息内容丢掉，改为 `content_to_text(m.content)`（该处是喂给意图识别的文本，拍扁正确，但不该丢空）。

- [ ] **Step 1: 写失败测试**

新建 `tests/unit/test_event_redaction.py`：

```python
from ctx_weft.core.content import content_to_text, redact_content_for_event
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart

_LONG_B64 = "QUJDRA==" * 500          # 模拟真实图片的体量


def _img():
    return ImagePart(data=_LONG_B64, media_type="image/png")


def _redact(messages):
    """复刻 act.py / observe.py 的 payload 构造表达式（改造后形态）。"""
    return [{"role": m.role, "content": redact_content_for_event(m.content)}
            for m in messages]


def test_plain_text_payload_unchanged():
    msgs = [LLMMessage(role="user", content="hello")]
    assert _redact(msgs) == [{"role": "user", "content": "hello"}]


def test_base64_never_reaches_event_payload():
    msgs = [LLMMessage(role="user", content=[TextPart(text="看图"), _img()])]
    blob = _redact(msgs)[0]["content"]
    assert _LONG_B64 not in blob, "完整 base64 不得进事件 payload"
    assert len(blob) < 200, "脱敏后应是短标记，不是几万字符"
    assert "看图" in blob and "image/png" in blob


def test_recognize_intent_gets_text_not_empty():
    """recognize_intent 的 else "" 会把整条多模态消息丢空。"""
    m = LLMMessage(role="user", content=[TextPart(text="看图"), _img()])
    assert content_to_text(m.content) == "看图"
```

**再补一条真正驱动事件的测试**：`act.py` / `observe.py` 的改动点在 `make_event(... payload={"messages": [...]})` 里。若能低成本构造 `state`/`ctx` 驱动到那一行（参照 `tests/unit/` 下既有的 act/observe 测试惯例），就断言真实发出的 `LLM_PROMPT_SENT` 事件 payload 里无 base64；若驱动成本过高，在报告中说明，并以上面的表达式复刻测试为准。

- [ ] **Step 2-4: RED → 实现 → GREEN**

- [ ] **Step 5: 回归**

Run: `uv run pytest tests/unit -k "act or observe or intent or event" -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add src/ctx_weft/core/loop/steps/ tests/unit/test_event_redaction.py
git commit -m "feat(events): LLM_PROMPT_SENT 脱敏，base64 不再进事件库"
```

---

### Task 7: 兑现 Phase 0 的两条遗留义务 + 端到端测试

**Files:**
- Test: `tests/integration/test_multimodal_end_to_end.py`（Phase 1 已建，在其中追加）
- Test: `tests/unit/test_budget_strategy.py`（追加）
- Modify: `docs/superpowers/specs/2026-08-20-multimodal-design.md`（勾掉两条义务）

**背景：** spec §6.5 留下两条必须在 Phase 2 显式验证的义务：

1. **`composer.py:400` 的 `image_tokens(m.content)` 必须从恒 0 转为生效。** Phase 0 时 composer 一律拍扁，该项是死代码；Task 1/3/4 之后 `LLMMessage.content` 可为 parts，它应当开始计数。
2. **`budget.py:89` 的 `image_part_count(b.content)` 必须从「按构造恒 0」转为生效。** Phase 0 时 `ContextBlock.content` 是 `_history.py` 拍扁后的字符串；Task 2 之后它可为 parts。

**这两条不是"顺便验证"，是 Phase 2 的验收条件。** 不生效说明前面几个任务没真正打通。

- [ ] **Step 1: 写验证测试**

在 `tests/unit/test_budget_strategy.py` 追加：一条驱动 `PriorityBudgetStrategy.apply` 溢出的用例，其 priority-0 block 的 `content` 为**真实的 part 列表**（不是手工构造的、而是经 `record_to_history_block` 产出的），断言 `ContextOverflowError.image_count` 正确。

在 `tests/integration/test_multimodal_end_to_end.py` 追加：断言经完整装配后，`AssembledPrompt.token_count` 对含图会话**大于**同等文本会话（证明 `composer.py:400` 生效）。

- [ ] **Step 2: 端到端测试**

在同一集成测试文件追加：驱动 `start_session(user_prompt=[TextPart, ImagePart])` 走完至少一个 actor 回合，断言

1. 不抛异常
2. memory 里的 `USER_PROMPT` 记录仍是 part 列表
3. **送到 LLM adapter 的 wire payload 里含 image block**（用 mock adapter 捕获，或断言 `AssembledPrompt.messages` 里有含 `ImagePart` 的消息）

第 3 条是本 Phase 的真正验收——前两条 Phase 1 就有了。

- [ ] **Step 3: 更新 spec**

在 `docs/superpowers/specs/2026-08-20-multimodal-design.md` §6.5 中，把那两条「Phase 2 须显式验证」的义务标注为**已兑现**，并注明验证它们的测试名。不要删除原文，改为追加一行说明——保留决策轨迹。

- [ ] **Step 4: 提交**

```bash
git add tests/ docs/superpowers/specs/
git commit -m "test(multimodal): 兑现 Phase 0 的两条遗留义务 + 端到端图片出网验证"
```

---

### Task 8: Phase 2 全量回归

**Files:** 全仓

- [ ] **Step 1: 全量测试**

Run: `uv run pytest tests/`
Expected: 失败数**必须仍是 3**，且正是那三条既有环境失败。任何第四条都是本 Phase 引入的——**报告，不要试图修**。

- [ ] **Step 2: ruff 增量核对**

```bash
SRC=$(git diff --name-only <phase2-base>..HEAD -- 'src/*.py' | tr '\n' ' ')
uv run ruff check $SRC --select I001,F401,F811
```
把结果与基线（`git checkout <phase2-base> -- src/` 后同命令）**逐条**比对，只看 HEAD 独有的行。期望：**零新增**。比对完务必 `git checkout HEAD -- src/` 复原。

- [ ] **Step 3: 人工确认三条不变量**

1. **拍扁只剩两类**：`knowledge.py` / `long_memory.py` / `task_spec.py`（检索 query）与 `compact.py` / `finalize.py` / `background_observe.py` / `recognize_intent.py`（摘要输入）。其余装配链路径不得再有无条件 `content_to_text`。
2. **摘要仍恒为纯文本**：`_history.py` 的三个包装器未被改成 parts-aware。
3. **纯文本 wire 形态未变**：三家 adapter 对 `str` content 的产出与 Phase 2 之前逐字节相同。

- [ ] **Step 4: 提交**

```bash
git commit --allow-empty -m "chore: Phase 2 完成——多模态端到端可用（inline base64）"
```

---

## Phase 2 完成标准

- 装配链保 parts：`_history` 建块、`agent_recall` 召回、`composer` 建消息/加框/拼接
- 三家 adapter 把 `ContentPart` 转成各自 wire 形态
- 事件 payload 脱敏，base64 不进事件库
- **Phase 0 的两条遗留义务已兑现并有测试证明**
- 端到端测试证明图片真正到达 wire payload
- 纯文本行为逐字节不变；全量失败数仍为 3

## 后续 Phase

Phase 3（外部化）的计划在 Phase 2 落地后编写。**它必须处理 Phase 1 最终评审留下的一条契约**：`normalize_content` **不得** try/except `NullBlobStore.put` 的 `NotImplementedError`——那会把「响亮失败」变成控制流、抵消其设计意图。必须先探询 store（`isinstance(store, NullBlobStore)` 或新增 `can_externalize` 属性）再决定是否外部化。
