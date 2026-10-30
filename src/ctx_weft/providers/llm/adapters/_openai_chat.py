"""Chat Completions message and tool serialization shared by both transports."""

from __future__ import annotations

import json
from typing import Any

from ctx_weft.protocols import RAW_ARGS_KEY, LLMMessage, LLMTool
from ctx_weft.providers.llm.adapters._schema import sanitize_boolean_schemas

# OpenAI 的 role="tool" 只接受纯文本，图片必须重定位到随后的 user 消息（Phase 3c Task B）。
# 这条标记把「图去哪了」告诉模型，替代改前的**静默丢弃**。
#
# **必须逐字节确定**（同用户裁定 D2 的缓存理由）：不得含 blob sha / 随机 id / 时间戳 /
# 跨调用计数器——tool 结果处在缓存前缀的中段，标记每次不同会把其后的整段前缀缓存砸掉。
# 同一段内的多张图共用同一条标记，不加序号（图按 tool 消息顺序合并进同一条 user 消息，
# 模型不需要靠序号对齐）。
# 本仓所有图片占位的清单见 `core/media/refs.py` 模块 docstring（L6 收口，裁定 R1）。
# 这一条是**单向渲染、永不回读**，故留在 wire 层不搬进 `core/media/`；
# 只有 L0.5 的占位需要被解析回来。文案为英文，与清单里其余四条一致。
_TOOL_IMAGE_NOTICE = "\n\n[image see the following message]"


def _dump_tool_arguments(args: Any) -> str:
    """把 assistant tool_call 的参数序列化回给模型（OpenAI 要求 arguments 为 JSON 字符串）。

    不变式：**发出去的 arguments 必须永远是合法 JSON**——严格 OpenAI 兼容端（vLLM 等）会对
    历史 tool_call 的 arguments 再做一次 ``json.loads``，非法 JSON 直接 400
    （``Expecting ',' delimiter``）。

    兜底 ``{"_raw": <原文>}``（真畸形、finalize 没能解包）绝不把 ``_raw`` 哨兵当参数名喂回去
    （模型会照抄 ``_raw`` 死循环），也绝不逐字回吐畸形串（严格服务端会二次解析 → 400）：
      - 原文本身是合法 JSON（罕见的流式拼接抖动救回、finalize 未解包的非 dict）→ 照发（服务端能解析）。
      - 原文畸形 → 降级成合法空对象 ``"{}"``。畸形调用仍以 tool_call 形态流经 gateway，gateway 回一条
        带原文的 error tool_result 驱动模型下轮自纠（见 capability_gateway ``_raw`` 出口）；本处只需
        保证线上 arguments 合法，畸形原文不靠这格传回。
    """
    if isinstance(args, dict) and list(args) == [RAW_ARGS_KEY] and isinstance(args[RAW_ARGS_KEY], str):
        raw = args[RAW_ARGS_KEY]
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            return "{}"
        return raw
    return json.dumps(args)


def _serialize_messages(
    system: str, messages: list[LLMMessage]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})

    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            content: Any = (m.content if isinstance(m.content, str)
                            else _parts_to_blocks(m.content))
            tc_list = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": _dump_tool_arguments(tc.get("arguments", tc.get("input", {}))),
                    },
                }
                for tc in (m.tool_calls or [])
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": content or None, "tool_calls": tc_list}
            if m.reasoning_content:
                msg["reasoning_content"] = m.reasoning_content
            result.append(msg)
            i += 1
        elif m.role == "tool":
            # 批处理**整段连续 tool 消息**（与 anthropic.py 的 tool 分支同构）：每条仍只发
            # 文本，图片攒起来，整段结束后合并成一条 user 消息追加（见 _TOOL_IMAGE_NOTICE）。
            relocated: list[Any] = []
            while i < len(messages) and messages[i].role == "tool":
                tm = messages[i]
                if isinstance(tm.content, str):
                    # 纯文本 str 形态：原样透传，wire 逐字节不变。
                    content = tm.content
                else:
                    images = [p for p in (tm.content or [])
                              if getattr(p, "type", None) == "image"]
                    text = _parts_to_text(tm.content)
                    if images:
                        relocated.extend(images)
                        # 纯图片结果（text 为空）时不拼前导空行——OpenAI 的 tool content
                        # 为空会被拒（同 anthropic.py 的 _EMPTY_TOOL_RESULT_CONTENT 理由），
                        # 标记本身就是那条非空占位。
                        content = (f"{text}{_TOOL_IMAGE_NOTICE}" if text
                                   else _TOOL_IMAGE_NOTICE.lstrip("\n"))
                    else:
                        content = text
                result.append({
                    "role": "tool",
                    "tool_call_id": tm.tool_call_id or "",
                    "content": content,
                })
                i += 1
            if relocated:
                result.append({"role": "user", "content": _parts_to_blocks(relocated)})
        else:
            content = (m.content if isinstance(m.content, str)
                       else _parts_to_blocks(m.content))
            entry: dict[str, Any] = {"role": m.role, "content": content}
            if m.role == "assistant" and m.reasoning_content:
                entry["reasoning_content"] = m.reasoning_content
            result.append(entry)
            i += 1

    return result


def _parts_to_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        texts: list[str] = []
        for p in parts:
            # dict 形态 part 的分支已删（Phase 3c Task E/E2）：dict 是**协议违规**，已在
            # ``LLMMessage`` / ``MemoryRecord`` / ``MemoryEvent`` 三处 ``__post_init__``
            # 边界归一成 dataclass；留着分支会让后来者以为 dict 是受支持的形态。
            if hasattr(p, "text"):
                texts.append(p.text)
            # else: 非文本 dataclass part（ImagePart 等）——跳过，语义对齐 core/utils.content_to_text
        return " ".join(texts)
    return str(parts)


def _parts_to_blocks(parts: Any) -> list[dict[str, Any]]:
    """把 ContentPart 列表转成 OpenAI wire blocks（文本 → text block，图片 → image_url block）。

    到达本函数时 source_type 已恒为 "base64"——**这不是数据模型的性质，而是上游的
    保证**：Phase 3b 起图片在入口被外部化成 ``blob:<sha>`` ref，由
    ``core.loop.llm_gateway.stream_llm`` 在出网前（本函数之前的最后一个 async 关口）
    调 ``rehydrate_content`` 还原回 base64（架构裁定 T0：本函数是同步的，
    ``MemoryBlobStore.get`` 是 async，没法在这里 await）。取不到图时上游已把该 part 降级成
    文本占位，故本函数无需处理 ref。**绕过 gateway 直调 adapter 的路径上此保证不成立**，
    那条路上的 ref 会被拼进 data URL。"""
    blocks: list[dict[str, Any]] = []
    for p in parts:
        # dict 形态 part 的分支已删（Phase 3c Task E/E2）：dict 是**协议违规**，已在
        # ``LLMMessage.__post_init__``（本函数入参恒为 ``LLMMessage.content``）与
        # ``MemoryRecord`` / ``MemoryEvent`` 三处边界归一成 dataclass，到这里不再有 dict。
        # 万一有（绕过构造器直调本函数），落到最后一支 → ``getattr`` 取不到 → 当作空文本
        # 跳过，不会 raise：adapter 在同步出网主路径上，任何 raise 都会掀掉整个 LLM 请求。
        if getattr(p, "type", None) == "image":
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{p.media_type};base64,{p.data}"},
            })
        else:
            # I1: 同上对 None 兜底；注意不能改成 `or str(p)`——那会把无 .text 属性的
            # 对象整个 repr 当文本泄漏进发给模型的文本（Phase 2 修过的同类泄漏）。
            text = getattr(p, "text", None) or ""
            if text.strip():  # 同上：纯空白块与空块同属 provider 400 的一类（spec §13）
                blocks.append({"type": "text", "text": text})
    return blocks


def _map_tools(tools: list[LLMTool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": sanitize_boolean_schemas(
                    t.input_schema or {"type": "object", "properties": {}}
                ),
            },
        }
        for t in tools
    ]
