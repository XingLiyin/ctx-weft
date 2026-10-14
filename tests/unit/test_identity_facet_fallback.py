"""Identity facets depend on observation judgement, never execution channel."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources.identity import IdentitySource
from ctx_weft.core.utils.estimate import estimate_tokens


def _facet(text): return SimpleNamespace(text=text, style="")


def _template(identity: dict):
    return SimpleNamespace(id="tpl", version="1", identity=identity)


def _req(purpose, template, judge_task=True):
    return SimpleNamespace(purpose=purpose, template=template, extra={"judge_task": judge_task}, token_counter=estimate_tokens)


async def _facets(req):
    return [b async for b in IdentitySource().fetch(req, SimpleNamespace())]


@pytest.mark.asyncio
async def test_background_observe_falls_back_to_observe():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE-OBSERVE")})
    blocks = await _facets(_req("observe", tpl))
    assert blocks[0].content == "ROLE-OBSERVE"


@pytest.mark.asyncio
async def test_background_recap_falls_back_to_compact():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE"),
                     "compact": _facet("COMPACT-RECAP")})
    blocks = await _facets(_req("observe", tpl, judge_task=False))
    assert blocks[0].content == "COMPACT-RECAP"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "purpose", ["observe", "compact"])
async def test_observe_and_compact_purposes_do_not_fall_back_to_act(purpose):
    """没有专属块就不产 identity——不拿 actor 的 SOUL 冒充 observer/压缩纪律。"""
    tpl = _template({"act": _facet("SOUL")})
    assert await _facets(_req(purpose, tpl)) == []


@pytest.mark.asyncio
async def test_recognize_intent_still_falls_back_to_act():
    """给自己的任务命名仍可拿 SOUL 当人格。"""
    tpl = _template({"act": _facet("SOUL")})
    blocks = await _facets(_req("recognize_intent", tpl))
    assert blocks[0].content == "SOUL"


@pytest.mark.asyncio
async def test_unused_facet_cannot_override_observe():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE"),
                     "unused": _facet("BG")})
    blocks = await _facets(_req("observe", tpl))
    assert blocks[0].content == "ROLE"
