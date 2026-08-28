"""`media:get_image` —— L0.5 占位的**取回**（子设计 §4.2 / §7 / §8 / §10）。

Task 2 的 `fold.py` 把图收起来（真图 → 含 ref 的文本占位，落库）；本模块把它取回来。
占位的编解码**不在这里**——`refs.find_image_placeholders` 是本仓唯一知道占位长什么样
的地方，本模块只消费它。

════════════════════════════════════════════════════════════════════════════
图回到**对话尾部**，所以位置信息文本是必需的
════════════════════════════════════════════════════════════════════════════

取回的图随 tool result 追加在对话末尾，而不是在历史原位还原——理由是 KV cache
（子设计 §4.4）：原位还原等于改 prompt 中部，其后整段前缀缓存全部失效，而被折叠的图
往往位置靠前，代价接近整份 prompt 重付；tool result 是 append-only，前缀不动。

代价是**图不在它原本的上下文位置上**。故返回的第一条 `TextPart` 必须说清「这张图原本
挂在哪条消息上」——它是这个取舍的补偿，不是装饰。位置由读 task 视图算出，这也正是本
provider 落在 core 侧（构造时注入 `ProviderRegistry`）而不是塞进 `FilesystemToolsProvider`
的原因：普通 capability provider 拿不到 `MemoryProvider`（先例：`ControlCapabilityProvider`）。

════════════════════════════════════════════════════════════════════════════
判断题 1 —— 「第几条 user 回合」的口径
════════════════════════════════════════════════════════════════════════════

**从 1 开始数，只数 task 视图里 `role == "user"` 的记录，按视图顺序（`load_view` 已按
`(timestamp, seq_no)` 升序）。** 理由：

- **从 1 开始**：这句话是写给模型看的自然语言（"your 2nd message"），英文序数没有
  "0th"；0-based 只在代码里自洽，放进 prompt 会让模型把第 2 条说成第 1 条。
- **只数 `role == "user"`**：模型要定位的是「我发的哪一条」。若把 assistant / tool 记录
  一起编号，序号会随工具调用次数漂移，而模型看到的历史里 tool result 与 assistant 消息
  的边界跟它自己的直觉并不一致（同一轮里可能有 3 条 tool 记录），序号立刻失去意义。
- 占位不在 user 记录里时（工具结果里的图、摘要里残留的 ref），报的是它**之前最近的那
  条 user 回合**并明说自己是哪种回合（见 `_origin_phrase`）——不硬凑一个不存在的"第 N
  条 user 消息"。

口径必须在**工具描述里写清楚**（"counting your own messages from 1"），否则模型只能猜。

════════════════════════════════════════════════════════════════════════════
错误处理：这条路在工具调用循环上，**一律不抛**（子设计 §10）
════════════════════════════════════════════════════════════════════════════

| 情形 | 行为 |
|---|---|
| ref 不在本视图的任何占位中 | 单条说明性 `TextPart`，**不含 `ImagePart`** |
| `MemoryBlobStore` 未注册（`blob_store=None` / `NullMemoryBlobStore.get` 返回 None） | 同上，且说明是「字节取不到」而非「没这张图」 |
| `blob.get` 抛 / `load_view` 抛 | 同上，记 warning |

**取回阶段就发现取不到**与 gateway 出网时的 `[image unavailable]` 降级是两件事：后者
发生在已经决定要发这张图之后，前者能让模型当场知道「别再试了」。

════════════════════════════════════════════════════════════════════════════
§12 未决参数：一次取几个 ref
════════════════════════════════════════════════════════════════════════════

暂定**单个**（避免一次调用把窗口打满），但「单个」只钉在 `MAX_REFS_PER_CALL` 与工具
schema 上：`get_image` 内部按**一列 ref** 处理（`_requested_refs` → 循环），把上限调到
2 或把 schema 改成数组都不需要动取回逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ctx_weft.core.media.refs import find_image_placeholders
from ctx_weft.protocols import (
    ContentPart,
    ImagePart,
    MemoryAddress,
    MemoryScope,
    ProviderContext,
    TextPart,
)
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)

logger = logging.getLogger(__name__)

__all__ = ["MAX_REFS_PER_CALL", "MediaCapabilityProvider", "get_image"]

PROVIDER_NAME = "media"
GET_IMAGE = "get_image"
GET_IMAGE_ID = f"{PROVIDER_NAME}:{GET_IMAGE}"

#: 一次调用最多取回几张图（子设计 §12 未决，暂定 1）。改这个常量 + schema 即可放宽，
#: 取回逻辑本身按列表写，不认「单个」。
MAX_REFS_PER_CALL = 1

# 工具描述（给 LLM 看的 schema description）。**模型会不会主动调用取决于这段文字**，
# 故它要回答四个问题：什么时候该调、参数从哪儿抄、结果长什么样、失败了怎么办。
# 措辞与占位文本（`refs.IMAGE_PLACEHOLDER_TEMPLATE`）逐词呼应——占位里说的是
# "dropped to save context" / "bring it back"，这里就不换一套说法，否则模型认不出
# 这个工具就是占位让它调的那个。
GET_IMAGE_DESCRIPTION = (
    "Bring back an image that was dropped from this conversation to save context. "
    "Such an image appears in the history as a placeholder like "
    '[image blob:<sha> media_type=image/png — dropped to save context; '
    'call media:get_image("blob:<sha>") to bring it back]. '
    "Pass that blob:<sha> string, copied exactly as written in the placeholder, as `ref`. "
    "The image comes back inside this tool result — that is, at the END of the "
    "conversation, not at its original position — together with a line saying which of "
    "your messages it was originally attached to (your own messages are counted from 1). "
    "One image per call. Call it only when you actually need to look at that image again; "
    "a restored image is short-lived and can be dropped again later, and the placeholder "
    "stays in place so you can always bring it back once more. "
    "If the ref is unknown or its bytes are no longer stored, you get a short explanation "
    "instead of an image — do not retry with a guessed or reconstructed ref."
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {
            "type": "string",
            "description": (
                "The blob:<sha> reference, copied verbatim from the placeholder text "
                "in the conversation history."
            ),
        },
    },
    "required": ["ref"],
}


# ── 位置信息 ──────────────────────────────────────────────────────────────────


_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n: int) -> str:
    """1 → "1st"。位置信息是给模型读的英文句子，不是给代码读的下标。"""
    if 10 <= n % 100 <= 20:      # 11th / 12th / 13th —— 不是 11st / 12nd / 13rd
        return f"{n}th"
    suffix = _ORDINAL_SUFFIX.get(n % 10, "th")
    return f"{n}{suffix}"


#: 占位不在 user 记录里时，用来说清「那是哪种回合」。
_ROLE_WORD = {
    "assistant": "one of your own earlier replies",
    "tool": "an earlier tool result",
    "system": "an earlier system message",
    "user": "one of your messages",
}


@dataclass(frozen=True)
class _Hit:
    """一个 ref 在视图里的落点。"""

    ref: str
    media_type: str
    user_turn: int        # 含自身在内、其之前的 user 回合数（1-based；0 = 之前没有）
    in_user_record: bool
    role: str


def _origin_phrase(hit: _Hit) -> str:
    """「原本挂在哪」的英文短语。口径见模块 docstring 判断题 1。"""
    if hit.in_user_record:
        return f"originally attached to your {_ordinal(hit.user_turn)} message in this task"
    where = _ROLE_WORD.get(hit.role, "an earlier turn")
    if hit.user_turn <= 0:
        return f"originally part of {where}, before your 1st message in this task"
    return (f"originally part of {where}, after your {_ordinal(hit.user_turn)} "
            f"message in this task")


def _restored_text(hit: _Hit) -> str:
    return (f"Restored image {hit.ref} ({hit.media_type}), {_origin_phrase(hit)}. "
            f"It is shown here at the end of the conversation, not at its original "
            f"position.")


def _unavailable_text(hit: _Hit) -> str:
    return (f"Image {hit.ref} ({hit.media_type}) is {_origin_phrase(hit)}, but its "
            f"stored bytes are no longer available, so it cannot be restored.")


def _not_found_text(ref: str) -> str:
    return (f'No image with ref "{ref}" is present in this task: no placeholder in the '
            f"conversation carries that ref. Copy the blob:<sha> string exactly as it "
            f"appears in the placeholder text; nothing was restored.")


_NO_REF_TEXT = (
    "media:get_image needs a `ref` argument: the blob:<sha> string copied from an "
    "[image ... dropped to save context ...] placeholder in the conversation. "
    "Nothing was restored."
)


# ── 视图扫描 ──────────────────────────────────────────────────────────────────


def _record_texts(rec: Any) -> list[str]:
    """一条记录里所有**文本**片段。

    图片 part 天然被跳过——判据 `not hasattr(p, "text")` 冻结（裁定 D1），此处取的正是
    它的补集，不另立一套判断。dict 形态的 part（JSON 往返来的存量记录）一并接住。
    """
    content = getattr(rec, "content", None)
    if isinstance(content, str):
        return [content] if content else []
    if not content:
        return []
    out: list[str] = []
    for part in content:
        text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _locate(records: Sequence[Any], ref: str) -> _Hit | None:
    """在视图里找 ``ref`` 的**第一个**占位，连同它的位置信息。找不到返回 ``None``。

    同一 ref 可能出现多次（多图消息被降、摘要里也留了一份 ref）。取**最早**那次：占位
    的原位就是图原本所在的位置，后来的那些是折叠留下的副本，说它们会把模型指到错的地方。
    """
    user_turns = 0
    for rec in records:
        role = str(getattr(rec, "role", "") or "")
        if role == "user":
            user_turns += 1
        for text in _record_texts(rec):
            for found_ref, media_type in find_image_placeholders(text):
                if found_ref == ref:
                    return _Hit(ref=ref, media_type=media_type, user_turn=user_turns,
                                in_user_record=(role == "user"), role=role)
    return None


def _requested_refs(ref: Any) -> list[str]:
    """把工具参数归一成一列 ref（去空、去重、保序，截到 `MAX_REFS_PER_CALL`）。

    「一次一个」只体现在这里的上限与 schema 上；调用方给列表（将来放宽 §12 时）同样能走。
    """
    if isinstance(ref, str):
        candidates: Iterable[Any] = [ref]
    elif isinstance(ref, (list, tuple)):
        candidates = ref
    else:
        candidates = []
    out: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:MAX_REFS_PER_CALL]


# ── §8 对外 API ───────────────────────────────────────────────────────────────


async def get_image(
    memory: Any,
    address: Any,
    ctx: ProviderContext,
    ref: Any,
    *,
    scope: MemoryScope = MemoryScope.TASK,
    kinds: Iterable[Any] | None = None,
    blob_store: Any | None = None,
) -> list[ContentPart]:
    """`media:get_image` 工具体。返回 ``[TextPart(位置信息), ImagePart(ref)]``。

    ``ref`` 不存在于本视图的任何 L0.5 占位中时，返回**单条说明性 `TextPart`**——不抛、
    也不返回 `ImagePart`（子设计 §10）。本函数在工具调用循环上，任何异常都会打断 loop，
    故 `load_view` / `blob.get` 的失败一律吸收成说明性文本。

    ``blob_store=None`` 表示宿主没有可用的 blob store（未注册时 `ProviderRegistry`
    给的是 `NullMemoryBlobStore`，其 `get` 恒返回 None，两者行为一致）。字节确实取回来了才
    返回 `ImagePart`：出网时再发现取不到只能降级成 `[image unavailable]`，那时模型已经
    白等一轮。顺带把 `byte_size` 填上——`utils.image_tokens` 按体积估预算，而 ref 形态的
    `data` 长度与真实体积无关。

    参数名注：子设计 §8 写的是 `get_image(memory, scope, ctx, ref)`，那个 `scope` 指坐标；
    v2 已把坐标改名 `address`（同 `fold.demote_for_budget`），位置参数顺序不变。
    """
    refs = _requested_refs(ref)
    if not refs:
        return [TextPart(text=_NO_REF_TEXT)]

    records: Sequence[Any] = ()
    try:
        records = await memory.load_view(
            address, scope, ctx, kinds=list(kinds) if kinds else None)
    except Exception:
        logger.warning("media:get_image 读视图失败，按「找不到」处理 (address=%s)",
                       address, exc_info=True)

    parts: list[ContentPart] = []
    for one in refs:
        parts.extend(await _restore_one(records, one, ctx, blob_store))
    return parts


async def _restore_one(
    records: Sequence[Any], ref: str, ctx: ProviderContext, blob_store: Any | None,
) -> list[ContentPart]:
    hit = _locate(records, ref)
    if hit is None:
        return [TextPart(text=_not_found_text(ref))]

    blob: tuple[bytes, str] | None = None
    if blob_store is not None:
        try:
            blob = await blob_store.get(ref, ctx)
        except Exception:
            logger.warning("media:get_image 取 blob 失败 (ref=%s)", ref, exc_info=True)
            blob = None
    if not blob:
        return [TextPart(text=_unavailable_text(hit))]

    data, stored_type = blob
    # 占位里的 media_type 是当初落库那张图的，优先用它；只有它退化成 `image` 兜底
    # （`refs._UNKNOWN_MEDIA_TYPE`）时才用 store 记的那个。
    media_type = hit.media_type if "/" in hit.media_type else (stored_type or hit.media_type)
    hit = _Hit(ref=hit.ref, media_type=media_type, user_turn=hit.user_turn,
               in_user_record=hit.in_user_record, role=hit.role)
    return [
        TextPart(text=_restored_text(hit)),
        # source_type="ref"：字节留在 blob store 里，出网前由 adapter rehydrate
        # （子设计 §7「明确不属于本模块」）。这里不塞 base64。
        ImagePart(data=ref, media_type=media_type, source_type="ref",
                  byte_size=len(data) if isinstance(data, (bytes, bytearray)) else None),
    ]


# ── Provider ─────────────────────────────────────────────────────────────────


class MediaCapabilityProvider(ToolCapabilityProvider):
    """core 侧 provider，只暴露 `media:get_image`。

    构造时注入 `ProviderRegistry`（同 `SkillExecutorCapabilityProvider`），memory 与
    blob store **每次调用时**才解析：宿主可能先建 runtime 再 `register_memory` /
    `register_memory_blob_store`，构造期取一次会把接线顺序变成隐性约束
    （`ProviderRegistry.get_memory_blob_store` 的 docstring 记着同款坑）。

    无 per-session 状态，故不实现 `SessionScopedCapabilityProvider`。
    """

    name = PROVIDER_NAME
    description = (
        "Restores images that were dropped from the conversation to save context. "
        "Images dropped this way leave a placeholder carrying their blob:<sha> ref; "
        "media:get_image turns that ref back into the image."
    )

    def __init__(self, providers: Any) -> None:
        self._providers = providers

    def capability(self) -> ToolCapability:
        return ToolCapability(
            id=GET_IMAGE_ID,
            name=GET_IMAGE,
            description=GET_IMAGE_DESCRIPTION,
            input_schema=dict(_INPUT_SCHEMA),
            side_effects=False,
            # 只读、可随时重新派生，且输出是一句话 + 一个 ref —— 落盘只会把 ImagePart
            # 那条路弄复杂（spill 只作用于文本部分），没有任何收益。
            spillable=False,
        )

    def _blob_available(self) -> bool:
        """memory blob store 是否可用——决定 `media:get_image` 是否对模型可见。

        用 **memory** 侧判据而非 event 侧：本工具取的是 L0.5 占位里的 ref，而 L0.5 是
        memory 侧的机制（`compact._media_enabled` 用的是同一个判据，保持一致——本计划
        另有 event blob store，但那是 event 流侧的机制，与本工具无关，不能顺手换用）。

        在**调用时**解析而不是构造时：注册发生在 `Runtime.__init__`，而 host 完全可能
        先构造 Runtime 再 `register_memory()` / `register_memory_blob_store()`——构造期
        判定会让工具永远缺席，即使后来接上了 memory（同 `ProviderRegistry.
        get_memory_blob_store` docstring 记的那个「先取后注册」坑）。

        不吞异常：`get_memory_blob_store()` 未注册时回落到缓存的 `NullMemoryBlobStore()`
        （`.can_externalize` 恒 `False`），文档化为从不抛（`runtime.py` 同函数 docstring）。
        真抛了大概率是别处的接线坏了或契约被改，应该响亮报出来，而不是被这里悄悄吞成
        「工具消失」——那种失败没有异常、没有日志，只会表现成模型突然拿不到这个工具。
        """
        return bool(self._providers.get_memory_blob_store().can_externalize)

    async def list(self, ctx: ProviderContext) -> list[ToolCapability]:
        # 条件可见（spec §8）：没有可用 blob 时不把工具暴露给模型——此刻它取不回任何东西
        # （视图里也不会有 L0.5 占位可读），暴露出来只会占 prompt 位置、诱导无效调用。
        return [self.capability()] if self._blob_available() else []

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name, capability_count=1 if self._blob_available() else 0,
            supports_streaming=False, supports_cancel=False,
            description=self.description,
        )

    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        return None

    def invoke(
        self, capability_id: str, arguments: dict[str, Any], ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        return self._handle(capability_id, arguments, ctx)

    async def _handle(
        self, capability_id: str, arguments: dict[str, Any], ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        # 惰性 import：`capability_gateway` 属 core.loop，而 Task 5 会让 core.orchestrator
        # 的 compact 反过来 import core.media —— 模块级引用会把两边绑成一个 import 环。
        from ctx_weft.core.loop.capability_gateway import CONTENT_PARTS_KEY

        if capability_id.split(":")[-1] != GET_IMAGE:
            yield CapabilityEvent(kind="error", payload={
                "code": "UNKNOWN_MEDIA_CAPABILITY",
                "message": f"Unknown media capability: {capability_id}",
            })
            return

        try:
            memory = self._providers.get_memory()
        except Exception as exc:            # 接线错误（memory 未注册）——响亮报错，别装作找不到图
            yield CapabilityEvent(kind="error", payload={
                "code": "MEDIA_NO_MEMORY", "message": str(exc)})
            return
        try:
            blob_store = self._providers.get_memory_blob_store()
        except Exception:
            logger.warning("media:get_image 取 blob store 失败，按未注册处理", exc_info=True)
            blob_store = None

        address = MemoryAddress(session_id=ctx.session_id, task_id=ctx.task_id,
                                agent_id=ctx.agent_id)
        parts = await get_image(memory, address, ctx, arguments.get("ref"),
                                blob_store=blob_store)

        # 文本走正常的工具文本输出，图片 part 走 CONTENT_PARTS_KEY 通道交给 gateway
        # （Task 3 建好的通用接缝）——不在这里自己拼 content。
        # 判据 `not hasattr(p, "text")` 冻结（裁定 D1）。
        text = "\n".join(p.text for p in parts if hasattr(p, "text"))
        media_parts = [p for p in parts if not hasattr(p, "text")]
        payload: dict[str, Any] = {"content": text, "metadata": {}}
        if media_parts:
            payload["metadata"] = {CONTENT_PARTS_KEY: media_parts}
        yield CapabilityEvent(kind="result", payload=payload)
