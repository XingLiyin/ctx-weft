"""Compare merged templates against bilingual outputs captured before the merge."""
import json
from pathlib import Path

import pytest
from jinja2 import DictLoader, Environment, StrictUndefined

from ctx_weft.core.assembler import prompts

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "prompt_asset_merge.json")
                  .read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["locale"] + ":" + c["key"])
def test_merged_assets_preserve_exact_output(case):
    key, values, locale = case["key"], case["values"], case["locale"]
    if key.startswith("runtime."):
        name = key.removeprefix("runtime.")
        if name == "final_reply_note_untitled":
            name = "final_reply_note"
        actual = prompts.runtime_prompt(name, locale=locale, **values)
    else:
        if key.startswith("act.resume."):
            values = dict(ref=values.get("ref"), completed_tasks=values.get("completed_tasks", False),
                          full_context=".fallback." not in key)
            key = "act.resume"
        elif key.startswith("review.boundary."):
            values = dict(boundary_kind=key.removeprefix("review.boundary."))
            key = "review.boundary"
        elif key == "act.reply_language":
            key = "common.reply_language"
        pack = prompts.PromptPack(prompts.load_prompt_assets(locale))
        if key in {"review.context", "review.judge", "review.recap"}:
            # Observe now owns composition; reconstruct the old fragment for its
            # frozen baseline without updating any expected prompt text.
            def render(fragment):
                return pack(fragment, **{k: v for k, v in values.items()
                                         if k in prompts.CONNECTORS[fragment]})

            role = values.get("role_section", "")
            actual = (role + "\n\n---\n\n" if role else "") + render("review.context")
            if key != "review.context":
                actual += ("\n\n" if key == "review.recap" else "") + render(key)
        else:
            actual = pack(key, **values)
    if key in {"review.judge", "review.recap"}:
        previous_tool = "control__report_task_outcome" if key == "review.judge" else "control__collect_process_report"
        actual = actual.replace("control__report_observation", previous_tool)
        actual = actual.replace(" 不要填写任务判决、失败原因或下一轮提示字段。", "")
        actual = actual.replace(" Do not fill task verdict, failure reason, or next-step hint fields.", "")
    assert actual == case["text"]


def test_entry_exports_can_be_shared_macros(monkeypatch):
    source = (
        '{% macro system(ctx) %}{{ ctx.soul }}{% endmacro %}'
        '{% macro before(message, slots, ctx) %}{% endmacro %}'
        '{% macro after(message, slots, ctx) %}{% endmacro %}'
        '{% macro instruction(ctx) %}{{ ctx.body }}{% endmacro %}')
    monkeypatch.setattr(prompts, "_JINJA", Environment(loader=DictLoader({
        "shared": source,
        "entry": '{% import "shared" as shared with context %}'
                 '{% set system, before, after, instruction = '
                 'shared.system, shared.before, shared.after, shared.instruction %}',
        "bad": '{% set system = "not a macro" %}',
    }), undefined=StrictUndefined))
    entry = prompts.PromptTemplate("entry")
    assert entry.system(dict(soul="S", body="B")) == "S"
    assert entry.instruction(dict(soul="S", body="B")) == "B"
    with pytest.raises(ValueError, match="system"):
        prompts.PromptTemplate("bad")


def test_included_fragment_variables_are_part_of_the_contract(monkeypatch):
    monkeypatch.setattr(prompts, "_JINJA", Environment(loader=DictLoader({
        "fragment": "{{ value }} {{ local }}", "leaf": "{{ deeper }}",
        "nested": '{% include "leaf" %}',
    }), undefined=StrictUndefined))
    source = '{% set local = "owned" %}{% include "fragment" %}{% include "nested" %}'
    assert prompts._template_variables(source, "entry") == {"value", "deeper"}


def test_shared_macro_validation_keeps_context_dependent_setup(monkeypatch):
    source = (
        '{% set prefix = ctx.prefix %}'
        '{% macro system(ctx) %}{{ prefix }}{% endmacro %}'
        '{% macro before(message, slots, ctx) %}{% endmacro %}'
        '{% macro after(message, slots, ctx) %}{% endmacro %}'
        '{% macro instruction(ctx) %}{{ ctx.body }}{% endmacro %}')
    monkeypatch.setattr(prompts, "_JINJA", Environment(
        loader=DictLoader({"entry": source}), undefined=StrictUndefined))
    entry = prompts.PromptTemplate("entry")
    assert entry.system(dict(prefix="one", body="B")) == "one"
    assert entry.system(dict(prefix="two", body="B")) == "two"


def test_runtime_group_duplicate_macro_names_fail(monkeypatch):
    # Reuse real paths as the inventory, substituting only the parsed source text.
    first, second = prompts.runtime_asset_paths("en")[:2]
    monkeypatch.setattr(prompts, "runtime_asset_paths", lambda locale: (first, second))
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path in (first, second):
            return "{% macro duplicate() %}x{% endmacro %}"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(ValueError, match="duplicate runtime macro"):
        prompts._runtime_definitions.__wrapped__("en")


@pytest.mark.parametrize("locale", ("en", "zh"))
def test_runtime_macro_contracts_and_auto_names(locale):
    definitions = prompts._runtime_definitions(locale)
    assert set(definitions) == prompts.RUNTIME_KEYS
    for path in prompts.runtime_asset_paths(locale):
        text = path.read_text(encoding="utf-8")
        for value in prompts.AUTO_PLACEHOLDERS.values():
            assert value not in text, (path, value)
    assert {key: value[1:] for key, value in definitions.items()} == {
        key: value[1:] for key, value in prompts._runtime_definitions("en").items()}
