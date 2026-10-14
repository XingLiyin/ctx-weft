"""Purpose remains authoritative; observation judgement is an independent option."""

from types import SimpleNamespace

import pytest
from jinja2 import meta, nodes

from ctx_weft.core.assembler import (
    ActIntent,
    CompactIntent,
    MetadataIntent,
    ReviewIntent,
)
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.prompts import load_prompt_template
from ctx_weft.core.assembler.request_policy import (
    compile_prompt_policy,
    prompt_intent_for_purpose,
    resolve_prompt_policy,
    select_identity_facet,
)
from ctx_weft.protocols import IdentityFacet


@pytest.mark.parametrize(
    ("purpose", "extra", "entry", "facet", "tool_purpose", "images"),
    [
        ("act", {}, "act", "act", "act", "preserve"),
        ("observe", {}, "observe", "observe", "observe", "text_only"),
        (
            "observe", {}, "observe", "observe",
            "observe", "text_only",
        ),
        (
            "observe", {"judge_task": False}, "observe", "compact", "observe", "text_only",
        ),
        ("compact", {}, "compact", "compact", "compact", "text_only"),
        ("recognize_intent", {}, "metadata", "metadata", "recognize_intent", "text_only"),
    ],
)
def test_legacy_purpose_compiles_to_complete_definition(
    purpose, extra, entry, facet, tool_purpose, images,
) -> None:
    definition = resolve_prompt_policy(
        SimpleNamespace(purpose=purpose, extra=extra, intent=None))

    assert definition.template == entry
    assert definition.facet == facet
    assert definition.capability_purpose == tool_purpose
    assert definition.images == images
    assert definition.judge_task == (entry == "observe" and extra.get("judge_task", True))


def test_compact_scope_is_an_intent_dimension_not_a_new_purpose() -> None:
    task = resolve_prompt_policy(
        SimpleNamespace(purpose="compact", extra={"compact_scope": "task"}, intent=None))
    agent = resolve_prompt_policy(
        SimpleNamespace(purpose="compact", extra={"compact_scope": "agent"}, intent=None))

    assert task.template == agent.template == "compact"
    assert task.scope == "task"
    assert agent.scope == "agent"


@pytest.mark.parametrize(
    ("intent", "legacy_purpose"),
    [
        (ActIntent(), "act"),
        (ReviewIntent(judge_task=True), "observe"),
        (ReviewIntent(judge_task=True), "observe"),
        (ReviewIntent(judge_task=False), "observe"),
        (CompactIntent(scope="task"), "compact"),
        (CompactIntent(scope="agent"), "compact"),
        (MetadataIntent(), "recognize_intent"),
    ],
)
def test_explicit_intent_matches_the_legacy_definition(intent, legacy_purpose: str) -> None:
    explicit = compile_prompt_policy(intent)
    legacy = compile_prompt_policy(prompt_intent_for_purpose(
        legacy_purpose,
        {"compact_scope": getattr(intent, "scope", "task"),
         "judge_task": getattr(intent, "judge_task", True)},
    ))

    assert explicit == legacy


def test_purpose_and_intent_must_agree() -> None:
    request = SimpleNamespace(purpose="act", extra={}, intent=ReviewIntent(judge_task=False))
    with pytest.raises(ValueError, match="does not match purpose"):
        resolve_prompt_policy(request)


def test_metadata_recipe_reads_the_existing_recognize_intent_facet() -> None:
    template = SimpleNamespace(
        identity={
            "act": IdentityFacet(text="SOUL"),
            "recognize_intent": IdentityFacet(text="METADATA"),
        },
    )

    definition = compile_prompt_policy(MetadataIntent())
    facet = select_identity_facet(template, definition.facet)
    assert facet is not None
    assert facet.text == "METADATA"


def test_removed_purposes_are_rejected() -> None:
    for purpose in ("background_observe", "background_recap"):
        with pytest.raises(ValueError, match="unsupported prompt purpose"):
            resolve_prompt_policy(SimpleNamespace(purpose=purpose, extra={}, intent=None))
    with pytest.raises(ValueError, match="compact scope"):
        CompactIntent(scope="session")


@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.parametrize("name", ["act", "observe", "compact", "metadata"])
def test_every_entry_has_valid_macros_and_includes(name, locale):
    entry = load_prompt_template(name, locale)
    assert entry.name == f"entries/{name}.{locale}.jinja"


@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.parametrize("name", ["act", "observe", "compact", "metadata"])
def test_entry_owns_its_message_layout(name, locale):
    entry = load_prompt_template(name, locale)
    environment = entry.template.environment
    source = environment.loader.get_source(environment, entry.name)[0]
    parsed = environment.parse(source)
    local_macros = {node.name for node in parsed.body if isinstance(node, nodes.Macro)}
    assert {"system", "before", "after", "fresh", "continuation", "instruction"} <= local_macros
    dependencies = tuple(meta.find_referenced_templates(parsed))
    assert all(name is not None and not name.startswith("entries/") for name in dependencies)


@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.parametrize("name", ["act", "observe", "compact", "metadata"])
@pytest.mark.parametrize("soul,background", [
    ("", ""), ("PERSONA", ""), ("", "BACKGROUND"), ("PERSONA", "BACKGROUND"),
    ("PERSONA\n ", "\nBACKGROUND\n "), ("{{ literal }}", "<tag>{untouched}</tag>"),
])
def test_system_lives_in_entries_and_preserves_exact_layout(name, locale, soul, background):
    entry = load_prompt_template(name, locale)
    expected = soul
    if background:
        expected += ("\n\n---\n\n" if soul else "") + "## Project Background\n\n" + background
    assert entry.system(dict(soul=soul, project_background=background)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "extra", "intent"),
    [
        ("act", {}, ActIntent()),
        ("observe", {"observe_boundary": "normal"}, ReviewIntent(judge_task=True)),
        (
            "observe",
            {"observe_boundary": "plain_text"},
            ReviewIntent(judge_task=True),
        ),
        (
            "observe",
            {"observe_boundary": "interrupt", "judge_task": False},
            ReviewIntent(judge_task=False),
        ),
        ("compact", {"compact_scope": "task"}, CompactIntent(scope="task")),
        ("compact", {"compact_scope": "agent"}, CompactIntent(scope="agent")),
        ("recognize_intent", {}, MetadataIntent()),
    ],
)
async def test_explicit_intent_and_legacy_purpose_render_identically(
    purpose: str, extra: dict, intent,
) -> None:
    task = SimpleNamespace(
        id="tsk_1",
        title="",
        description="",
        user_prompt="",
        user_prompt_in_memory=False,
        outputs=None,
    )
    common = dict(
        purpose=purpose,
        extra=extra,
        task=task,
        template=None,
        token_counter=lambda text: len(text),
    )

    legacy = await DefaultComposer().compose([], SimpleNamespace(**common, intent=None))
    explicit = await DefaultComposer().compose([], SimpleNamespace(**common, intent=intent))

    assert explicit == legacy
