"""Call settings retain their type across persistence and never share owner state."""

from copy import deepcopy
from dataclasses import dataclass, replace

import pytest

from ctx_weft.core.control.reducers import deserialize_view, reduce_events, serialize_view
from ctx_weft.core.loop.llm_gateway import apply_dynamic_max_tokens, build_llm_request
from ctx_weft.core.orchestrator.model import ModelChoice
from ctx_weft.protocols import (
    LLMRequest,
    ModelCallConfig,
    ModelCallConfigError,
    deserialize_call_config,
    register_model_call_config,
    serialize_call_config,
)
from ctx_weft.protocols.events import EventType
from tests.unit.test_gateway_prompt_compile import setup as prompt_setup
from tests.unit.test_set_llm_commands import TPL, _reg


@dataclass
class CustomCallConfig(ModelCallConfig):
    thinking_budget: int | None = None
    labels: list[str] | None = None


register_model_call_config("test.custom-call-config", CustomCallConfig)


@pytest.mark.parametrize("kwargs", [
    {"reasoning_effort": ""}, {"temperature": -0.1}, {"temperature": float("nan")},
    {"top_p": 2}, {"max_tokens": 0}, {"max_tokens": True}, {"stop": []}, {"stop": [1]},
])
def test_invalid_common_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ModelCallConfig(**kwargs)


def test_config_subclass_json_roundtrip_and_detachment():
    config = CustomCallConfig(reasoning_effort="ultra", thinking_budget=4000,
                              labels=["saved"], stop=("END",))
    payload = serialize_call_config(config)
    restored = deserialize_call_config(payload)
    assert type(restored) is CustomCallConfig
    assert restored == config
    payload["values"]["labels"].append("changed")
    assert config.labels == restored.labels == ["saved"]
    assert deserialize_call_config(None) == ModelCallConfig()


@pytest.mark.parametrize("payload", [
    {"type": "missing", "version": 1, "values": {}},
    {"type": "base", "version": 999, "values": {}},
    {"type": "base", "version": 1, "values": {"unknown": 1}},
    {"type": "base", "version": True, "values": {}},
])
def test_unknown_or_invalid_codec_fails_without_dropping_settings(payload):
    with pytest.raises(ModelCallConfigError):
        deserialize_call_config(payload)


def test_unregistered_subclass_cannot_be_silently_persisted_as_base():
    @dataclass
    class Unregistered(ModelCallConfig):
        extra: int = 1

    with pytest.raises(ModelCallConfigError):
        serialize_call_config(Unregistered())


def test_request_defaults_are_independent_and_old_fields_are_removed():
    a, b = LLMRequest("m", "", []), LLMRequest("m", "", [])
    a.config.temperature = 0.5
    assert b.config.temperature is None
    assert not hasattr(a, "temperature") and not hasattr(a, "max_tokens")


@pytest.mark.asyncio
async def test_config_only_change_emits_full_choice_and_repeated_selection_is_noop():
    reg = _reg()
    agent, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    reg.event_bus.events.clear()
    config = CustomCallConfig(reasoning_effort="high", labels=["owner"])
    choice = ModelChoice(config=config)
    config.labels.append("outside")
    assert await reg.set_agent_llm(agent.id, choice, reason="test")
    choice.config.labels.append("caller mutation")
    event = reg.event_bus.events[-1]
    assert event.type == EventType.AGENT_LLM_CHANGED
    assert deserialize_call_config(event.payload["llm_config"]).labels == ["owner"]
    assert reg.resolve_model(agent.id).config.labels == ["owner"]
    assert not await reg.set_agent_llm(
        agent.id, ModelChoice(config=CustomCallConfig(reasoning_effort="high", labels=["owner"])),
        reason="repeat",
    )
    assert len(reg.event_bus.events) == 1


@pytest.mark.asyncio
async def test_parent_child_session_and_running_snapshot_isolation():
    reg = _reg()
    parent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        llm=ModelChoice(config=CustomCallConfig(reasoning_effort="high", labels=["x"])),
    )
    child, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default", parent_agent_id=parent.id,
    )
    other, _ = await reg.instantiate(template_id=TPL, session_id="s2", tenant_id="default")
    running = reg.resolve_model(parent.id)
    assert reg.resolve_model(child.id).config == running.config
    assert reg._agents[parent.id].llm.config is not reg._agents[child.id].llm.config
    assert await reg.set_session_llm(
        "s1", ModelChoice(config=ModelCallConfig(reasoning_effort="low")), reason="test",
    ) == 2
    changes = [e for e in reg.event_bus.events if e.type == EventType.AGENT_LLM_CHANGED]
    assert len({e.causation_id for e in changes}) == 1
    assert running.config.reasoning_effort == "high"
    assert reg.resolve_model(parent.id).config.reasoning_effort == "low"
    assert reg.resolve_model(child.id).config.reasoning_effort == "low"
    assert reg.resolve_model(other.id).config == ModelCallConfig()
    independent, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    assert reg.resolve_model(independent.id).config == ModelCallConfig()
    assert all(e.type in (EventType.AGENT_INSTANTIATED, EventType.AGENT_SPAWNED,
                          EventType.AGENT_LLM_CHANGED) for e in reg.event_bus.events)


@pytest.mark.asyncio
async def test_event_replay_snapshot_and_registry_reload_preserve_subclass():
    reg = _reg()
    agent, _ = await reg.instantiate(
        template_id=TPL, session_id="s1", tenant_id="default",
        llm=ModelChoice(config=CustomCallConfig(reasoning_effort="high", thinking_budget=2048)),
    )
    await reg.set_agent_llm(
        agent.id, ModelChoice(config=CustomCallConfig(reasoning_effort="low", thinking_budget=1024)),
        reason="changed",
    )
    view = reduce_events(reg.event_bus.events, "run")
    snapshot = serialize_view(view)
    restored = deserialize_view(deepcopy(snapshot))
    fresh = _reg()
    await fresh.load(restored.agents, session_id="s1", tenant_id="default")
    config = fresh.resolve_model(agent.id).config
    assert type(config) is CustomCallConfig
    assert config.reasoning_effort == "low" and config.thinking_budget == 1024
    snapshot["agents"][agent.id]["llm_config"]["values"]["reasoning_effort"] = "mutated"
    assert view.agents[agent.id].llm_config["values"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_legacy_events_and_snapshots_restore_empty_config():
    reg = _reg()
    agent, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    legacy = [replace(e, payload={k: v for k, v in e.payload.items() if k != "llm_config"})
              for e in reg.event_bus.events]
    view = reduce_events(legacy, "run")
    blob = serialize_view(view)
    blob["agents"][agent.id].pop("llm_config")
    fresh = _reg()
    await fresh.load(deserialize_view(blob).agents, session_id="s1", tenant_id="default")
    assert fresh.resolve_model(agent.id).config == ModelCallConfig()


@pytest.mark.asyncio
async def test_unknown_config_is_preserved_in_projection_and_blocks_execution_restore():
    reg = _reg()
    agent, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")
    event = reg.event_bus.events[-1]
    unknown = {"type": "uninstalled-adapter", "version": 2, "values": {"budget": 123}}
    view = reduce_events([replace(event, payload={**event.payload, "llm_config": unknown})], "r")
    restored = deserialize_view(serialize_view(view))
    assert restored.agents[agent.id].llm_config == unknown
    with pytest.raises(ModelCallConfigError):
        await _reg().load(restored.agents, session_id="s1", tenant_id="default")


@pytest.mark.asyncio
async def test_failed_event_commit_does_not_publish_new_choice():
    reg = _reg()
    agent, _ = await reg.instantiate(template_id=TPL, session_id="s1", tenant_id="default")

    async def fail(event):
        raise OSError("storage unavailable")

    reg.event_bus.emit = fail
    with pytest.raises(OSError):
        await reg.set_agent_llm(
            agent.id, ModelChoice(config=ModelCallConfig(reasoning_effort="high")), reason="test",
        )
    assert reg.resolve_model(agent.id).config == ModelCallConfig()


@pytest.mark.asyncio
async def test_each_request_gets_subclass_copy_and_budget_never_changes_run_config():
    inputs, state, ctx, _ = await prompt_setup()
    state.resolved_model = replace(
        state.resolved_model,
        config=CustomCallConfig(reasoning_effort="high", max_tokens=90000, labels=["run"]),
    )
    a = await build_llm_request(ctx, state, inputs)
    b = await build_llm_request(ctx, state, inputs)
    assert type(a.config) is CustomCallConfig
    a.prompt_token_estimate = 99000
    apply_dynamic_max_tokens(ctx, a, state.resolved_model)
    assert a.config.max_tokens == 1000
    a.config.labels.append("adapter")
    assert b.config.max_tokens == state.resolved_model.config.max_tokens == 90000
    assert b.config.labels == state.resolved_model.config.labels == ["run"]


@pytest.mark.asyncio
async def test_retry_discards_adapter_config_mutations():
    from ctx_weft.core.loop.llm_gateway import stream_llm_resilient
    from ctx_weft.protocols import LLMCallError, LLMChunk
    from tests.unit.test_midstream_recovery import context, state

    seen = []

    class MutatingAdapter:
        def complete(self, request, stream=True):
            async def run():
                seen.append(deepcopy(request.config))
                request.config.reasoning_effort = "adapter mutation"
                request.config.labels.append("mutated")
                if len(seen) == 1:
                    raise LLMCallError("connection lost", retriable=True, outage=True)
                yield LLMChunk(kind="done", finish_reason="stop")
            return run()

    config = CustomCallConfig(reasoning_effort="high", labels=["original"])
    req = LLMRequest("m", "", [], config=config)
    ctx = context(MutatingAdapter())
    stream = stream_llm_resilient(ctx, state(), req)
    async with stream:
        async for _ in stream:
            stream.accept_current()
    assert len(seen) == 2 and seen == [config, config]
    assert req.config.labels == ["original"]
    prompt = next(e for e in ctx.event_bus.events if e.type == EventType.LLM_PROMPT_SENT)
    assert deserialize_call_config(prompt.payload["llm_config"]) == config
