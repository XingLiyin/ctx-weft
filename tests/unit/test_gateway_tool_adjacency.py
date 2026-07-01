"""Gateway 发送前合法化：tool result 位置相邻性。

OpenAI 兼容端点要求 role=tool 紧跟在 role=assistant(tool_calls) 之后、按 tool_call_id
配对，中间不得夹其它角色。跨层历史按 timestamp 归并会把 user 回合插进 assistant↔
tool_result 之间——集合级配对（drop_dangling/orphan）查不出这种错位，需 reorder 修正。
"""

from __future__ import annotations

from ctx_weft.core.loop.llm_gateway import (
    legalize_messages,
    reorder_tool_results_after_calls,
)
from ctx_weft.protocols import LLMMessage


def _assistant_call(*cids: str, content: str = "") -> LLMMessage:
    return LLMMessage(
        role="assistant", content=content,
        tool_calls=[{"id": c, "name": "t", "arguments": {}} for c in cids],
    )


def _tool_result(cid: str, content: str = "ok") -> LLMMessage:
    return LLMMessage(role="tool", content=content, tool_call_id=cid)


def _shape(msgs: list[LLMMessage]) -> list[tuple[str, str | None]]:
    """(role, tool_call_id|first tool_call id) 序列，便于断言位置。"""
    out: list[tuple[str, str | None]] = []
    for m in msgs:
        if m.role == "assistant" and m.tool_calls:
            out.append(("assistant", m.tool_calls[0].get("id")))
        elif m.role == "tool":
            out.append(("tool", m.tool_call_id))
        else:
            out.append((m.role, None))
    return out


def _adjacency_ok(msgs: list[LLMMessage]) -> bool:
    """每个带 tool_calls 的 assistant，其后必须紧跟覆盖全部 id 的 tool 消息（中间无他角色）。"""
    for i, m in enumerate(msgs):
        if m.role == "assistant" and m.tool_calls:
            need = {tc.get("id") for tc in m.tool_calls}
            j = i + 1
            while need and j < len(msgs) and msgs[j].role == "tool":
                need.discard(msgs[j].tool_call_id)
                j += 1
            if need:
                return False
    return True


# ── reorder_tool_results_after_calls ──────────────────────────────────────────


def test_moves_result_up_past_interleaved_user() -> None:
    # 这是会话里真实命中的形状：assistant(tool_calls) → user → tool(result)。
    msgs = [
        LLMMessage(role="user", content="做成 skill"),
        _assistant_call("c1"),
        LLMMessage(role="user", content="## Current Task ..."),
        _tool_result("c1", "Skill 已创建"),
    ]
    out = reorder_tool_results_after_calls(msgs)
    assert _shape(out) == [
        ("user", None),
        ("assistant", "c1"),
        ("tool", "c1"),
        ("user", None),
    ]
    assert _adjacency_ok(out)


def test_already_adjacent_unchanged() -> None:
    msgs = [LLMMessage(role="user", content="hi"), _assistant_call("c1"), _tool_result("c1")]
    out = reorder_tool_results_after_calls(msgs)
    assert out == msgs


def test_multiple_calls_ordered_by_tool_calls() -> None:
    # result 乱序 + 夹 user：按 tool_calls 顺序紧贴 assistant 排列。
    msgs = [
        LLMMessage(role="user", content="hi"),
        _assistant_call("c1", "c2"),
        _tool_result("c2", "two"),
        LLMMessage(role="user", content="noise"),
        _tool_result("c1", "one"),
    ]
    out = reorder_tool_results_after_calls(msgs)
    assert _shape(out) == [
        ("user", None),
        ("assistant", "c1"),
        ("tool", "c1"),
        ("tool", "c2"),
        ("user", None),
    ]
    assert _adjacency_ok(out)


def test_result_before_call_moved_after() -> None:
    msgs = [
        LLMMessage(role="user", content="hi"),
        _tool_result("c1", "early"),
        _assistant_call("c1"),
    ]
    out = reorder_tool_results_after_calls(msgs)
    assert _shape(out) == [("user", None), ("assistant", "c1"), ("tool", "c1")]
    assert _adjacency_ok(out)


def test_orphan_tool_left_in_place() -> None:
    # 无 owner（没有 assistant 调用 cX）的孤儿 tool 不动——交 drop_orphan_tool_results 处理。
    msgs = [LLMMessage(role="user", content="hi"), _tool_result("orphan")]
    out = reorder_tool_results_after_calls(msgs)
    assert out == msgs


# ── 全链路 legalize_messages ───────────────────────────────────────────────────


def test_legalize_yields_valid_adjacency() -> None:
    # 复现会话失败形状，跑完整合法化链后必须位置合法。
    msgs = [
        LLMMessage(role="user", content="做成 skill"),
        _assistant_call("c1"),
        LLMMessage(role="user", content="## Current Task ..."),
        _tool_result("c1", "Skill 已创建"),
        LLMMessage(role="user", content="Continue"),
    ]
    out = legalize_messages(msgs)
    assert _adjacency_ok(out)
    # tool result 必须仍在（未被误删）。
    assert any(m.role == "tool" and m.tool_call_id == "c1" for m in out)
