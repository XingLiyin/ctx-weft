"""The core owns collection; replaceable adapters connect through protocols only."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctx_weft.core.loop import llm_stream
from ctx_weft.core.loop.llm_gateway import stream_llm
from ctx_weft.protocols.llm import LLMChunk, LLMRequest, current_stream_writer


def test_collection_module_depends_only_on_protocols():
    tree = ast.parse(inspect.getsource(llm_stream))
    dependencies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            dependencies.append(node.module or "")
        elif isinstance(node, ast.Import):
            dependencies.extend(alias.name for alias in node.names)
    assert all(not name.startswith("ctx_weft.") or name.startswith("ctx_weft.protocols.")
               for name in dependencies), dependencies
    root = Path(llm_stream.__file__).resolve().parents[2]
    assert not (root / "providers/llm/stream.py").exists()


async def test_complete_only_client_is_collected_by_core_without_provider_hooks():
    async def complete(request, stream=True):
        yield LLMChunk(kind="reasoning", text="reason")
        yield LLMChunk(kind="token", text="literal <think> text")
        yield LLMChunk(kind="done", finish_reason="stop")

    client = SimpleNamespace(complete=complete)
    stream = stream_llm(client, LLMRequest(model="external", system="", messages=[]))
    async with stream:
        async for _ in stream:
            stream.accept_current()
    result = stream.get_final_result()
    # Core must not impose a built-in provider's tag dialect on arbitrary clients.
    assert result.text == "literal <think> text"
    assert result.reasoning == "reason"
    assert stream.get_final_result() is result
    assert current_stream_writer() is None


async def test_external_parser_shares_core_buffers_and_owns_its_wire_format():
    observed = {}

    class Parser:
        def __init__(self, buffers):
            self.buffers = buffers
            self.released = False

        def visible_text(self):
            return self.buffers.content_text.removeprefix("WIRE:")

        def provider_state(self, reasoning_blocks, incomplete, tools):
            return {"external": {"blocks": reasoning_blocks}}

        def release(self):
            self.released = True

    async def complete(request, stream=True):
        writer = current_stream_writer()
        assert writer is not None
        buffers = writer.begin_attempt()
        parser = Parser(buffers)
        writer.attach_parser(parser)
        observed.update(writer=writer, buffers=buffers, parser=parser)
        buffers.content_text = "WIRE:answer"
        yield LLMChunk(kind="reasoning", text="reason", reasoning_index=7)
        yield LLMChunk(kind="token", text="answer")
        yield LLMChunk(kind="done", finish_reason="stop")

    stream = stream_llm(
        SimpleNamespace(complete=complete), LLMRequest(model="external", system="", messages=[]),
    )
    async with stream:
        async for chunk in stream:
            stream.accept_current()
            assert current_stream_writer() is None
            assert observed["writer"].state.buffers is observed["buffers"]
            if chunk.kind == "token":
                # The core did not append the visible delta to the shared wire text again.
                assert observed["buffers"].content_text == "WIRE:answer"
    result = stream.get_final_result()
    assert result.text == "answer"
    assert result.reasoning == "reason"
    assert result.provider_state == {"external": {"blocks": {7: "reason"}}}
    assert observed["buffers"].content_text == ""
    assert observed["parser"].released


async def test_failed_read_restores_writer_context_and_keeps_partial_result():
    async def source():
        assert current_stream_writer() is stream
        yield LLMChunk(kind="token", text="accepted")
        assert current_stream_writer() is stream
        raise ValueError("broken source")

    stream = llm_stream.CollectedLLMStream(source())
    await anext(stream)
    stream.accept_current()
    with pytest.raises(ValueError, match="broken source"):
        await anext(stream)
    assert current_stream_writer() is None
    await stream.aclose()
    assert stream.partial_result().text == "accepted"
