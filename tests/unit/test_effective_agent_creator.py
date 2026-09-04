"""_effective_agent：非 subagent 任务的串行键应落到 **creator**（延续创建者对话），
而非一律 root。root 仅作最终兜底（无 assigned、无 creator 时）。

背景：非 subagent 任务派发时在 `assigned or creator or root` 的 agent scope 上跑
（runtime._resolve）。串行键（_effective_agent）必须与之一致，否则 subagent 派生的
非 subagent 子任务会被误并进 root 桶 → 与真正的 root 任务并发跑进 root scope。
"""
from __future__ import annotations

from ctx_weft.core.orchestrator.task_manager import TaskManager
from ctx_weft.core.domain.models import NormalTaskSettings, Session, Task

ROOT = "root_agt"


def _tm() -> TaskManager:
    tm = TaskManager(session_id="s1", max_concurrent=1)
    tm.set_session(Session(id="s1", tenant_id="default", user_prompt="x",
                           status="RUNNING", token_budget=0, root_agent_id=ROOT))
    return tm


def _task(tid: str, *, creator: str | None = None, assigned: str | None = None,
          use_subagent: bool = False) -> Task:
    return Task(id=tid, session_id="s1", status="PENDING",
                creator_agent_id=creator, assigned_agent_id=assigned,
                settings=NormalTaskSettings(use_subagent=use_subagent))


def test_non_subagent_child_of_subagent_keys_on_creator_not_root():
    tm = _tm()
    # subagent S 派生的非 subagent 子（尚未 assign）→ 应跟 S 串行，而非 root
    key = tm._effective_agent(_task("C", creator="sub_S"))
    assert key == "sub_S"
    assert key != ROOT


def test_non_subagent_child_of_root_keys_on_root():
    tm = _tm()
    # root 建的非 subagent 子（creator=root）→ 仍是 root（creator==root，行为不变）
    assert tm._effective_agent(_task("P", creator=ROOT)) == ROOT


def test_assigned_wins_over_creator():
    tm = _tm()
    # 已派发（assigned 已回填）→ 用 assigned，不回退 creator
    assert tm._effective_agent(_task("D", creator="sub_S", assigned="real_agt")) == "real_agt"


def test_falls_back_to_root_when_no_assigned_no_creator():
    tm = _tm()
    assert tm._effective_agent(_task("E")) == ROOT


def test_two_different_creators_get_distinct_keys_for_parallelism():
    tm = _tm()
    # 不同 subagent 名下的非 subagent 步 → 不同键 → 可并行（不再全会话串在 root）
    assert tm._effective_agent(_task("X", creator="sub_A")) != tm._effective_agent(_task("Y", creator="sub_B"))


def test_subagent_branch_unchanged():
    tm = _tm()
    # subagent 任务仍走独立身份分支：assigned 空 → per-task 唯一 __sub__ 键（不受本改动影响）
    key = tm._effective_agent(_task("S", creator="whoever", use_subagent=True))
    assert key == "__sub__S"
