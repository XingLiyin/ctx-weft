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

from loomex_core.protocols import (
    LLMCallError, LLMChunk, LLMMessage, LLMRequest, LLMTool, LLMUsage, ToolCall, LLMClient,
)
from loomex_core.core.utils import estimate_tokens

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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._context_limit = context_limit
        self._max_output_tokens = max_output_tokens
        self._timeout = timeout_sec
        self._max_http_retries = max_http_retries
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

        for attempt in range(self._max_http_retries):
            tool_call_buffers: dict[int, dict[str, Any]] = {}
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload) as resp:
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

                        raise LLMCallError(
                            f"OpenAI API error {resp.status_code}: {err_body}",
                            status_code=resp.status_code,
                            retriable=resp.status_code in _RETRIABLE_CODES,
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

                        choices = event.get("choices") or []
                        if not choices:
                            usage_data = event.get("usage") or {}
                            if usage_data:
                                yield LLMChunk(
                                    kind="usage",
                                    usage=LLMUsage(
                                        prompt_tokens=usage_data.get("prompt_tokens", 0),
                                        completion_tokens=usage_data.get("completion_tokens", 0),
                                        total_tokens=usage_data.get("total_tokens", 0),
                                    ),
                                )
                            continue

                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        finish_reason = choice.get("finish_reason")

                        reasoning = delta.get("reasoning_content") or ""
                        if reasoning:
                            yield LLMChunk(kind="reasoning", text=reasoning)

                        text = delta.get("content") or ""
                        if text:
                            yield LLMChunk(kind="token", text=text)

                        for tc_delta in (delta.get("tool_calls") or []):
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                            buf = tool_call_buffers[idx]
                            if tc_delta.get("id"):
                                buf["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                buf["name"] += fn["name"]
                            if fn.get("arguments"):
                                buf["arguments"] += fn["arguments"]

                        if finish_reason:
                            for buf in tool_call_buffers.values():
                                try:
                                    args = json.loads(buf["arguments"]) if buf["arguments"] else {}
                                except json.JSONDecodeError:
                                    args = {"_raw": buf["arguments"]}
                                yield LLMChunk(
                                    kind="tool_call",
                                    tool_call=ToolCall(id=buf["id"], name=buf["name"], arguments=args),
                                )
                            yield LLMChunk(
                                kind="done",
                                finish_reason=finish_reason,
                            )
                            return

                    return

            except httpx.TransportError as exc:
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
            payload["tool_choice"] = "auto"
        return payload


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
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]
