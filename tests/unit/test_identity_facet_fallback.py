"""IdentitySource：background_observe facet 缺失 → 回退 observe（ROLE.md）→ 再回退 act。"""
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
async def test_background_observe_falls_back_to_act_when_no_observe():
    tpl = _template({"act": _facet("SOUL")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "SOUL"


@pytest.mark.asyncio
async def test_background_observe_prefers_own_facet():
    tpl = _template({"act": _facet("SOUL"), "observe": _facet("ROLE"),
                     "background_observe": _facet("BG")})
    blocks = await _facets(_req("background_observe", tpl))
    assert blocks[0].content == "BG"
