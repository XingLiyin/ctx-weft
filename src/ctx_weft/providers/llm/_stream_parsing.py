"""Provider parsing rules over request buffers supplied through the LLM protocol."""

from __future__ import annotations

from typing import Any

from ctx_weft.protocols.llm import LLMStreamBuffers, ToolCall, current_stream_writer
from ctx_weft.providers.llm.text_calls import ContentGate, clean_visible, merge_content


class ReasoningCandidate:
    """Keep the existing fallback rule for reasoning outside delta."""

    def __init__(self) -> None:
        self.held = ""
        self.delta_seen = False

    def observe(self, text: str) -> None:
        if text and not self.delta_seen:
            self.held = text if self.held and text.startswith(self.held) else self.held + text

    def take(self) -> str:
        text, self.held = self.held, ""
        return text


class AdapterStreamParser:
    """Parse wire content without allocating a second copy of the request buffers."""

    def __init__(self, buffers: LLMStreamBuffers) -> None:
        self.buffers = buffers
        self.gate = ContentGate()
        self._thinking: dict[int, dict[str, Any]] = {}

    @property
    def content_text(self) -> str:
        return self.buffers.content_text

    @property
    def tool_buffers(self) -> dict[int, dict[str, Any]]:
        return self.buffers.tool_buffers

    def feed_text(self, text: str) -> str:
        self.buffers.content_text = merge_content(self.buffers.content_text, text)
        return self.gate.feed(self.buffers.content_text)

    def feed_tool(
        self, index: int, *, call_id: str = "", name: str = "", arguments: str = "",
        append_name: bool = False, reset: bool = False,
    ) -> None:
        if reset:
            self.tool_buffers[index] = {"id": "", "name": "", "arguments": ""}
        buf = self.tool_buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id:
            buf["id"] = call_id
        if name:
            buf["name"] = buf["name"] + name if append_name else name
        if arguments:
            buf["arguments"] = merge_content(buf["arguments"], arguments)

    def start_thinking(self, index: int) -> None:
        self._thinking[index] = {"signature": "", "closed": False}

    def signature(self, index: int, signature: str) -> None:
        if index in self._thinking:
            self._thinking[index]["signature"] = signature

    def stop_block(self, index: int) -> None:
        if index in self._thinking:
            self._thinking[index]["closed"] = True

    def visible_text(self) -> str:
        return clean_visible(self.content_text)

    def provider_state(
        self, reasoning_blocks: dict[int, str], incomplete: set[int],
        tools: tuple[ToolCall, ...],
    ) -> dict[str, Any]:
        blocks = [
            {"type": "thinking", "thinking": reasoning_blocks.get(index, ""),
             "signature": info["signature"]}
            for index, info in self._thinking.items()
            if info["closed"] and info["signature"] and index not in incomplete
        ]
        return {
            "anthropic": {"thinking_blocks": blocks, "tool_call_ids": tuple(t.id for t in tools)}
        } if blocks else {}

    def release(self) -> None:
        self._thinking.clear()


def begin_parsed_attempt() -> AdapterStreamParser:
    """Borrow core's buffers when collected; standalone complete() still yields chunks."""
    writer = current_stream_writer()
    buffers = writer.begin_attempt() if writer is not None else LLMStreamBuffers()
    parser = AdapterStreamParser(buffers)
    if writer is not None:
        writer.attach_parser(parser)
    return parser
