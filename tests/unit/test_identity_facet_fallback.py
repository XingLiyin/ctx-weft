"""IdentitySource collects the template; Jinja selects the step facet."""
from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.identity import IdentitySource
from tests.assembler_testkit import compile_prompt, prompt_request


def _facet(text):
    return SimpleNamespace(text=text, style="")


def _template(identity: dict):
    return SimpleNamespace(id="tpl", version="1", identity=identity)


def _request(purpose, template, judge_task=True):
    return prompt_request(
        purpose=purpose,
        template=template,
        extra={"judge_task": judge_task},
    )


async def _blocks(request):
    return [block async for block in IdentitySource().fetch(request, SimpleNamespace())]


def _text(prompt):
    return prompt.system + "\n" + "\n".join(
        str(message.content) for message in prompt.messages)


async def test_identity_source_returns_raw_template():
    template = _template({"act": _facet("SOUL")})
    block = (await _blocks(_request("act", template)))[0]

    assert block.value is template
    assert block.kind == "identity"


async def test_observe_uses_observe_facet():
    template = _template({"act": _facet("SOUL"), "observe": _facet("ROLE-OBSERVE")})
    request = _request("observe", template)

    assert "ROLE-OBSERVE" in _text(compile_prompt(await _blocks(request), request))


async def test_background_recap_uses_compact_facet():
    template = _template({
        "act": _facet("SOUL"),
        "observe": _facet("ROLE"),
        "compact": _facet("COMPACT-RECAP"),
    })
    request = _request("observe", template, judge_task=False)
    text = _text(compile_prompt(await _blocks(request), request))

    assert "COMPACT-RECAP" in text
    assert "ROLE\n" not in text


async def test_observe_without_facet_does_not_use_act_as_role():
    template = _template({"act": _facet("SOUL")})
    request = _request("observe", template)
    prompt = compile_prompt(await _blocks(request), request)
    messages = "\n".join(str(message.content) for message in prompt.messages)

    assert "## Your Current Role\n\nSOUL" not in messages


async def test_compact_without_facet_does_not_use_act_as_role():
    template = _template({"act": _facet("SOUL")})
    request = _request("compact", template)
    prompt = compile_prompt(await _blocks(request), request)
    messages = "\n".join(str(message.content) for message in prompt.messages)

    assert "## Your Current Role\n\nSOUL" not in messages


async def test_recognize_intent_falls_back_to_act():
    template = _template({"act": _facet("SOUL")})
    request = _request("recognize_intent", template)

    assert "SOUL" in _text(compile_prompt(await _blocks(request), request))


async def test_unused_facet_cannot_override_observe():
    template = _template({
        "act": _facet("SOUL"),
        "observe": _facet("ROLE"),
        "unused": _facet("BG"),
    })
    request = _request("observe", template)
    text = _text(compile_prompt(await _blocks(request), request))

    assert "ROLE" in text
    assert "BG" not in text
