"""ALM 的只读记录访问器（2026-09-04 spec §5.3）。

runtime 此前有多处直接读 `AgentLifecycleManager._agents`。私有穿透让
「ALM 是 agent 身份唯一住所」这条边界只存在于文档里。
"""

from __future__ import annotations

from ctx_weft.core.orchestrator.lifecycle.agent_manager import (
    AgentRecordView,
    _AgentRecord,
)
from ctx_weft.protocols import LoopConfig, MemoryConfig
from tests.integration.test_minimal_loop import (
    InlineAgentTemplateProvider,
    make_runtime,
)


def _alm():
    return make_runtime(agent_provider=InlineAgentTemplateProvider())._agent_lifecycle_manager


def _plant(alm, agent_id, *, session_id="s1", parent=None, status="idle"):
    alm._agents[agent_id] = _AgentRecord(
        session_id=session_id, tenant_id="default", template_id="tpl",
        parent_agent_id=parent, spawn_depth=0 if parent is None else 1,
        memory_config=MemoryConfig(), loop_config=LoopConfig(), status=status,
    )


def test_record_of_returns_view():
    alm = _alm()
    _plant(alm, "agt_1")
    rec = alm.record_of("agt_1")
    assert isinstance(rec, AgentRecordView)
    assert rec.agent_id == "agt_1"
    assert rec.session_id == "s1"
    assert rec.tenant_id == "default"
    assert rec.template_id == "tpl"
    assert rec.status == "idle"


def test_record_of_unknown_returns_none():
    """查无此 agent 不是编程错误——调用方（如 send_message 的守卫）自己决定怎么报。"""
    assert _alm().record_of("nope") is None


def test_view_is_frozen():
    alm = _alm()
    _plant(alm, "agt_1")
    rec = alm.record_of("agt_1")
    import dataclasses

    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.status = "running"


def test_view_is_a_snapshot_not_a_live_reference():
    """改 registry 不该反映到已经取出的视图上——否则调用方会拿到会变的『只读』对象。"""
    alm = _alm()
    _plant(alm, "agt_1", status="idle")
    snap = alm.record_of("agt_1")
    alm._agents["agt_1"].status = "running"
    assert snap.status == "idle"
    assert alm.record_of("agt_1").status == "running"


def test_set_current_task_updates_record():
    """`_start_task_for_agent` 的写口：ALM 是唯一改 `current_task_id` 的外部入口。"""
    alm = _alm()
    _plant(alm, "agt_1")
    alm.set_current_task("agt_1", "tsk_1")
    assert alm.record_of("agt_1").current_task_id == "tsk_1"


def test_set_current_task_unknown_agent_is_noop():
    alm = _alm()
    alm.set_current_task("nope", "tsk_1")  # 不抛错


def test_runtime_no_longer_reads_private_agents_dict():
    """结构性守卫：runtime.py 里不该再出现 `reg._agents` 私有穿透。"""
    from pathlib import Path
    src = Path("src/ctx_weft/core/runtime.py").read_text(encoding="utf-8")
    assert "reg._agents" not in src
