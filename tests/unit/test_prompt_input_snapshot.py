"""Historical prompts use saved inputs, including folded memory and live tools."""
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.assembler import ContextAssembler, ContextBlock, ContextRequest
from ctx_weft.core.assembler.assembler import AssemblerDeps
from ctx_weft.core.loop.llm_gateway import prompt_event_inputs
from ctx_weft.protocols import (
    ImagePart,
    LLMMessage,
    LLMTool,
    MemoryAddress,
    MemoryEvent,
    MemoryKind,
    MemoryScope,
    ProviderContext,
    TextPart,
)
from ctx_weft.protocols.capability import SkillCapability, SkillDefinition, ToolCapability
from ctx_weft.protocols.knowledge import KnowledgeDoc
from ctx_weft.protocols.template import IdentityFacet
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from ctx_weft.providers.memory.sql import open_sqlite_memory
from tests.assembler_testkit import ListedCapabilities

ADDRESS = MemoryAddress(session_id="s", task_id="t", agent_id="a")
CTX = ProviderContext(session_id="s")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
BODY = "historical memory body " * 500


@pytest.fixture(params=["memory", "sqlite"])
async def memory(request, tmp_path):
    if request.param == "memory":
        yield InMemoryMemoryProvider()
    else:
        async with open_sqlite_memory(tmp_path / "prompts.db") as provider:
            yield provider


def prompt_providers():
    tool = ToolCapability(id="test:read", name="read", description="old tool description",
                          purposes=["act", "observe", "recognize_intent"],
                          input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
    skill = SkillCapability(id="test:skill", name="skill", description="old skill")
    return {"test": ListedCapabilities([tool, skill], description="old provider", definitions={
        "skill": SkillDefinition("test:skill", "skill", "old skill instructions")})}


async def compile_event(payload, *, memory, ctx, template):
    assembler = ContextAssembler([], AssemblerDeps(memory, [], ctx, capability_provider_index=prompt_providers()))
    return await assembler.compile(payload, template=template)


async def assembled(memory, purpose="act", *, staged=False, counter=None):
    event = MemoryEvent(id="input", address=ADDRESS, content=BODY, timestamp=NOW,
                        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
                        role="user", metadata={"task_id": "t"})
    await memory.ingest(event, CTX)
    record = (await memory.read_records(["input"], CTX))[0]
    tool = ToolCapability(id="test:read", name="read", description="old tool description",
                          purposes=["act", "observe", "recognize_intent"],
                          input_schema={"type": "object", "properties": {"path": {"type": "string"}}})
    skill = SkillCapability(id="test:skill", name="skill", description="old skill")
    template = NS(id="agent:example", identity={
        name: IdentityFacet(f"role for {name}")
        for name in ("act", "observe", "compact", "recognize_intent")
    })
    task = NS(id="t", title="old task", description="old description", user_prompt=BODY,
              user_prompt_in_memory=True, outputs="old output", switch_reason="old switch",
              settings=NS(skill_name="test__skill"))
    guidance = {"guidance": {"task_ref": "old task", "plan_tasks": [],
                             "has_other_tasks": False, "completed_tasks": [],
                             "review_note": "old next step", "attended": True,
                             "unattended": False, "unattended_others": False},
                "resume": {"ref": "old task", "completed_tasks": False}}
    blocks = [
        ContextBlock("h", "agent_recall", "history", record, {"sequence": 1}),
        ContextBlock("cap", "capability", "capabilities", tool,
                     {"provider_name": "test", "provider_description": "old provider"}),
        ContextBlock("skill", "capability", "capabilities", skill),
        ContextBlock("definition", "capability:skill-definition", "directive",
                     SkillDefinition("test:skill", "skill", "old skill instructions")),
        ContextBlock("guidance", "guidance", "guidance", guidance),
        ContextBlock("knowledge", "knowledge:docs", "reference",
                     KnowledgeDoc("doc", "old knowledge", .9, "docs"), {"score": .9}),
        ContextBlock("topic", "blackboard:topic", "blackboard", record,
                     {"intent": "predecessor", "topic": "topic"}),
        ContextBlock("semantic", "long_memory", "summary", record, {"score": .8}),
    ]

    class Source:
        async def fetch(self, request, deps):
            for block in blocks:
                yield block

    request = ContextRequest(
        purpose=purpose, scope=ADDRESS, task=task, agent=NS(id="a"),
        session=NS(context_limit=100_000, reserved_output_tokens=1000),
        template=template, bound_capabilities=[],
        extra={"observe_boundary": "actor_done", "subtasks": [
            {"task_id": "child", "title": "old child", "outcome": "finished"}],
            "staged_memory": [event] if staged else []},
    )
    if counter is not None:
        request.token_counter = counter
    assembler = ContextAssembler([Source()], AssemblerDeps(memory, [], CTX, capability_provider_index=prompt_providers()))
    inputs = await assembler.assemble(request)
    prompt = await assembler.compile(inputs, template=request.template, token_counter=request.token_counter)
    return prompt, request, blocks


@pytest.mark.parametrize("purpose", ["act", "observe", "compact", "recognize_intent"])
async def test_roundtrip_after_fold_and_source_state_changes(memory, purpose):
    prompt, request, blocks = await assembled(memory, purpose)
    snapshot = prompt_event_inputs(prompt, prompt.messages)
    serialized = json.dumps(snapshot)
    assert BODY not in serialized
    assert "system" not in snapshot and "messages" not in snapshot and "extra" not in snapshot
    assert snapshot["task"]["user_prompt_memory_id"] == "input"
    request.task.title = "new title"
    request.task.outputs = "new output"
    request.extra["subtasks"][0]["title"] = "new child"
    blocks[1].value.description = "new tool description"
    blocks[1].metadata["provider_description"] = "new provider"
    blocks[3].value.instructions = "new instructions"
    blocks[4].value["guidance"]["review_note"] = "new next step"
    blocks[5].value.content = "new knowledge"
    await memory.fold(["input"], [], CTX)
    restored = await compile_event(json.loads(serialized), memory=memory, ctx=CTX,
                                    template=request.template)
    assert restored.system == prompt.system
    assert restored.messages == prompt.messages
    assert restored.tools == prompt.tools


async def test_unpersisted_inputs_and_react_messages_are_self_contained(memory):
    prompt, request, _ = await assembled(memory, "observe", staged=True)
    await memory.fold(["input"], [], CTX)
    appended = [LLMMessage("assistant", "observer's first attempt"),
                LLMMessage("user", "submit using the terminal tool")]
    snapshot = prompt_event_inputs(prompt, prompt.messages + appended)

    class NoReads:
        async def read_records(self, ids, ctx):
            raise AssertionError("staged inputs should not need a persisted row")

    restored = await compile_event(json.loads(json.dumps(snapshot)), memory=NoReads(),
                                    ctx=CTX, template=request.template)
    assert restored.messages == prompt.messages + appended


async def test_appended_persisted_messages_use_ids_and_keep_actual_calls(memory):
    prompt, request, _ = await assembled(memory)
    content = [TextPart(text="tool image"), ImagePart(
        data="blob:" + "a" * 64, media_type="image/png", source_type="ref")]
    await memory.ingest(MemoryEvent(
        id="result", address=ADDRESS, content=content, timestamp=NOW, role="tool",
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK,
        metadata={"tool_call_id": "call"}), CTX)
    appended = [LLMMessage("tool", content, tool_call_id="call", memory_id="result")]
    snapshot = prompt_event_inputs(prompt, prompt.messages + appended)
    assert "content" not in snapshot["appended_messages"][0]
    restored = await compile_event(snapshot, memory=memory, ctx=CTX, template=request.template)
    assert restored.messages[-1].content == content
    assert restored.messages[-1].tool_call_id == "call"


async def test_missing_reference_and_wrong_session_fail_explicitly(memory):
    prompt, request, _ = await assembled(memory)
    snapshot = prompt_event_inputs(prompt, prompt.messages)
    missing = deepcopy(snapshot)
    missing["memory_blocks"][0]["memory_id"] = "missing"
    with pytest.raises(ValueError, match="memory records missing"):
        await compile_event(missing, memory=memory, ctx=CTX, template=request.template)
    with pytest.raises(ValueError, match="different session"):
        await compile_event(snapshot, memory=memory, ctx=ProviderContext(session_id="other"),
                             template=request.template)


async def test_gateway_emits_inputs_and_freezes_actual_live_tools(memory):
    from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
    from ctx_weft.protocols import LLMRequest
    from ctx_weft.protocols.events import EventType
    from tests.unit.test_llm_event_convergence import _make_ctx, _make_state, _RecordingBus

    prompt, _, _ = await assembled(memory)
    pinned = LLMTool("pinned", "discovered after assembly", {"type": "object"})
    bus = _RecordingBus()
    ctx, state = _make_ctx(bus), _make_state()
    refs = [{"provider": "test", "id": "test:pinned"}]
    request = LLMRequest("model", prompt.system, prompt.messages, tools=[pinned],
                         prompt_inputs=prompt_event_inputs(prompt, prompt.messages, refs))
    stream = stream_llm_resilient(ctx, state, request)
    async for _ in stream:
        stream.accept_current()
    event = next(event for event in bus.events if event.type == EventType.LLM_PROMPT_SENT)
    assert ctx.llm.last_request.prompt_inputs is None
    assert "system" not in event.payload and "messages" not in event.payload
    assert BODY not in json.dumps(event.payload)
    assert event.payload["tools"] == refs
    pinned.description = "mutated later"
    assert event.payload["tools"] == refs
    assert "discovered after assembly" not in json.dumps(event.payload)


async def test_recompile_uses_current_template_without_collecting_again(memory):
    prompt, request, _ = await assembled(memory)
    snapshot = prompt_event_inputs(prompt, prompt.messages)
    assert "role for act" not in json.dumps(snapshot)
    request.template.identity["act"].text = "current identity template"
    restored = await compile_event(snapshot, memory=memory, ctx=CTX, template=request.template)
    assert "current identity template" in restored.system


async def test_saved_token_counts_and_pins_do_not_change_prefix_budget(memory):
    prompt, request, _ = await assembled(memory, counter=lambda text: len(text) * 3)
    snapshot = prompt_event_inputs(prompt, prompt.messages)
    snapshot["tools"] = [{"provider": "test", "id": "test:read"}]
    restored = await compile_event(json.loads(json.dumps(snapshot)), memory=memory,
                                    ctx=CTX, template=request.template)
    assert restored.system == prompt.system
    assert restored.messages == prompt.messages
    assert restored.token_count == prompt.token_count
    assert restored.tools[0].name == "test__read"


async def test_unpersisted_images_are_externalized_without_mutating_inputs(memory):
    from ctx_weft.core.loop.llm_gateway import externalize_prompt_images
    from tests.unit.test_event_blob_store import _Stub

    prompt, _, _ = await assembled(memory, staged=True)
    snapshot = prompt_event_inputs(prompt, prompt.messages)
    inline = {"type": "image", "source_type": "base64", "media_type": "image/png", "data": "ZGF0YQ=="}
    snapshot["memory_blocks"][0]["record"]["content"] = [inline]
    store = _Stub()
    stored = await externalize_prompt_images(snapshot, event_blob_store=store, ctx=CTX)
    assert "ZGF0YQ==" not in json.dumps(stored)
    part = stored["memory_blocks"][0]["record"]["content"][0]
    assert part["source_type"] == "ref"
    assert (await store.get(part["data"], CTX))[0] == b"data"
    assert snapshot["memory_blocks"][0]["record"]["content"][0] == inline


async def test_assembled_context_is_the_event_data_and_compiles_without_live_sources(memory):
    prompt, request, _ = await assembled(memory)
    assert not hasattr(prompt.inputs, "request")
    assert BODY not in json.dumps(asdict(prompt.inputs))
    payload = prompt.inputs.to_payload()
    assert "prompt_snapshot" not in payload

    class NoSource:
        async def fetch(self, *args):
            raise AssertionError("compile must never collect sources")
            yield

    class NoLiveCache:
        def available(self, *args):
            raise AssertionError("compile must never query current capabilities")

    assembler = ContextAssembler([NoSource()], AssemblerDeps(memory, [], CTX,
                                                            capability_cache=NoLiveCache(),
                                                            capability_provider_index=prompt_providers()))
    compiled = await assembler.compile(payload, template=request.template)
    assert compiled.system == prompt.system and compiled.messages == prompt.messages


async def test_task_multimodal_content_already_in_memory_keeps_only_its_id(memory):
    from ctx_weft.core.assembler.sources.agent_recall import AgentRecallSource

    content = [TextPart(text="look at this image"), ImagePart(
        data="blob:" + "b" * 64, media_type="image/png", source_type="ref")]
    await memory.ingest(MemoryEvent(
        id="image-input", address=ADDRESS, content=content, timestamp=NOW, role="user",
        kind=MemoryKind.CONVERSATION_TURN, scope=MemoryScope.TASK), CTX)
    request = ContextRequest(
        purpose="act", scope=ADDRESS,
        task=NS(id="t", title="image task", user_prompt=content, user_prompt_in_memory=True),
        agent=NS(id="a"), session=NS(context_limit=100_000, reserved_output_tokens=0),
        template=None, bound_capabilities=[])
    assembler = ContextAssembler([AgentRecallSource()], AssemblerDeps(memory, [], CTX, capability_provider_index=prompt_providers()))
    inputs = await assembler.assemble(request)
    prompt = await assembler.compile(inputs)
    assert inputs.task["user_prompt_memory_id"] == "image-input"
    assert "user_prompt" not in inputs.task
    assert "blob:" not in json.dumps(inputs.to_payload())
    assert any(any(isinstance(part, ImagePart) for part in message.content)
               for message in prompt.messages if isinstance(message.content, list))


async def test_record_cache_cannot_bypass_tenant_scoped_reads(memory):
    prompt, request, _ = await assembled(memory)
    ctx = ProviderContext(session_id=CTX.session_id, tenant_id="another-tenant")
    assembler = ContextAssembler([], AssemblerDeps(memory, [], ctx, capability_provider_index=prompt_providers()))
    with pytest.raises(ValueError, match="memory records missing"):
        await assembler.compile(prompt.inputs, template=request.template)
