"""MockLLMAdapter：测试用 LLM 模拟器。

按预定义的 response 序列依次返回；用于 Phase 1 集成测试与单测。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ctx_weft.protocols import LLMChunk, LLMClient, LLMRequest, LLMUsage, ToolCall
from ctx_weft.core.utils import estimate_tokens


@dataclass
class MockResponse:
    """一次 LLM 调用的预期返回。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    chunk_size: int = 16  # streaming 时每个 chunk 的字符数
    # usage 拆分模拟（事件层/联调测试用；cache 之和应 ≤ 估算的 prompt_tokens，测试自行保证）
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class MockLLMAdapter(LLMClient):
    """按 responses 队列依次返回；用完后 raise。"""

    def __init__(
        self,
        responses: list[MockResponse],
        context_limit: int = 100_000,
        output_reserve: int = 4096,
    ) -> None:
        self._responses = list(responses)
        self._idx = 0
        self._context_limit = context_limit
        self._output_reserve = output_reserve
        # 记录最近一次调用的 request（供测试断言）
        self.last_request: LLMRequest | None = None

    @property
    def context_limit(self) -> int:
        return self._context_limit

    @property
    def output_reserve(self) -> int:
        return self._output_reserve

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def complete(
        self,
        request: LLMRequest,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        self.last_request = request

        if self._idx >= len(self._responses):
            raise RuntimeError(
                f"MockLLMAdapter exhausted: called {self._idx + 1} times but only "
                f"{len(self._responses)} responses configured"
            )
        response = self._responses[self._idx]
        self._idx += 1

        return self._stream(response, request)

    async def _stream(
        self,
        response: MockResponse,
        request: LLMRequest,
    ) -> AsyncIterator[LLMChunk]:
        # 文本分 chunk 流出
        text = response.text
        size = max(1, response.chunk_size)
        for i in range(0, len(text), size):
            yield LLMChunk(kind="token", text=text[i : i + size])

        # tool_calls 一次性返回
        for tc in response.tool_calls:
            yield LLMChunk(kind="tool_call", tool_call=tc)

        # usage
        prompt_text = request.system + "\n".join(
            (m.content if isinstance(m.content, str) else "")
            for m in request.messages
        )
        prompt_tokens = estimate_tokens(prompt_text)
        completion_tokens = estimate_tokens(text)
        yield LLMChunk(
            kind="usage",
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_write_tokens=response.cache_write_tokens,
                reasoning_tokens=response.reasoning_tokens,
                # input_tokens 自动派生 = prompt − read − write
            ),
        )

        yield LLMChunk(
            kind="done",
            finish_reason="tool_use" if response.tool_calls else "stop",
        )

    async def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)
