"""Agent 的字段集合是被钉住的——六个只写不读的字段已在 2026-09-02 删除。

与 tests/unit/test_session_status_domain.py 同类：值域/字段域由测试守，
免得「看着像状态机、其实没人读」的字段再长回来。
"""
from __future__ import annotations

import dataclasses

from ctx_weft.core.domain.models import Agent

EXPECTED_FIELDS = {
    "id", "session_id", "tenant_id",
    "template_id", "parent_agent_id", "spawn_depth",
    "memory_config", "loop_config", "loop_guard",
    "runtime", "created_at", "updated_at",
}

REMOVED_FIELDS = {
    "status", "template_version", "bound_capability_ids",
    "active_task_id", "tracking_task_ids", "fetched_tracking_ids",
}


def test_agent_field_set_is_pinned():
    actual = {f.name for f in dataclasses.fields(Agent)}
    assert actual == EXPECTED_FIELDS


def test_removed_fields_stay_removed():
    actual = {f.name for f in dataclasses.fields(Agent)}
    assert actual & REMOVED_FIELDS == set()


def test_agent_status_type_is_gone():
    """Agent 的状态不再是 `Agent` 的字段，也不由领域模型层定义。

    2026-09-04：`core.state` 更名为 `core.domain`，断言对象随之改为
    `core.domain.models`（原先断言的是包 `core.state`，而 `core.domain` 刻意不做
    re-export，断在子模块上更贴近「这个符号没有从这里出去」的本意）。
    agent 五态机的词表住在 `core.domain.status`，状态本身住在
    `AgentLifecycleManager._AgentRecord`。
    """
    import ctx_weft.core.domain.models as models
    assert not hasattr(models, "AgentStatus")
