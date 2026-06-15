"""CapabilityCache: qualified-name lookup + cap.id-based duplicate detection."""

from __future__ import annotations

import pytest

from loomex_core.core.orchestrator.capability_cache import (
    CapabilityCache,
    DuplicateCapabilityName,
)
from loomex_core.protocols.capability import ToolCapability


def _cap(cid: str, name: str) -> ToolCapability:
    return ToolCapability(id=cid, name=name, description="d")


def test_two_mcp_tools_same_bare_name_coexist() -> None:
    cache = CapabilityCache()
    caps = [_cap("mcp:a:search", "search"), _cap("mcp:b:search", "search")]
    cache.put("agt_1", caps)  # must NOT raise on duplicate bare name
    assert cache.get_by_qualified_name("agt_1", "mcp__a__search").id == "mcp:a:search"
    assert cache.get_by_qualified_name("agt_1", "mcp__b__search").id == "mcp:b:search"


def test_get_by_qualified_name_miss_returns_none() -> None:
    cache = CapabilityCache()
    cache.put("agt_1", [_cap("mcp:a:search", "search")])
    assert cache.get_by_qualified_name("agt_1", "search") is None  # bare does not resolve


def test_duplicate_cap_id_still_raises() -> None:
    cache = CapabilityCache()
    with pytest.raises(DuplicateCapabilityName):
        cache.put("agt_1", [_cap("mcp:a:search", "search"), _cap("mcp:a:search", "dup")])
