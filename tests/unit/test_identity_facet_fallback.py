"""IdentitySource：background_observe facet 缺失 → 回退 observe（ROLE.md）→ 到此为止。

observe 家族（observe / background_observe / background_recap）**不回退 act**
（2026-09-28）：见 `sources/identity.py::_OBSERVE_PURPOSES`。其余 purpose 照旧回退。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ctx_weft.core.assembler.sources.identity import IdentitySource
from ctx_weft.core.utils.estimate import estimate_tokens


def _facet(text): return SimpleNamespace(text=text, style="")


def _template(identity: dict):
    return SimpleNamespace(id="tpl", version="1", identity=identity)


def _req(purpose, template):
    return SimpleNamespace(purpose=purpose, template=template, extra={}, token_counter=estimate_tokens)


async def _facets(req):
    return [b async for b in IdentitySource().fetch(req, SimpleNamespace())]


@pytest.mark.asyncio
async def test_background_observe_falls_back_to_observe():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE-OBSERVE")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "ROLE-OBSERVE"


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["observe", "background_observe", "background_recap"])
async def test_observe_family_does_not_fall_back_to_act(purpose):
    """没有 ROLE 就是没有 observer——不拿 actor 的 SOUL 冒充一个。"""
    tpl = _template({"act": _facet("SOUL")})
    assert await _facets(_req(purpose, tpl)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["compact", "recognize_intent"])
async def test_the_actors_own_purposes_still_fall_back_to_act(purpose):
    """压自己的对话、认自己的意图——拿 SOUL 当人格是对的。"""
    tpl = _template({"act": _facet("SOUL")})
    blocks = await _facets(_req(purpose, tpl))
    assert blocks[0].content == "SOUL"


@pytest.mark.asyncio
async def test_background_observe_prefers_own_facet():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE"),
                     "background_observe": _facet("BG")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "BG"
