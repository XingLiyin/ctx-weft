"""Async OpenAI (and compatible) LLM adapter.

Implements LLMClient protocol using httpx for async HTTP.
Compatible with OpenAI chat completions API + any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ctx_weft.protocols import (
    LLMCallError, LLMChunk, LLMMessage, LLMRequest, LLMTool, LLMUsage, ToolCall, LLMClient,
)
from ctx_weft.core.utils import estimate_tokens
from ctx_weft.providers.llm._finalize import build_finalize_chunks
from ctx_weft.providers.llm._schema import sanitize_boolean_schemas
from ctx_weft.providers.llm.text_calls import ContentGate, merge_content as _merge_content

logger = logging.getLogger(__name__)

_OPENAI_API_URL = "https://api.openai.com"

_NON_RETRIABLE_CODES = frozenset({400, 401, 403, 404})
_RETRIABLE_CODES     = frozenset({429, 500, 502, 503, 504})


class OpenAIAdapter(LLMClient):
    """Async OpenAI chat completions adapter."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = _OPENAI_API_URL,
        context_limit: int = 128_000,
        max_output_tokens: int = 4096,
        timeout_sec: int = 120,
        max_http_retries: int = 3,
        tool_choice: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._context_limit = context_limit
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout_sec
        self._max_http_retries = max_http_retries
        # None = 省略 tool_choice（vLLM 不开 --enable-auto-tool-choice 会拒绝带 "auto" 的请求）。
        # 显式给 "auto"/"none"/"required" 才发送。
        self._tool_choice = tool_choice
        self._client = self._make_client()

    @property
    def context_limit(self) -> int:
        return self._context_limit

    @property
    def max_output_tokens(self) -> int:
        return self._max_output_tokens

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
        for attempt in range(self._max_http_retries):
            tool_call_buffers: dict[int, dict[str, Any]] = {}
            content_text = ""
            finish_reason: str | None = None
            usage: LLMUsage | None = None
            gate = ContentGate()  # 增量剥离 <think>/<tool_call> 标签
            try:
                # 流式 read 超时 = 闲置(自上一字节起)超时,而非整段时长上限:正常流(token/思考/
                # 工具参数分片)持续来字节会不断重置计时器,故长工具调用/长思考不会误触发。仅当连接
                # 静默(中途断网/死 socket,无字节无 RST)超过 timeout 才触发 ReadTimeout(TransportError)
                # → produced=True 时抛 outage → INTERRUPTED;否则会永久挂起。
                async with self._client.stream(
                    "POST", url, headers=headers, json=payload,
                    timeout=httpx.Timeout(self._timeout, read=self._timeout),
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        err_body = body.decode()

                        if resp.status_code in _NON_RETRIABLE_CODES:
                            raise LLMCallError(
                                f"OpenAI API error {resp.status_code}: {err_body}",
                                status_code=resp.status_code,
                                retriable=False,
                            )

                        if resp.status_code in _RETRIABLE_CODES and attempt < self._max_http_retries - 1:
                            try:
                                delay = float(resp.headers.get("retry-after", 2 ** attempt))
                            except (ValueError, TypeError):
                                delay = float(2 ** attempt)
                            delay = min(delay, 60.0)
                            logger.warning(
                                "OpenAI API %d (attempt %d/%d), retrying in %.1fs: %.200s",
                                resp.status_code, attempt + 1, self._max_http_retries, delay, err_body,
                            )
                            await asyncio.sleep(delay)
                            continue

                        retry_after_hdr = resp.headers.get("retry-after")
                        try:
                            retry_after_val = float(retry_after_hdr) if retry_after_hdr else None
                        except (ValueError, TypeError):
                            retry_after_val = None
                        is_retriable = resp.status_code in _RETRIABLE_CODES
                        raise LLMCallError(
                            f"OpenAI API error {resp.status_code}: {err_body}",
                            status_code=resp.status_code,
                            retriable=is_retriable,
                            outage=is_retriable,
                            retry_after_sec=retry_after_val,
                        )

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
                            usage = LLMUsage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
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
                            content_text = _merge_content(content_text, text)
                            tok = gate.feed(content_text)
                            if tok:
                                produced = True
                                yield LLMChunk(kind="token", text=tok)

                        tc_deltas = delta.get("tool_calls") or []
                        for tc_delta in tc_deltas:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                            buf = tool_call_buffers[idx]
                            if tc_delta.get("id"):
                                buf["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            name = fn.get("name")
                            if name:
                                buf["name"] += name if isinstance(name, str) else str(name)
                            args = fn.get("arguments")
                            if args:
                                buf["arguments"] += args if isinstance(args, str) else json.dumps(args)
                        if tc_deltas:
                            # 工具调用参数流式累积期间不产出 token：消费者的 async-for 会一直挂起，
                            # act 流式循环顶部的暂停/取消检查点无从触发 → 发个无负载心跳让其有机会运行。
                            # 同时标记 produced：本轮已在生成工具调用，若此后 transport 断流，走 retriable
                            # 让任务层干净整跑，而非静默 inline 重发整个长工具调用（见 except 分支）。
                            produced = True
                            yield LLMChunk(kind="tool_call_partial")

                    # 流以任何方式结束（[DONE] / 自然结束）→ 统一收尾：截断判定、
                    # native 规整、文本/思考还原、usage、done（见 _finalize）。
                    for ch in build_finalize_chunks(
                        content_text=content_text,
                        native_tool_calls=_parse_buffers(tool_call_buffers),
                        had_native_buffer=bool(tool_call_buffers),
                        saw_terminal=finish_reason is not None,
                        usage=usage,
                        finish_reason=finish_reason,
                        emitted_visible_len=gate.emitted_len,
                    ):
                        yield ch
                    return

            except httpx.TransportError as exc:
                # 已吐过 chunk（流中断）→ 不在 adapter 内重试（会重复 token），
                # 抛 retriable 让 TaskManager 整任务干净重跑。
                if produced:
                    logger.warning(
                        "OpenAI stream interrupted mid-flight (tool_call_in_progress=%s): %s",
                        bool(tool_call_buffers), exc,
                    )
                    raise LLMCallError(
                        f"LLM stream interrupted mid-flight: {exc}", retriable=True, outage=True,
                    ) from exc
                if attempt < self._max_http_retries - 1:
                    delay = float(2 ** attempt)
                    logger.warning(
                        "OpenAI transport error (attempt %d/%d), retrying in %.1fs: %s",
                        attempt + 1, self._max_http_retries, delay, exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LLMCallError(str(exc), retriable=False) from exc

    async def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    async def list_models(self) -> list[str]:
        """List available model ids via GET {base}/v1/models. Raises on HTTP error."""
        resp = await self._client.get(f"{self._base_url}/v1/models", headers=self._headers())
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]

    def _make_client(self) -> httpx.AsyncClient:
        """Build the httpx client. Overridable to inject SSL verification."""
        return httpx.AsyncClient(timeout=self._timeout)

    def _chat_url(self) -> str:
        """Chat-completions endpoint. Overridable for endpoint inference."""
        return f"{self._base_url}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        model = request.model if request.model and request.model != "mock" else self._model
        messages = _serialize_messages(request.system, request.messages)
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


def _parse_buffers(buffers: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """把累积的 tool_call 缓冲解析成规整 ToolCall：丢弃 id/name 为空者；
    arguments JSON 解析失败保留 ``{"_raw": ...}`` 交 gateway 报错（不静默丢）。"""
    calls: list[ToolCall] = []
    for buf in buffers.values():
        if not buf["id"] or not buf["name"]:
            continue
        try:
            args = json.loads(buf["arguments"]) if buf["arguments"] else {}
        except json.JSONDecodeError:
            args = {"_raw": buf["arguments"]}
        calls.append(ToolCall(id=buf["id"], name=buf["name"], arguments=args))
    return calls


def _serialize_messages(
    system: str, messages: list[LLMMessage]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})

    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            content: Any = m.content if isinstance(m.content, str) else _parts_to_text(m.content)
            tc_list = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("arguments", tc.get("input", {}))),
                    },
                }
                for tc in (m.tool_calls or [])
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": content or None, "tool_calls": tc_list}
            if m.reasoning_content:
                msg["reasoning_content"] = m.reasoning_content
            result.append(msg)
        elif m.role == "tool":
            content = m.content if isinstance(m.content, str) else _parts_to_text(m.content)
            result.append({
                "role": "tool",
                "tool_call_id": m.tool_call_id or "",
                "content": content,
            })
        else:
            content = m.content if isinstance(m.content, str) else _parts_to_text(m.content)
            entry: dict[str, Any] = {"role": m.role, "content": content}
            if m.role == "assistant" and m.reasoning_content:
                entry["reasoning_content"] = m.reasoning_content
            result.append(entry)

    return result


def _parts_to_text(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        return " ".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in parts
        )
    return str(parts)


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
