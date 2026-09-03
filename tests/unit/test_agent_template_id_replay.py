"""子 agent 的 template_id 必须能从事件流重建，而不是一律回落 session root 模板。

背景：`Agent.template_id` 在 spawn 时是正确的（AgentRegistry.instantiate 用
解析后的 sub_tmpl_id 构造），但它只活在内存的 `TaskRunner._resolved_agents` dict 里。
事件流里只有 root agent 发过 `AgentInstantiated`（session_manager），子 agent 那条从来
不发，且 reducer 根本不处理这个事件类型 —— 于是 `_rebuild_agents` 只能从 session/task
树推算，推不出模板，冷 resume 的 `pre_resolved` 就把每个 agent 都填成 root 的 template_id。

这在授权收口到 `ctx.agent_template_id` 之后是有后果的：AllowListAuthorizer 按模板做策略，
重建后的子 agent 会顶着 root 的模板身份。
"""

from __future__ import annotations

from datetime import UTC, datetime

from ctx_weft.core.control.reducers import reduce_events
from ctx_weft.protocols.events import Event, EventType

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)

SESSION = "s1"
ROOT_AGENT = "agt_root"
SUB_AGENT = "agt_sub"
ROOT_TMPL = "local:root-template"
SUB_TMPL = "local:researcher"


def _ev(n: int, type_: str, *, agent_id: str | None = None,
        task_id: str | None = None, payload: dict | None = None) -> Event:
    return Event(
        id=f"evt_{n:04d}",
        run_id="run_1",
        sequence=n,
        session_id=SESSION,
        type=type_,
        timestamp=_T0,
        agent_id=agent_id,
        task_id=task_id,
        payload=payload or {},
    )


def _stream() -> list[Event]:
    """root agent + 一个显式指定了 subagent_template 的子 agent。"""
    return [
        _ev(1, EventType.SESSION_CREATED, agent_id=ROOT_AGENT, payload={
            "template_id": ROOT_TMPL,
            "root_agent_id": ROOT_AGENT,
            "user_prompt": "hi",
        }),
        _ev(2, EventType.AGENT_INSTANTIATED, agent_id=ROOT_AGENT, payload={
            "template_id": ROOT_TMPL,
            "template_version": "1",
        }),
        _ev(3, EventType.TASK_CREATED, task_id="tsk_sub", payload={
            "task": {
                "id": "tsk_sub",
                "creator_agent_id": ROOT_AGENT,
                "assigned_agent_id": SUB_AGENT,
                "settings": {"use_subagent": True, "subagent_template": "researcher"},
            },
        }),
        _ev(4, EventType.AGENT_INSTANTIATED, agent_id=SUB_AGENT, payload={
            "template_id": SUB_TMPL,
            "template_version": "1",
        }),
    ]


def test_subagent_template_id_survives_replay() -> None:
    """重放后子 agent 带的是**自己**的模板，不是 root 的。"""
    view = reduce_events(_stream(), run_id="run_1")

    assert view.agents[SUB_AGENT].template_id == SUB_TMPL


def test_root_agent_template_id_survives_replay() -> None:
    view = reduce_events(_stream(), run_id="run_1")

    assert view.agents[ROOT_AGENT].template_id == ROOT_TMPL


def test_spawn_depth_and_parent_still_inferred() -> None:
    """折入 template_id 不得破坏 _rebuild_agents 原有的树形推算。

    这条是防回归的重点：若实现改成「_apply 里直接建 AgentView」，
    _rebuild_agents 的 `aid in view.agents → continue` 会跳过它，depth/parent 静默丢失。
    """
    view = reduce_events(_stream(), run_id="run_1")

    root = view.agents[ROOT_AGENT]
    sub = view.agents[SUB_AGENT]
    assert (root.spawn_depth, root.parent_agent_id) == (0, None)
    assert (sub.spawn_depth, sub.parent_agent_id) == (1, ROOT_AGENT)


def test_legacy_stream_without_agent_instantiated_falls_back_empty() -> None:
    """存量事件流没有子 agent 的 AgentInstantiated —— 不得报错，template_id 留空。

    留空而非猜测：调用方（冷 resume 的 pre_resolved）据此回落 session 模板，
    保持与改动前完全一致的行为，零数据迁移。
    """
    legacy = [ev for ev in _stream() if not (
        ev.type == EventType.AGENT_INSTANTIATED and ev.agent_id == SUB_AGENT
    )]

    view = reduce_events(legacy, run_id="run_1")

    assert view.agents[SUB_AGENT].template_id == ""
    assert view.agents[SUB_AGENT].spawn_depth == 1
