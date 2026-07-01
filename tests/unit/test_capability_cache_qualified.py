"""CapabilityCache: qualified-name lookup + cap.id-based duplicate detection."""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.capability_cache import (
    CapabilityCache,
    DuplicateCapabilityName,
)
from ctx_weft.protocols.capability import ToolCapability


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


# ─── session 全局区（控制工具）──────────────────────────────────────────────────

def test_global_region_survives_evict() -> None:
    """register_global 的控制工具不随 per-agent evict 逐出，get_by_qualified_name 仍命中。"""
    cache = CapabilityCache()
    cache.register_global([_cap("control:collect_process_report", "collect_process_report")])
    cache.put("agt_1", [_cap("mcp:a:search", "search")])

    # evict 前：per-agent + 全局都命中
    assert cache.get_by_qualified_name("agt_1", "control__collect_process_report").id == "control:collect_process_report"
    assert cache.get_by_qualified_name("agt_1", "mcp__a__search").id == "mcp:a:search"

    cache.evict("agt_1")  # per-agent 清空（模拟 run 收尾）

    # evict 后：控制工具（全局）仍命中；per-agent 的 mcp 已没
    assert cache.get_by_qualified_name("agt_1", "control__collect_process_report").id == "control:collect_process_report"
    assert cache.get_by_qualified_name("agt_1", "mcp__a__search") is None


def test_get_merges_global_control_tools() -> None:
    """get(agent) 返回 per-agent 快照 + session 全局控制工具（供 background observe 装配）。"""
    cache = CapabilityCache()
    cache.register_global([_cap("control:collect_process_report", "collect_process_report")])
    cache.put("agt_1", [_cap("mcp:a:search", "search")])
    ids = {c.id for c in cache.get("agt_1")}
    assert ids == {"mcp:a:search", "control:collect_process_report"}


def test_global_does_not_leak_across_evict_into_store() -> None:
    """全局区独立于 per-agent _store：evict 不动全局，且全局工具不污染另一未绑定 agent 的 get。"""
    cache = CapabilityCache()
    cache.register_global([_cap("control:finish_task", "finish_task")])
    cache.put("agt_1", [_cap("mcp:a:search", "search")])
    cache.evict("agt_1")
    # agt_2 从未 put → get 抛 KeyError（has_agent 守卫前置），但 get_by_qualified_name 仍可解析全局
    assert cache.get_by_qualified_name("agt_2", "control__finish_task").id == "control:finish_task"
