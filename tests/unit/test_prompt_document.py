"""Generic editing contracts, independent of the agent loop's message shapes."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from jinja2 import DictLoader, Environment, StrictUndefined, TemplateNotFound, UndefinedError

from ctx_weft.core.assembler import prompts
from ctx_weft.core.assembler.prompt_document import MessageRef, PromptDocument
from ctx_weft.protocols import ImagePart, LLMMessage, TextPart


def template(monkeypatch, *, before="", after="", instruction="", extra=None):
    source = (
        '{% macro system(ctx) %}{{ ctx.system }}{% endmacro %}'
        '{% macro before(message, slots, ctx) %}' + before + '{% endmacro %}'
        '{% macro after(message, slots, ctx) %}' + after + '{% endmacro %}'
        '{% macro instruction(ctx) %}' + instruction + '{% endmacro %}'
    )
    monkeypatch.setattr(prompts, "_JINJA", Environment(
        loader=DictLoader({"custom.jinja": source, **(extra or {})}), undefined=StrictUndefined))
    return prompts.PromptTemplate("custom.jinja")


def test_references_survive_insertions_and_do_not_match_by_content():
    doc = PromptDocument([LLMMessage(role="user", content="same") for _ in range(2)])
    first, second = doc.refs
    inserted = doc.insert_before(first, LLMMessage(role="user", content="new"))
    doc.insert_after(second, LLMMessage(role="assistant", content="last"))
    doc.wrap(second, before="[", after="]")
    assert doc.refs[:3] == (inserted, first, second)
    assert [m.content for m in doc.messages] == ["new", "same", "[same]", "last"]


def test_unknown_or_duplicate_references_fail_without_mutation():
    message = LLMMessage(role="user", content="body")
    with pytest.raises(ValueError, match="unique"):
        PromptDocument([message, message], refs=[MessageRef("x"), MessageRef("x")])
    doc = PromptDocument([message])
    before = doc.preview()
    with pytest.raises(KeyError):
        doc.wrap(MessageRef("missing"), after="!")
    assert doc.preview() == before


def test_arbitrary_slots_and_same_target_have_template_order(monkeypatch):
    entry = template(monkeypatch,
                     before='{% if "b" in slots %}B{% endif %}{% if "a" in slots %}A{% endif %}',
                     after='{{ message.role }}')
    doc = PromptDocument([LLMMessage(role="user", content="body")])
    ref = doc.refs[0]
    doc.decorate(entry, bindings={"a": [ref], "b": [ref]}, context={})
    assert doc.messages[0].content == "BAbodyuser"
    assert doc.preview()["trace"][-1]["slots"] == ["a", "b"]


def test_undefined_template_input_is_atomic_and_reports_entry(monkeypatch):
    entry = template(monkeypatch, after='{% if "fail" in slots %}{{ ctx.missing }}{% else %}!{% endif %}')
    doc = PromptDocument([LLMMessage(role="user", content="one"), LLMMessage(role="user", content="two")])
    initial = doc.preview()
    with pytest.raises(UndefinedError) as error:
        doc.decorate(entry, bindings={"fail": [doc.refs[1]]}, context={})
    assert "custom.jinja" in str(error.value.__notes__)
    assert doc.preview() == initial


def test_macro_and_include_validation(monkeypatch):
    with pytest.raises(TemplateNotFound):
        template(monkeypatch, after='{% include "missing.jinja" %}')
    monkeypatch.setattr(prompts, "_JINJA", Environment(loader=DictLoader({"bad": "text"})))
    with pytest.raises(ValueError, match="system"):
        prompts.PromptTemplate("bad")


def test_multimodal_and_tool_fields_survive_and_inputs_stay_immutable():
    original = [
        LLMMessage(role="user", content=[TextPart(text="look"), ImagePart(data="eA==", media_type="image/png")]),
        LLMMessage(role="assistant", content="", tool_calls=[{"id": "call", "name": "tool", "input": {}}]),
        LLMMessage(role="tool", content="result", tool_call_id="call"),
    ]
    saved = deepcopy(original)
    doc = PromptDocument(original)
    doc.wrap(doc.refs[0], before="prefix", after="suffix")
    assert doc.messages[0].content[1] == original[0].content[1]
    assert doc.messages[1:] == original[1:]
    exposed = doc.messages
    exposed[1].tool_calls[0]["id"] = "modified"
    assert doc.messages[1].tool_calls[0]["id"] == "call"
    assert original == saved


def test_append_only_merges_when_explicit_and_safe():
    doc = PromptDocument([LLMMessage(role="assistant", content="answer")])
    ref = doc.append(LLMMessage(role="user", content="cue"), merge_with_last=True)
    assert len(doc.messages) == 2
    assert doc.append(LLMMessage(role="user", content="tail"), merge_with_last=True) == ref
    assert doc.messages[-1].content == "cue\n\ntail"
    assert doc.append(LLMMessage(role="user", content="")) is None
    doc.append(LLMMessage(role="tool", content="result", tool_call_id="id"), merge_with_last=True)
    assert len(doc.messages) == 3


def test_ordinary_jinja_include_and_request_isolation(monkeypatch):
    entry = template(monkeypatch, instruction='{% include "part.jinja" %}',
                     extra={"part.jinja": "{{ ctx.value }}"})
    assert entry.instruction({"value": "{{ untouched }}"}) == "{{ untouched }}"
    assert entry.instruction({"value": "second"}) == "second"


def test_reasoning_is_neither_dropped_nor_lost_by_merge():
    doc = PromptDocument([LLMMessage(role="user", content="body", reasoning_content="kept")])
    original = doc.refs[0]
    doc.wrap(original, before="prefix", after="suffix")
    assert doc.messages[0].reasoning_content == "kept"
    doc.append(LLMMessage(role="user", content="more", reasoning_content="new"), merge_with_last=True)
    assert len(doc.messages) == 2 and doc.messages[-1].reasoning_content == "new"
    doc.append(LLMMessage(role="assistant", content="", reasoning_content="reasoning only"))
    assert len(doc.messages) == 3


@pytest.mark.asyncio
async def test_preview_uses_the_real_compiler_without_mutating_request():
    from ctx_weft.core.assembler.composer import DefaultComposer

    req = SimpleNamespace(purpose="compact", template=None, extra={}, token_counter=len,
                          task=SimpleNamespace(id="t", title="T", description="", user_prompt="U",
                                               user_prompt_in_memory=False))
    saved = deepcopy(req)
    composer = DefaultComposer()
    preview = await composer.preview([], req)
    result = await composer.compose([], req)
    from dataclasses import asdict

    assert preview["system"] == result.system
    assert [m["message"] for m in preview["messages"]] == [asdict(m) for m in result.messages]
    assert preview["token_count"] == result.token_count
    assert req == saved
