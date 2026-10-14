"""Frozen pre-cleanup runtime outputs; do not regenerate to hide a regression."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, StrictUndefined, UndefinedError

from ctx_weft.core.assembler import prompts
from ctx_weft.core.loop.capability_gateway import converge_tool_output
from ctx_weft.core.loop.task_memory_fold import _final_reply_block, _recap_block
from ctx_weft.core.loop.steps.finalize import _dispatch_ack, _dispatch_running_ack

BASELINE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "prompt_cleanup.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", BASELINE["runtime"])
def test_runtime_text_unchanged(name):
    case = BASELINE["runtime"][name]
    name = "final_reply_note" if name == "final_reply_note_untitled" else name
    assert prompts.runtime_prompt(name, **case["values"]) == case["text"]


@pytest.mark.parametrize("ref", BASELINE["dispatch"])
def test_dispatch_text_unchanged(ref):
    case = BASELINE["dispatch"][ref]
    assert _dispatch_running_ack(ref) == case["running"]
    for outcome in ("success", "fail", "continue"):
        assert _dispatch_ack(ref, outcome) == case[outcome]


@pytest.mark.parametrize("ref", BASELINE["finish"])
def test_finish_text_unchanged(ref):
    assert _final_reply_block(ref, "  final result  ") == BASELINE["finish"][ref]["reply"]
    assert _recap_block(ref) == BASELINE["finish"][ref]["recap"]


@pytest.mark.parametrize("tail", (0, 4))
async def test_spill_text_unchanged(tail):
    result = await converge_tool_output(
        "abcdefghijklmno", "inv1", None, None, threshold=6, preview_chars=3, tail_chars=tail)
    assert result == BASELINE["spill"][str(tail)]


def test_runtime_rejects_missing_unknown_and_extra_arguments():
    with pytest.raises(UndefinedError):
        prompts.runtime_prompt("unknown_tool")
    with pytest.raises(KeyError):
        prompts.runtime_prompt("not_a_prompt")
    with pytest.raises(KeyError):
        prompts.runtime_prompt("no_reply", typo="x")


def test_runtime_is_jinja_not_string_replacement(monkeypatch):
    # Editors may add comments, branching and includes to runtime assets too.
    filename = prompts._runtime_definitions("en")["human_note"][0]
    source = (
        "{% from 'runtime/conversation.en.jinja' import no_reply %}"
        "{% macro human_note(note, result) %}"
        "{# invisible #}{% if note %}{{ note }}: {% endif %}{{ result }}"
        "{{ no_reply() }}{% endmacro %}")
    monkeypatch.setattr(prompts, "_JINJA", Environment(
        loader=ChoiceLoader([DictLoader({filename: source}), prompts._JINJA.loader]),
        undefined=StrictUndefined, autoescape=False))
    literal = "{{ result }} {result} <tag>"
    assert prompts.runtime_prompt("human_note", note=literal, result="OK") == (
        literal + ": OK" + prompts.runtime_prompt("no_reply"))
    assert prompts.runtime_prompt("human_note", note="", result="OK") == (
        "OK" + prompts.runtime_prompt("no_reply"))
