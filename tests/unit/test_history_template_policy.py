"""Task presentation is chosen by Jinja, consistently for every consumer."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader

from ctx_weft.core.assembler import prompts
from ctx_weft.core.assembler.builder import PromptBuilder, compile_history, history_records
from ctx_weft.core.assembler.sources._history import history_facts
from tests.compaction_testkit import _active_memory_tokens
from ctx_weft.core.runtime import _copy_memory_for_inherit
from ctx_weft.protocols import MemoryAddress, MemoryScope, load_records
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider
from tests.assembler_testkit import compile_prompt, context_from_blocks, prompt_request
from tests.unit.test_capsule_variants import CTX, SCOPE, blocks, body, closure


def test_facts_retain_all_candidates_and_unwrapped_raw_answers():
    records = body() + closure(reply="")
    rows, facts = history_facts(records)
    assert {row["record_id"] for row in rows} == {r.id for r in records}
    task = next(iter(facts["task_history"].values()))
    assert task["source"]["message"].content == "Delivered answer"
    assert task["closing"]["kind"] == "finish"
    assert all("visible" not in row and "show_finish" not in row for row in rows)
    assert "<task_finish" in "\n".join(
        str(m.content) for m in compile_history(records).messages
    )


@pytest.mark.asyncio
async def test_only_editing_the_template_changes_prompt_snapshot_and_token_estimate(monkeypatch):
    records = body() + closure(reply="")
    memory = InMemoryMemoryProvider()
    for record in records:
        await memory.ingest(record, CTX)
    state = SimpleNamespace(scope=SCOPE)
    ctx = SimpleNamespace(
        memory=memory, provider_ctx=CTX, llm=SimpleNamespace(tokenizer=SimpleNamespace(count=len))
    )
    before = await _active_memory_tokens(state, ctx)
    program = (
        Path(prompts.__file__).with_name("prompt_assets") / "steps/_history.jinja"
    ).read_text(encoding="utf-8")
    rule = "{% set choice.show = true %}"
    assert program.count(rule) == 1
    environment = prompts._JINJA.overlay(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {"steps/_history.jinja": program.replace(rule, "{% set choice.show = item.ref != task.closing.ref %}")}
                ),
                prompts._JINJA.loader,
            ]
        )
    )
    monkeypatch.setattr(prompts, "_JINJA", environment)
    full = compile_prompt(blocks(records), prompt_request(task_id="next"))
    assert all("<task_finish" not in str(m.content) for m in full.messages)
    assert all("<task_finish" not in str(r.content) for r in history_records(records))
    assert await _active_memory_tokens(state, ctx) < before
    await _copy_memory_for_inherit(
        source_agent_id="a",
        child_task=SimpleNamespace(id="child"),
        sub_agent=SimpleNamespace(id="b"),
        memory=memory,
        session_id="s",
        tenant_id="default",
    )
    inherited = await load_records(
        memory, MemoryAddress(session_id="s", agent_id="b"), MemoryScope.AGENT, CTX
    )
    assert all("<task_finish" not in str(r.content) for r in inherited)
    # The parent facts and persisted rows were never filtered or rewritten.
    assert len(history_facts(records)[0]) == len(records)
    assert len(await load_records(memory, SCOPE, MemoryScope.TASK, CTX)) == 2


def test_generic_group_alternative_keeps_decorations_and_exact_budget():
    builder = PromptBuilder(len)
    template = prompts._JINJA.from_string("""
    {% do prompt.message(full, priority=6, group="background", ref="original") %}
    {% call prompt.alternative("background") %}
      {% do prompt.message(short, priority=6, group="background", ref="replacement") %}
      {% do prompt.before("replacement", "[", priority=6, group="background") %}
      {% do prompt.after("replacement", "]", priority=6, group="background") %}
    {% endcall %}
    """)
    from ctx_weft.protocols import LLMMessage

    template.render(
        prompt=builder,
        full=LLMMessage(role="user", content="Long background " * 100),
        short=LLMMessage(role="assistant", content="Brief"),
    )
    from ctx_weft.core.utils.estimate import estimate_content_tokens

    budget = estimate_content_tokens("[Brief]", count=len)
    output = builder.build(budget)
    assert [(m.role, m.content) for m in output.messages] == [("assistant", "[Brief]")]
    assert output.token_count == budget
    assert builder.refs == ("original",)


def test_active_instruction_stays_outside_the_finish_record():
    records = body() + closure()
    context = context_from_blocks(prompt_request(task_id="next"), blocks(records))
    context._skill_definition = {"skill_name": "test", "instructions": "Unique active directive"}
    prompt = PromptBuilder(len).compile(context)
    joined = "\n".join(str(m.content) for m in prompt.messages)
    assert "<task_finish" in joined
    finish = next(m for m in prompt.messages if "<task_finish" in str(m.content))
    assert "Unique active directive" not in str(finish.content)
    assert joined.count("Unique active directive") == 1
