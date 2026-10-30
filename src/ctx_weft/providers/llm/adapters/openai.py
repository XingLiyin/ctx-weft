"""Official OpenAI Chat Completions adapter, composed with the Python SDK."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    omit,
)
from openai.lib.streaming.chat import AsyncChatCompletionStream

from ctx_weft.protocols import RAW_ARGS_KEY, LLMCallError, LLMChunk, LLMRequest, LLMUsage, ToolCall
from ctx_weft.protocols.llm import LLMStreamBuffers, current_stream_writer
from ctx_weft.providers.llm.adapters._errors import http_error, transport_error
from ctx_weft.providers.llm.adapters._modality import downgrade_for_text_only
from ctx_weft.providers.llm.adapters._openai_chat import _map_tools, _serialize_messages
from ctx_weft.providers.llm.tokenizer import HeuristicTokenizer


def _sdk_base_url(base_url: str) -> str:
    """Accept service roots, API prefixes and existing Chat Completions URLs."""
    url = urlsplit(base_url.strip())
    if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
        raise ValueError("base_url must be an HTTP(S) URL without query or fragment")
    path = url.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)]
            break
    else:
        if not re.fullmatch(r"v\d+", path.rsplit("/", 1)[-1], re.IGNORECASE):
            path += "/v1"
    return urlunsplit((url.scheme, url.netloc, path + "/", "", ""))


class OpenAIAdapter:
    """LLMClient implementation for protocol ``openai-official``.

    The SDK owns HTTP and SSE decoding. This adapter only translates the runtime
    contract. It does not inherit from the OpenAI-compatible implementation.
    ``reasoning_effort_provider`` is evaluated per request, so host context-local
    reasoning controls remain safe when the adapter is shared between sessions.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com",
        context_limit: int = 128_000,
        output_reserve: int = 4096,
        timeout_sec: int = 120,
        tool_choice: str | None = None,
        *,
        image_input: bool = False,
        http_client: httpx.AsyncClient | None = None,
        reasoning_effort_provider: Callable[[], str | None] | None = None,
    ) -> None:
        api_base = _sdk_base_url(base_url)
        self._model = model
        self._context_limit = context_limit
        self._output_reserve = output_reserve
        self._tool_choice = tool_choice
        self._image_input = image_input
        self._reasoning_effort_provider = reasoning_effort_provider
        self._tokenizers: dict[str, HeuristicTokenizer] = {}
        self._sdk = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=httpx.Timeout(timeout_sec, read=timeout_sec),
            max_retries=0,
            http_client=http_client,
        )

    @property
    def model(self) -> str:
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

    def tokenizer_for(self, model: str) -> HeuristicTokenizer:
        if model not in self._tokenizers:
            self._tokenizers[model] = HeuristicTokenizer()
        return self._tokenizers[model]

    @property
    def tokenizer(self) -> HeuristicTokenizer:
        return self.tokenizer_for(self._model)

    async def aclose(self) -> None:
        await self._sdk.close()

    def complete(self, request: LLMRequest, stream: bool = True) -> AsyncIterator[LLMChunk]:
        return self._complete(request, stream)

    async def _complete(self, request: LLMRequest, stream: bool) -> AsyncIterator[LLMChunk]:
        runtime_messages = request.messages if self._image_input else downgrade_for_text_only(
            request, adapter_hint="OpenAIAdapter(image_input=True)",
        )
        messages = _serialize_messages(request.system, runtime_messages)
        for message in messages:
            message.pop("reasoning_content", None)
        effort = self._reasoning_effort_provider() if self._reasoning_effort_provider else None
        writer = current_stream_writer()
        buffers = writer.begin_attempt() if writer is not None else LLMStreamBuffers()
        if writer is not None:
            writer.attach_parser(_OfficialStreamParser(buffers))
        produced = False
        try:
            response = await self._sdk.chat.completions.create(
                model=request.model if request.model and request.model != "mock" else self._model,
                messages=messages,
                max_completion_tokens=request.max_tokens if request.max_tokens is not None else omit,
                temperature=request.temperature if request.temperature != 1.0 else omit,
                tools=_map_tools(request.tools) if request.tools else omit,
                tool_choice=self._tool_choice if request.tools and self._tool_choice is not None else omit,
                reasoning_effort=effort if effort is not None else omit,
                stream=stream,
                stream_options={"include_usage": True} if stream else omit,
            )
            if stream:
                # The convenience .stream() method requires strict tool schemas.
                # Its public SDK stream class also assembles non-strict tools,
                # without changing the runtime's existing schema semantics.
                async with AsyncChatCompletionStream(
                    raw_stream=response, response_format=omit, input_tools=[],
                ) as sdk_stream:
                    saw_chunk = False
                    async for event in sdk_stream:
                        if event.type == "chunk":
                            saw_chunk = True
                        elif event.type in {"content.delta", "refusal.delta"}:
                            # Borrow the SDK's accumulated string; do not assemble
                            # payloads or duplicate the full text in another buffer.
                            buffers.content_text = event.snapshot
                            produced = True
                            yield LLMChunk(kind="token", text=event.delta)
                        elif event.type == "tool_calls.function.arguments.delta":
                            produced = True
                            yield LLMChunk(kind="tool_call_partial")
                    if not saw_chunk:
                        raise LLMCallError("OpenAI stream ended empty", retriable=True, outage=True)
                    try:
                        completion = await sdk_stream.get_final_completion()
                    except LengthFinishReasonError as exc:
                        completion = exc.completion
            else:
                completion = response
                if completion.choices:
                    message = completion.choices[0].message
                    text = message.content or message.refusal or ""
                    if text:
                        buffers.content_text = text
                        produced = True
                        yield LLMChunk(kind="token", text=text)
            if not completion.choices or completion.choices[0].finish_reason is None:
                raise LLMCallError(
                    "OpenAI response ended without a terminal choice", retriable=True, outage=True,
                )
            choice = completion.choices[0]
            if choice.message.tool_calls and choice.finish_reason == "length":
                raise LLMCallError("OpenAI tool call exceeded its output budget", retriable=True)
            calls = [_map_tool_call(call) for call in choice.message.tool_calls or ()]
            if completion.usage is not None:
                yield LLMChunk(kind="usage", usage=_map_usage(completion.usage), finish_reason=choice.finish_reason)
            for call in calls:
                yield LLMChunk(kind="tool_call", tool_call=call)
            yield LLMChunk(kind="done", finish_reason=choice.finish_reason)
        except ContentFilterFinishReasonError as exc:
            raise LLMCallError("OpenAI response was filtered", retriable=False) from exc
        except APIStatusError as exc:
            raise http_error("OpenAI", exc.response) from exc
        except APIConnectionError as exc:
            raise _connection_error(exc, produced=produced) from exc
        except (httpx.TransportError, httpx.InvalidURL) as exc:
            raise transport_error("OpenAI", exc, produced=produced) from exc
        except APIError as exc:
            raise LLMCallError("OpenAI SDK rejected the response", retriable=False) from exc

    async def list_models(self) -> list[str]:
        try:
            return [model.id async for model in self._sdk.models.list()]
        except APIStatusError as exc:
            raise http_error("OpenAI", exc.response) from exc
        except APIConnectionError as exc:
            raise _connection_error(exc, produced=False) from exc
        except APIError as exc:
            raise LLMCallError("OpenAI SDK rejected the model list", retriable=False) from exc


def _connection_error(exc: APIConnectionError, *, produced: bool) -> LLMCallError:
    cause = exc.__cause__
    if cause is not None:
        return transport_error("OpenAI", cause, produced=produced)
    return LLMCallError("OpenAI connection failed", retriable=True, outage=True)


class _OfficialStreamParser:
    """Official deltas are strictly incremental and contain no dialect markup."""

    def __init__(self, buffers: LLMStreamBuffers) -> None:
        self.buffers = buffers

    def visible_text(self) -> str:
        return self.buffers.content_text

    def provider_state(self, reasoning_blocks, incomplete, tools) -> dict[str, Any]:
        return {}

    def release(self) -> None:
        pass


def _map_tool_call(call: Any) -> ToolCall:
    if call.type != "function":
        raise LLMCallError("Unsupported OpenAI tool call type", retriable=False)
    if not call.id or not call.function.name:
        raise LLMCallError("OpenAI returned an incomplete tool call", retriable=True)
    # Chat Completions defines function.arguments as a JSON string. The runtime
    # requires a dict. Decode once at this boundary, without dialect repair.
    arguments = getattr(call.function, "parsed_arguments", None)
    if arguments is None:
        try:
            arguments = json.loads(call.function.arguments)
        except (ValueError, TypeError):
            arguments = None
    if not isinstance(arguments, dict):
        arguments = {RAW_ARGS_KEY: call.function.arguments}
    return ToolCall(id=call.id, name=call.function.name, arguments=arguments)


def _map_usage(usage: Any) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cache_read_tokens=getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0,
        reasoning_tokens=getattr(usage.completion_tokens_details, "reasoning_tokens", 0) or 0,
    )
