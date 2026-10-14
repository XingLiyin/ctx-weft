"""Judge combines evaluation and summarization facets without changing other purposes."""
from types import SimpleNamespace as NS

import pytest

from ctx_weft.core.assembler import prompts
from ctx_weft.core.assembler.budget import PriorityBudgetStrategy
from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.request_policy import prompt_intent_for_purpose
from ctx_weft.core.assembler.sources.identity import IdentitySource
from ctx_weft.core.models.errors import ContextOverflowError
from ctx_weft.protocols import IdentityFacet


def request(purpose, compact="COMPACT {{ untouched }}", *, observe="ROLE", explicit=True):
    identity = {"act": IdentityFacet(text="SOUL")}
    if observe is not None:
        identity["observe"] = IdentityFacet(text=observe)
    if compact is not None:
        identity["compact"] = IdentityFacet(text=compact)
    return NS(
        purpose="observe" if purpose.startswith("background_") else purpose,
        intent=prompt_intent_for_purpose("observe" if purpose.startswith("background_") else purpose,
                                       {"judge_task": purpose != "background_recap"}) if explicit else None,
        template=NS(id="tpl", version="1", identity=identity),
        task=NS(id="task", title="Task", description="D", user_prompt="Ask",
                user_prompt_in_memory=False, outputs="Delivered"),
        extra={"observe_boundary": "finish", "judge_task": purpose != "background_recap"}, token_counter=len,
    )


async def collect(req):
    return [b async for b in IdentitySource().fetch(req, None)]


@pytest.mark.parametrize("purpose", ["observe", "background_observe"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_judge_renders_both_facets_once_in_order(purpose, explicit, locale, monkeypatch):
    load = prompts.load_prompt_template
    monkeypatch.setattr(prompts, "load_prompt_template", lambda name: load(name, locale))
    req = request(purpose, explicit=explicit)
    blocks = await collect(req)
    assert [b.metadata["facet_purpose"] for b in blocks] == ["observe", "compact"]
    assert all(b.priority == 0 and b.token_estimate == len(b.content) for b in blocks)
    # Source order must not determine which facet is the evaluation role.
    prompt = await DefaultComposer().compose(list(reversed(blocks)), req)
    text = prompt.messages[-1].content
    heading = "## Summary Principles" if locale == "en" else "## 总结原则"
    assert prompt.system == "SOUL"
    assert text.count("ROLE") == text.count("COMPACT {{ untouched }}") == 1
    assert text.index("ROLE") < text.index(heading) < text.index("COMPACT {{ untouched }}")
    assert text.index("COMPACT {{ untouched }}") < text.index("`act_recap`")
    assert "control__report_observation" in text
    assert prompts.render_template_file(f"compact/role_fallback.{locale}.jinja") not in text


@pytest.mark.parametrize("purpose", ["observe", "background_observe", "background_recap"])
@pytest.mark.parametrize("compact", [None, ""])
@pytest.mark.parametrize("locale", ["en", "zh"])
async def test_missing_or_empty_compact_uses_localized_fallback(purpose, compact, locale, monkeypatch):
    load = prompts.load_prompt_template
    monkeypatch.setattr(prompts, "load_prompt_template", lambda name: load(name, locale))
    req = request(purpose, compact)
    prompt = await DefaultComposer().compose(await collect(req), req)
    fallback = prompts.render_template_file(f"compact/role_fallback.{locale}.jinja")
    assert prompt.messages[-1].content.count(fallback) == 1
    assert prompt.messages[-1].content.count("ROLE") == (0 if purpose == "background_recap" else 1)


async def test_compact_never_impersonates_missing_observe():
    req = request("observe", observe=None)
    blocks = await collect(req)
    assert [b.metadata["facet_purpose"] for b in blocks] == ["compact"]
    prompt = await DefaultComposer().compose(blocks, req)
    assert "## Evaluation Principles" not in prompt.messages[-1].content
    assert "COMPACT {{ untouched }}" in prompt.messages[-1].content


async def test_observe_uses_only_the_declared_host_facets():
    req = request("background_observe", explicit=False)
    req.template.identity["background_observe"] = IdentityFacet(text="CUSTOM-JUDGE")
    prompt = await DefaultComposer().compose(await collect(req), req)
    assert "CUSTOM-JUDGE" not in prompt.messages[-1].content
    assert "ROLE" in prompt.messages[-1].content
    assert "COMPACT {{ untouched }}" in prompt.messages[-1].content


@pytest.mark.parametrize("purpose,facet", [
    ("act", "act"), ("compact", "compact"),
    ("background_recap", "compact"), ("recognize_intent", "metadata"),
])
async def test_other_purposes_still_collect_one_role(purpose, facet):
    req = request(purpose)
    blocks = await collect(req)
    assert [b.metadata["facet_purpose"] for b in blocks] == [facet]
    prompt = await DefaultComposer().compose(blocks, req)
    assert ("## Summary Principles" in prompt.messages[-1].content) == (purpose == "background_recap")
    assert "ROLE" not in prompt.system + str(prompt.messages)


async def test_both_judge_facets_are_protected_and_budgeted():
    req = request("observe")
    blocks = await collect(req)
    budget = PriorityBudgetStrategy()
    total = sum(b.token_estimate for b in blocks)
    assert await budget.apply(blocks, total, req) == blocks
    with pytest.raises(ContextOverflowError):
        await budget.apply(blocks, total - 1, req)


@pytest.mark.parametrize("locale", ["en", "zh"])
@pytest.mark.parametrize("compact", [None, "", "COMPACT {{ untouched }}"])
async def test_observe_body_is_common_except_optional_role_and_submission(locale, compact, monkeypatch):
    load = prompts.load_prompt_template
    monkeypatch.setattr(prompts, "load_prompt_template", lambda name: load(name, locale))
    contexts, bodies = {}, {}
    for mode, purpose in [("judge", "background_observe"), ("recap", "background_recap")]:
        req = request(purpose, compact)
        # Even extraneous ROLE blocks must never leak into recap's instructions.
        blocks = await collect(request("background_observe", compact))
        _, entry, ctx = DefaultComposer().prepare(list(reversed(blocks)), req)
        contexts[mode] = ctx
        bodies[mode] = entry.render("body", ctx)
        assert "role_section" not in ctx
        assert ctx["evaluation_role"] == ("ROLE" if mode == "judge" else "")
        assert ctx["summary_principles"] == (compact or "")
        assert "## Your Current Role" not in bodies[mode]
    summary_heading = "## Summary Principles" if locale == "en" else "## 总结原则"
    evaluation_heading = "## Evaluation Principles" if locale == "en" else "## 评价原则"
    assert bodies["judge"].startswith(evaluation_heading + "\n\nROLE\n\n---\n\n")
    assert bodies["recap"].startswith(summary_heading + "\n\n")
    for mode in bodies:
        # Strip only the separate submission fragment; shared summary and context
        # must be identical, not just contain the same sentinel.
        pack = prompts.PromptPack(prompts.load_prompt_assets(locale))
        key = f"review.{mode}"
        submission = pack(key, **{k: v for k, v in contexts[mode].items() if k in prompts.CONNECTORS[key]})
        suffix = submission if mode == "judge" else "\n\n" + submission
        assert bodies[mode].endswith(suffix)
        bodies[mode] = bodies[mode][bodies[mode].index(summary_heading):-len(suffix)]
    assert bodies["judge"] == bodies["recap"]
