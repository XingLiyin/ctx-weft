# Phase 4 实施计划——图片折叠与回放

子设计：`docs/superpowers/specs/2026-08-20-image-fold-replay-design.md`
上级设计：`docs/superpowers/specs/2026-08-20-multimodal-design.md`
分支 `feat/multimodal`，起点 `5a6f5e3`，基线 `1978 total / 1970 passed / 3 failed / 5 skipped`。

---

## 0. 子设计写于 Phase 3b 之前，一半已被后续阶段做掉

controller 逐条核实：

| 子设计条目 | 状态 |
|---|---|
| §2 token 估算图片感知 | ✅ Phase 0；3c/D 又改成按**体积**估算 |
| §3 `ContextOverflowError` 报图片数 | ✅ Phase 0 |
| §4.3 Anthropic `tool_result.content` 收 block 列表 | ✅ 已走 `_parts_to_blocks`（`anthropic.py`） |
| §4.3 OpenAI 分组循环 + 组尾追加 user 消息 | ✅ **Phase 3c Task B** |
| §4.2 `InvocationResult.content` 放宽 | ❌ 仍是 `content: str` |
| §7 `core/media/` 四个文件 | ❌ 全部未建 |
| §9 L0.5 接入 / §6.1 L1-L3 前置降级 | ❌ |
| §9 `MediaCapabilityProvider` 注册 | ❌ |

**所以本 Phase 的实际范围比子设计文本小**，§9 那张调用点表已完成两行。

另外子设计 §2 的表述已被 3c/D 推翻——它说「图片一律算 0」，那是 Phase 0 之前的现状；
现在是 `max(1600, byte_size // 128)`。写 spec 时要订正，不要照抄。

---

## 1. 已知的既有能力（本 Phase 直接消费，不要重造）

- `core/content.py::downgrade_images_to_text` —— per-purpose 降级用，**产出瞬时、不落库**
- `core/content.py::normalize_content_parts` —— 三处边界共用的归一
- `core/content.py::rehydrate_content` —— gateway 出网前 ref→base64
- `core/utils.py::image_tokens / image_byte_size` —— 体积相关的 token 口径
- `memory.fold(supersede_ids, replacements, ctx)` —— 原子「遗忘+补偿」，**已有，不需新增协议方法**
- `SqlMemoryProvider` 实现 `BlobStore`；`get_blob_store()` 自动解析

**关键区分（不要混淆）：**

| 降级 | 谁做 | 落库？ | 占位 |
|---|---|---|---|
| per-purpose（Phase 3c Task 4） | composer | **否**，只影响本次 prompt | `[image {media_type}]` |
| **L0.5（本 Phase）** | `media.demote_for_budget` | **是**，重写 memory 记录 | 含 ref，可被 `get_image` 解析回来 |

两者都存在、互不替代。per-purpose 是"这次不发"，L0.5 是"从记忆里收起来"。

---

## 2. ⚠️ L6 收口：占位文案现在有四种，本 Phase 还要加第五种

实测现存（`grep`）：

```
core/content.py      [image unavailable: {media_type}]     rehydrate 取不回
core/content.py      [image {media_type}]                  per-purpose 降级
openai.py            [图片见后一条消息]                      tool 图重定位标记
memory_sql/*.py      [image unavailable]                   （docstring 示例）
```

**中英不统一，且无单一真源。** 台账 L6 记的就是「Phase 4 若还要加占位，建议先收口」。

**controller 裁定**：只有 **L0.5 的占位需要被解析回来**（`get_image` 要从占位里找出 ref），
其余三种都是**单向渲染、永不回读**。所以：

- `core/media/refs.py` 拥有 **L0.5 占位的编解码**，这是它的核心职责，**且是唯一有解析语义的**
- 另外三种保持在原处（`content.py` 是归一层、`openai.py` 是 wire 格式，各归其位），
  但**统一成英文**并各自加一行注释指向一份清单
- 不为了"整齐"把它们搬进 `media/`——那会把 wire 格式知识拖进 core 模块

---

## 3. 任务拆分（7 个）

| # | 任务 | 要害 |
|---|---|---|
| 1 | `core/media/refs.py` + L6 占位收口 | 编解码往返；占位必须**确定性**（缓存约束） |
| 2 | `core/media/policy.py` + `fold.py` | `demote_for_budget` / `demote_all`；**位置不变**（`timestamp`/`seq_no` 原样带过） |
| 3 | `InvocationResult.content` 放宽 + `metadata["content_parts"]` 通道 | 落盘截断/human note/事件截断**只作用于文本部分** |
| 4 | `core/media/capability.py` + 注册 | `get_image` 要读 task 视图算"第几条 user 回合" |
| 5 | L0.5 接入 + §6.1 L1/L3 前置降级 | 插在 L1 **之前**；`_apply` 的 before/after 不变量 |
| 6 | 端到端 | 取回全链路 + 生命周期 + 双 adapter |
| 7 | spec 收口 + 全量回归 | 订正子设计 §2 的失效表述 |

---

## 4. 全局约束（沿用 Phase 3c，逐条绑定）

- 纯文本行为逐字节不变；不接 BlobStore 时行为不变（`demote_*` 返回 0）
- 判据 `not hasattr(p, "text")` **不解冻**（用户裁定 D1）
- 占位文本**逐字节确定性**（无随机 id / 时间戳 / 计数器）——缓存前缀约束
- **绝对禁止 `git stash`**
- **绝对禁止 `git add -A` / `git add .`**（本仓有并发会话）
- 全量单跑勿并发；**pytest 不输出末尾计数行**，精确计数用 `--junit-xml` 解析
- **跑测试前 `uv sync --all-extras`**（`--extra dev` 会卸掉别的 extra 的依赖）
- 变异脚本必须 `assert old in s` 确认 patch 应用；**含反斜杠字符串不走 heredoc**
- 「不超过上界」型断言对「什么都没做」不敏感；"没有发生某件事"型断言必须变异验证

---

## 5. §12 未决参数——按子设计暂定值执行，标记待校准

| 参数 | 暂定 | 说明 |
|---|---|---|
| `keep_recent` | **2** | 最近两张图保原样。需实测校准 |
| 占位措辞 | 见 Task 1 | 影响模型是否会主动调 `get_image`，需实测 |
| `get_image` 多 ref | **单个** | 避免一次调用把窗口打满 |

三条都**做成可配置**，不要硬编码在逻辑里，便于日后校准。

---

## 6. 本 Phase 不做

- L2 `demote_kept_capsules` 不加前置降级（子设计 §6.1 明确：纯遗忘、无处承载 ref）
- 装配侧（`agent_recall` / `_history` / `composer`）无本 Phase 专属改动
- 台账 L1/L3/L10/L12/L14/L17/L25 等 Phase 3c 遗留——各自独立，不并入本 Phase

---

## 7. 落地状态（2026-08-27 收口）

**Phase 4 已完成**，`5a6f5e3..4867795` 共九个提交，全量
`2088 total / 2080 passed / 3 failed / 5 skipped`（3 条为既有环境性失败，与起点一致）。

| # | 任务 | 提交 | 状态 |
|---|---|---|---|
| 1 | `core/media/refs.py` + L6 占位收口 | `e5ab6a5`（+ controller 订正 `2f1f6cf`） | ✅ 变异 6 个全杀 |
| 2 | `core/media/policy.py` + `fold.py` | `6dc40aa` | ✅ 变异 7 个全杀 |
| 3 | `InvocationResult.content` 放宽 + `CONTENT_PARTS_KEY` | `d1f0ea5`（+ 修 flake `64f23c0`） | ✅ 变异 6 个全杀 |
| 4 | `core/media/capability.py` + 注册 | `dda0c4f` | ✅ 变异 8 个全杀 |
| 5 | L0.5 接入 + §6.1 前置降级 | `08b3eaf` | ✅ 变异 8 个全杀 |
| 5b | 修 P4-L10（L1 的摘要顺序错） | `c90398a` | ✅ 计划外新增，变异 3 个全杀 |
| 6 | 端到端 | `4867795` | ✅ `src/` 一行未改；变异 9 个，7 杀 2 存活（不可观测/场景等价） |
| 7 | spec 收口 + 全量回归 | 本次 | ✅ |

**与本计划的偏差（都是加固，无缩水）：**

- **多出一个 Task 5b**：本计划第 3 节的七任务划分里没有它。Task 5 落地后发现
  §6.1 对 L1 实际无效（`escalating_compact` 先算摘要、后调 `fold_root_experience`，
  而 `demote_all` 在后者内部），记为 P4-L10 并单开任务修（`c90398a`）。
- **第 5 节三条「未决参数」全部按暂定值执行并做成可配置**：
  `LoopConfig.compact_keep_recent_images = 2` / `refs.IMAGE_PLACEHOLDER_TEMPLATE` /
  `capability.MAX_REFS_PER_CALL = 1`。三条仍待实测校准。
- **第 2 节的 L6 收口按裁定 R1 完成**：四种占位里只有 L0.5 那种有解析语义，
  `core/media/refs.py` 模块 docstring 是清单；另三种留在原处、统一成英文、各加注释指向清单。
- **多出一个 `_media_enabled(ctx)` 总闸**（本计划未要求）：未接 BlobStore 时
  L0.5 与 §6.1 连一次多余的 memory 读都不发，坐实第 4 节「不接 BlobStore 时行为不变」
  是**逐字节**的。
- **第 6 节「本 Phase 不做」的三条全部守住**：L2 未加前置降级；装配侧
  （`agent_recall` / `_history` / `composer`）一行未改；Phase 3c 遗留未并入。

**遗留 P4-L1 ~ P4-L14 的逐条处置见子设计
`docs/superpowers/specs/2026-08-20-image-fold-replay-design.md` §13.2**
（**已闭合 6 条**：L4/L7/L9/L10/L11/L13；**移交后续 5 条**：L2/L3/L5/L12/L14；
**明确不做 3 条**：L1/L6/L8。其中 L1/L9/L13 另带一条**移交宿主**的注意事项）。
其中**最要紧的是 P4-L12**（`test_dispatch_boundary_recap_e2e` 可能盖着一个真的折叠缺口，
诊断与处置建议见 §13.3）——**下一个接手的人应从它开始**。
