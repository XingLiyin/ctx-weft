"""Async OpenAI-compatible LLM adapter (legacy max_tokens transport).

Implements LLMClient protocol using httpx for async HTTP.
Compatible with OpenAI chat completions API + any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ctx_weft.protocols import (
    LLMChunk,
    LLMClient,
    LLMMessage,
    LLMRequest,
    LLMUsage,
    ToolCall,
)
from ctx_weft.providers.llm.adapters._errors import http_error, transport_error
from ctx_weft.providers.llm.adapters._finalize import build_finalize_chunks, parse_tool_arguments
from ctx_weft.providers.llm.adapters._modality import downgrade_for_text_only
from ctx_weft.providers.llm.adapters._openai_chat import (
    _TOOL_IMAGE_NOTICE as _TOOL_IMAGE_NOTICE,
)
from ctx_weft.providers.llm.adapters._openai_chat import (
    _dump_tool_arguments as _dump_tool_arguments,
)
from ctx_weft.providers.llm.adapters._openai_chat import (
    _map_tools,
    _serialize_messages,
)
from ctx_weft.providers.llm.adapters._openai_chat import (
    _parts_to_blocks as _parts_to_blocks,
)
from ctx_weft.providers.llm.adapters._openai_chat import (
    _parts_to_text as _parts_to_text,
)
from ctx_weft.providers.llm.adapters._stream_parsing import begin_parsed_attempt
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer

_OPENAI_API_URL = "https://api.openai.com"

class OpenAICompatibleAdapter(LLMClient):
    """Async OpenAI chat completions adapter."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = _OPENAI_API_URL,
        context_limit: int = 128_000,
        output_reserve: int = 4096,
        timeout_sec: int = 120,
        tool_choice: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._context_limit = context_limit
        self._output_reserve = output_reserve
        self._timeout = timeout_sec
        # None = 省略 tool_choice（vLLM 不开 --enable-auto-tool-choice 会拒绝带 "auto" 的请求）。
        # 显式给 "auto"/"none"/"required" 才发送。
        self._tool_choice = tool_choice
        self._client = self._make_client()
        self._tokenizers: dict[str, HeuristicTokenizer] = {}

    def tokenizer_for(self, model: str) -> HeuristicTokenizer:
        """按 model 惰性分桶的校准 tokenizer（_FixedModelClient 经此取绑定模型那只）。"""
        if model not in self._tokenizers:
            self._tokenizers[model] = HeuristicTokenizer()
        return self._tokenizers[model]

    @property
    def tokenizer(self) -> HeuristicTokenizer:
        return self.tokenizer_for(self._model)

    @property
    def model(self) -> str:
        """构造时配置的模型名（duck-typed，非协议必需）：host 裸 adapter 直传时，
        runtime 执行前经 getattr 读取回填 session.llm_model——实际调用一直用它替换
        "mock"（见 _build_payload），事件账面须与之同源。"""
        return self._model

    @property
    def context_limit(self) -> int:
        return self._context_limit

    @property
    def output_reserve(self) -> int:
        return self._output_reserve

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def complete(self, request: LLMRequest, stream: bool = True) -> AsyncIterator[LLMChunk]:
        return self._stream(request)

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMChunk]:
        payload = self._build_payload(request)
        headers = self._headers()
        url = self._chat_url()

        produced = False  # 是否已向消费者吐过 chunk（流已开始）
        state = begin_parsed_attempt()
        last_tc_idx: int | None = None  # 供缺 index 的续传 delta 归到最后活跃桶
        finish_reason: str | None = None
        usage: LLMUsage | None = None
        try:
            # 流式 read 超时 = 闲置(自上一字节起)超时,而非整段时长上限:正常流(token/思考/
            # 工具参数分片)持续来字节会不断重置计时器,故长工具调用/长思考不会误触发。仅当连接
            # 静默(中途断网/死 socket,无字节无 RST)超过 timeout 才触发 ReadTimeout(TransportError)
            # → 抛 outage，由 gateway 决定首包前重试或流中断。
            async with self._client.stream(
                "POST", url, headers=headers, json=payload,
                timeout=httpx.Timeout(self._timeout, read=self._timeout),
            ) as resp:
                if resp.status_code != 200:
                    raise http_error("OpenAI", resp)

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    # usage 可能随 finish_reason 同包，也可能在其后的 choices=[] 尾包里——
                    # 任何带 usage 的 event 都更新，并读到流尾再产出（不再 finish 即 return）。
                    usage_data = event.get("usage") or {}
                    if usage_data:
                        prompt_details = usage_data.get("prompt_tokens_details") or {}
                        completion_details = usage_data.get("completion_tokens_details") or {}
                        # 缓存命中：标准 prompt_tokens_details.cached_tokens →
                        # DeepSeek 方言 prompt_cache_hit_tokens → 0（方言封死在 adapter 内）
                        cache_read = (prompt_details.get("cached_tokens")
                                      or usage_data.get("prompt_cache_hit_tokens") or 0)
                        usage = LLMUsage(
                            prompt_tokens=usage_data.get("prompt_tokens", 0),
                            completion_tokens=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                            cache_read_tokens=cache_read,
                            cache_write_tokens=0,  # OpenAI 系不区分/不计费缓存写入
                            # input_tokens 不传 → __post_init__ 派生 prompt − cached
                            # （OpenAI 只报 cached 子集，无未缓存原始值可记）
                            reasoning_tokens=completion_details.get("reasoning_tokens", 0) or 0,
                        )

                    choices = event.get("choices") or []
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        produced = True
                        yield LLMChunk(kind="reasoning", text=reasoning)

                    text = delta.get("content") or ""
                    if text:
                        tok = state.feed_text(text)
                        if tok:
                            produced = True
                            yield LLMChunk(kind="token", text=tok)

                    tc_deltas = delta.get("tool_calls") or []
                    for tc_delta in tc_deltas:
                        idx = _resolve_tc_index(tc_delta, state.tool_buffers, last_tc_idx)
                        last_tc_idx = idx
                        fn = tc_delta.get("function") or {}
                        name = fn.get("name")
                        args = fn.get("arguments")
                        if args:
                            args = args if isinstance(args, str) else json.dumps(args)
                        state.feed_tool(
                            idx, call_id=tc_delta.get("id") or "",
                            name=(name if isinstance(name, str) else str(name)) if name else "",
                            arguments=args or "", append_name=True,
                        )
                    if tc_deltas:
                        # 工具调用参数流式累积期间不产出 token：消费者的 async-for 会一直挂起，
                        # act 流式循环顶部的暂停/取消检查点无从触发 → 发个无负载心跳让其有机会运行。
                        # 同时标记 produced：本轮已在生成工具调用，若此后 transport 断流，走 retriable
                        # 让 gateway 走中断入口，不自动重发本轮请求。
                        produced = True
                        yield LLMChunk(kind="tool_call_partial")

                # 流以任何方式结束（[DONE] / 自然结束）→ 统一收尾：截断判定、
                # native 规整、文本/思考还原、usage、done（见 _finalize）。
                for ch in build_finalize_chunks(
                    content_text=state.content_text,
                    native_tool_calls=_parse_buffers(state.tool_buffers),
                    had_native_buffer=bool(state.tool_buffers),
                    saw_terminal=finish_reason is not None,
                    usage=usage,
                    finish_reason=finish_reason,
                    emitted_visible_len=state.gate.emitted_len,
                ):
                    yield ch
                return

        except (httpx.TransportError, httpx.InvalidURL) as exc:
            raise transport_error("OpenAI", exc, produced=produced) from exc

    async def list_models(self) -> list[str]:
        """List available model ids via GET {base}/v1/models. Raises on HTTP error."""
        resp = await self._client.get(self._api_url("models"), headers=self._headers())
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]

    def _make_client(self) -> httpx.AsyncClient:
        """Build the httpx client. Overridable to inject SSL verification."""
        return httpx.AsyncClient(timeout=self._timeout)

    def _chat_url(self) -> str:
        """Chat-completions endpoint. Overridable for endpoint inference."""
        return self._api_url("chat/completions")

    def _api_url(self, operation: str) -> str:
        """Standard API path. Hosts may override URL policy by subclassing."""
        return f"{self._base_url}/v1/{operation}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        """出网前的模态处置——**本类是纯文本 adapter**，图片降级成文本占位并告警。

        能力由「注册了哪个 adapter 类」表达（spec 2026-08-28 §2「类型即声明」）：
        要发图请用 ``OpenAICompatibleMultimodalAdapter``，它覆盖本方法为原样透传。

        降级之后 ``_serialize_messages`` 里的 tool 图重定位（``_TOOL_IMAGE_NOTICE``）
        天然空转：已无 image part，``relocated`` 恒为空，一条 user 消息都不追加。
        故那段逻辑不需要任何改动。

        纯文本请求返回同一对象、不记日志（无图会话逐字节不受影响）。
        """
        return downgrade_for_text_only(
            request, adapter_hint="OpenAICompatibleMultimodalAdapter")

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model if request.model and request.model != "mock" else self._model
        messages = _serialize_messages(request.system, self._prepare_messages(request))
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.temperature != 1.0:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = _map_tools(request.tools)
            if self._tool_choice is not None:
                payload["tool_choice"] = self._tool_choice
        return payload


class OpenAICompatibleMultimodalAdapter(OpenAICompatibleAdapter):
    """支持图片输入的 OpenAI adapter。

    与基类的唯一区别：``_prepare_messages`` 原样透传，图片得以走到
    ``_serialize_messages`` 面前——被拼成 ``image_url`` data URL，且 ``role="tool"``
    里的图按既有逻辑重定位到随后的 user 消息（OpenAI 的 tool 只收纯文本）。

    能力由类型表达（spec 2026-08-28 §2）——host 注册哪个类，就是在声明该账号下的
    模型收不收图。core 对此零判断。
    """

    def _prepare_messages(self, request: LLMRequest) -> list[LLMMessage]:
        return request.messages


def _resolve_tc_index(
    tc_delta: dict[str, Any], buffers: dict[int, dict[str, Any]], last_idx: int | None,
) -> int:
    """决定本条 tool_call delta 该并入哪个 index 桶（容忍上游漏发 index）。

    OpenAI 流式协议里，一个 tool call 的首 delta 带 ``index``+``id``+``name``，其后的参数
    续传 delta 只带 ``index``。部分兼容端（vLLM/国产模型）在续传 delta 甚至第二个 call 上漏发
    ``index``；旧逻辑一律 ``get("index", 0)`` 会把它们错并进桶 0，参数首尾相接成畸形。规则：

      - 显式带 ``index`` → 用它（协议正道）。
      - 缺 ``index`` 但带 ``id``：id 命中现有桶 → 复用该桶（有的端每片都回带 id）；否则是
        「新 call」→ 另开一个新桶（现有最大 index + 1）。
      - 缺 ``index`` 且不带 ``id`` → 纯参数续传 → 归最后活跃桶（``last_idx``，无则 0）。
    """
    raw_idx = tc_delta.get("index")
    if raw_idx is not None:
        return raw_idx
    tc_id = tc_delta.get("id")
    if tc_id:
        for k, buf in buffers.items():
            if buf.get("id") == tc_id:
                return k
        return max(buffers) + 1 if buffers else 0
    return last_idx if last_idx is not None else 0


def _parse_buffers(buffers: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """把累积的 tool_call 缓冲解析成规整 ToolCall：丢弃 id/name 为空者；
    arguments 解析失败先试畸形救援，仍不行才保留 ``{"_raw": ...}`` 交 gateway 报错（不静默丢）。"""
    calls: list[ToolCall] = []
    for buf in buffers.values():
        if not buf["id"] or not buf["name"]:
            continue
        args = parse_tool_arguments(buf["arguments"])
        calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))
    return calls


