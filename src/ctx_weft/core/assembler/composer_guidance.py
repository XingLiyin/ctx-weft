"""Snapshot Act guidance/resume inputs before the loop consumes one-shot hints.

Prepare passes detached values; GuidanceSource renders guidance before budget trimming,
and the Jinja entry renders resume when the conversation needs a user continuation.
Legacy build_* helpers remain available for callers requesting text directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ctx_weft.core.assembler.prompts import connector
from ctx_weft.core.models.status import TERMINAL_TASK_STATUSES
from ctx_weft.core.utils.clock import as_utc
from ctx_weft.core.utils.content import content_to_text
from ctx_weft.core.utils.task_ref import task_label

_TERMINAL_STATUSES = TERMINAL_TASK_STATUSES  # 词表见 core.domain.status
_SUBTASK_RESULT_MAX = 150


def _nonterminal_tasks(task_manager) -> list:
    """session 内非终态 task（`_TERMINAL_STATUSES` 的补集），无 task_manager 时空表。"""
    if task_manager is None:
        return []
    return [t for t in task_manager.all_tasks() if t.status not in _TERMINAL_STATUSES]


def _has_other_open_tasks(task, task_manager) -> bool:
    """当前 task 之外是否还有其它非终态 task（决定是否提示「别自己做其它任务」）。"""
    return any(t.id != task.id for t in _nonterminal_tasks(task_manager))


def _finished_subtasks(task, task_manager) -> list:
    """当前 task 的 FINISHED 直接子任务，created_at 升序。

    只列 FINISHED：FAILED/CANCELED 的子任务可能需要重派/另行处理，不适合标成
    「已完成勿重做」。挂起恢复时任务树只剩非终态节点，完成的子任务从树里消失——
    这份清单把它们显式钉出来，防止 parent 把子任务的活自己再做一遍或重复派发。
    """
    if task_manager is None:
        return []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    done = [t for t in task_manager.all_tasks()
            if t.parent_task_id == task.id and t.status == "FINISHED"]
    # created_at 归一为 aware(UTC)：崩溃恢复重建的 task 可能带 naive 时间，与活任务的
    # aware 时间混排会抛 naive/aware 比较错（边界已在 converters 补齐，此处防御兜底）。
    done.sort(key=lambda t: as_utc(t.created_at) if t.created_at else epoch)
    return done


def _subtask_result_snippet(t) -> str:
    """完成子任务的结果摘要：outputs（成果物）优先，回退 task_summary；折叠单行、截断到 _SUBTASK_RESULT_MAX。

    parent 「build on their results」需要的是子任务的实际产出——outputs 是 finish_task 提交的
    deliverable（str 或 ContentPart 列表，content_to_text 归一）；outputs 空时回退 observer 的
    task_summary（整段综合总结）。多行折叠成单行，避免多行结果撑开每回合尾部的 guidance。
    两者皆空返回 ""（回退到只列 title，保持向后兼容）。
    """
    outputs = getattr(t, "outputs", None)
    text = outputs if isinstance(outputs, str) else (content_to_text(outputs) if outputs else "")
    if not (text or "").strip():
        text = getattr(t, "task_summary", None) or ""
    text = " ".join((text or "").split())  # 折叠所有空白（含换行）成单空格
    if not text:
        return ""
    return text[:_SUBTASK_RESULT_MAX] + "…" if len(text) > _SUBTASK_RESULT_MAX else text


def _session_task_tree(task, task_manager) -> str:
    """把 session 内**非终态** task 渲染成缩进任务树，当前 task 以 ``▶`` 标注。

    - 仅列非终态（`_TERMINAL_STATUSES` 的补集）——终态不刷屏、只留「还没做完的活」。
    - 按 parent_task_id 建树；父节点被过滤掉（终态/缺失）的非终态 task 提升到 root 层，避免孤儿丢失。
      roots 及同层子节点按 created_at 升序。
    - 只要有 ≥1 个非终态 task 就出树（含 root 独自 act 时只列它自己一行）——root 派生子任务后即转
      SUSPENDED 停止 act，故「单节点」唯一对应 root 独自 act 的情形，让它也能看到自己的定位。
    """
    tasks = _nonterminal_tasks(task_manager)
    if not tasks:
        return ""
    ids = {t.id for t in tasks}
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    children: dict[str | None, list] = {}
    for t in tasks:
        parent = t.parent_task_id if t.parent_task_id in ids else None
        children.setdefault(parent, []).append(t)
    for lst in children.values():
        lst.sort(key=lambda t: as_utc(t.created_at) if t.created_at else epoch)

    lines: list[str] = []

    def _walk(node_id: str | None, depth: int) -> None:
        for t in children.get(node_id, []):
            indent = "  " * depth
            marker = "▶ " if t.id == task.id else ""
            lines.append(f"{indent}- [{t.status}] {marker}{task_label(t)}")
            _walk(t.id, depth + 1)

    _walk(None, 0)
    return "\n".join(lines)


def build_resume_cue(
    task,
    task_manager,
    *,
    template: str = "act.resume",
) -> str:
    """续跑衔接 cue：历史以 assistant/tool 收尾时，composer 垫的 user 回合开场白。

    通用框架（盘点已完成、只做剩余）+ 确有已完成子任务时一句指向 guidance
    清单的提示（具体条目由 build_act_guidance 的 ALREADY COMPLETED 段承载，
    此处不重复展开）。
    """
    ref = task_label(task) if getattr(task, "id", None) else ""
    completed_tasks = bool(_finished_subtasks(task, task_manager))
    return connector(template, ref=ref or None, completed_tasks=completed_tasks, full_context=True)


def build_act_guidance(
    task, task_manager, *, template: str = "act.guidance",
) -> str:
    """Compatibility helper; new prepare calls pass the snapshot, not rendered prose."""
    return connector(template, **collect_guidance_context(task, task_manager))


def collect_resume_context(task, task_manager) -> dict:
    """Snapshot before task state changes; no task-manager reference escapes."""
    return dict(ref=task_label(task) if getattr(task, "id", None) else "",
                completed_tasks=bool(_finished_subtasks(task, task_manager)))


def collect_guidance_context(task, task_manager) -> dict:
    """构造 act 态势 guidance 文本（恒非空；调用方按 purpose/settings 门控）。

    段序 = 当前任务锚定 → plan 全景（任务树）→ 已完成子任务清单 → 静态指针。
    锚定行每个 act 回合都有：fresh 回合虽与同消息前部的 ``## Current Task`` 框
    略有重复，但长对话/续跑/追问回合里该框远在历史深处，此处是
    生成点附近唯一的任务锚。description 不重复（由 Current Task 框承载）。
    session 仅剩当前 task 一个非终态节点时，任务树整段不出现。
    """
    ref = task_label(task) if getattr(task, "id", None) else ""
    task_anchor = (connector("common.task_anchor", label=ref) if ref
                   else connector("common.task_anchor.unknown"))

    tree = _session_task_tree(task, task_manager)
    has_other_tasks = _has_other_open_tasks(task, task_manager)

    done = _finished_subtasks(task, task_manager)
    completed_lines: list[str] = []
    for t in done:
        completed_lines.append(f"- [FINISHED] {task_label(t)}")
        snippet = _subtask_result_snippet(t)
        if snippet:
            completed_lines.append(f"    → {snippet}")

    # review 的 note 也是「某个子任务产出不合格」的唯一入口（observer 只写判决与提示，
    # 不动任何 task 状态；reopen 已于 2026-09-19 删除）。上面那段的「例外」指的就是这里。
    hint = (getattr(task, "next_step_hint", None) or "").strip()
    # 文案与所有条件分支都在一个模板里；这里仅把 loop state 编译成具名槽位。
    return dict(
        task_anchor=task_anchor,
        plan_with_others=tree if tree and has_other_tasks else "",
        plan_solo=tree if tree and not has_other_tasks else "",
        completed_tasks="\n".join(completed_lines),
        review_note=hint,
        attended=not task.unattended,
        unattended=task.unattended,
        unattended_others=task.unattended and has_other_tasks,
    )
