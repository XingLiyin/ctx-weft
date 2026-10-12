"""黄金用例一致性 runner。

读 `docs/spec/golden/*.json`，对每个用例：
  1. 全量回放：reduce_events(events) 的字段 ⊇ expected，逐字段相等。
  2. 若用例含 snapshotAt=k：验证「快照(前 k 条) + 增量」== 全量回放（崩溃恢复不变式）。

这是 spec（docs/spec/）与本实现的一致性闸门，也是 Java / TS 移植可直接照搬的测试模板：
同一组 golden JSON，三份实现各跑同一断言。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ctx_weft.core.control.reducers import (
    apply_events,
    deserialize_view,
    reduce_events,
    serialize_view,
)
from ctx_weft.protocols.events import Event

_GOLDEN_DIR = Path(__file__).parents[2] / "docs" / "spec" / "golden"

# Event 信封：canonical camelCase（JSON）→ Python snake_case
_ENVELOPE = {
    "runId": "run_id", "sessionId": "session_id", "taskId": "task_id",
    "agentId": "agent_id", "tenantId": "tenant_id", "causationId": "causation_id",
    "schemaVersion": "schema_version",
}


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _to_event(d: dict[str, Any]) -> Event:
    kwargs: dict[str, Any] = {}
    for k, v in d.items():
        if k == "timestamp":
            kwargs["timestamp"] = datetime.fromisoformat(v)  # 3.11 支持结尾 "Z"
        else:
            kwargs[_ENVELOPE.get(k, k)] = v
    return Event(**kwargs)


def _load_cases() -> list[tuple[str, dict[str, Any]]]:
    if not _GOLDEN_DIR.exists():
        return []
    cases = []
    for f in sorted(_GOLDEN_DIR.glob("*.json")):
        cases.append((f.name, json.loads(f.read_text(encoding="utf-8"))))
    return cases


# 顶层标量断言字段：canonical camelCase → RunStateView 属性
_TOP = {
    "sessionStatus": "session_status",
    "taskStatus": "task_status",
    "currentStep": "current_step",
    "assembledPromptTokens": "assembled_prompt_tokens",
    "transcriptTurns": "transcript_turns",
    "eventsTotal": "events_total",
}


def _check(view: Any, expected: dict[str, Any]) -> None:
    for key, attr in _TOP.items():
        if key in expected:
            got = getattr(view, attr)
            assert got == expected[key], f"{key}: got {got!r}, want {expected[key]!r}"

    for sid, fields in expected.get("sessions", {}).items():
        sv = view.sessions.get(sid)
        assert sv is not None, f"session {sid} missing"
        for k, want in fields.items():
            got = getattr(sv, _camel_to_snake(k))
            assert got == want, f"session {sid}.{k}: got {got!r}, want {want!r}"

    for tid, fields in expected.get("tasks", {}).items():
        tv = view.tasks.get(tid)
        assert tv is not None, f"task {tid} missing"
        for k, want in fields.items():
            got = getattr(tv, _camel_to_snake(k))
            assert got == want, f"task {tid}.{k}: got {got!r}, want {want!r}"

    agents = expected.get("agents")
    if isinstance(agents, list):
        assert set(view.agents.keys()) == set(agents)
    elif isinstance(agents, dict):
        for aid, fields in agents.items():
            av = view.agents.get(aid)
            assert av is not None, f"agent {aid} missing"
            for k, want in fields.items():
                got = getattr(av, _camel_to_snake(k))
                assert got == want, f"agent {aid}.{k}: got {got!r}, want {want!r}"


_CASES = _load_cases()


def test_golden_dir_present() -> None:
    assert _CASES, f"no golden cases found under {_GOLDEN_DIR}"


@pytest.mark.parametrize("name,case", _CASES, ids=[n for n, _ in _CASES])
def test_golden_full_replay(name: str, case: dict[str, Any]) -> None:
    events = [_to_event(e) for e in case["events"]]
    view = reduce_events(events, run_id=case["runId"])
    _check(view, case["expected"])


@pytest.mark.parametrize("name,case", _CASES, ids=[n for n, _ in _CASES])
def test_golden_snapshot_plus_delta(name: str, case: dict[str, Any]) -> None:
    """快照(前缀) + 增量 == 全量回放。用例未指定 snapshotAt 则默认在中点切。"""
    events = [_to_event(e) for e in case["events"]]
    k = case.get("snapshotAt", len(events) // 2)
    if k <= 0 or k >= len(events):
        pytest.skip("no meaningful split point")

    full = reduce_events(events, run_id=case["runId"])

    head_view = reduce_events(events[:k], run_id=case["runId"])
    restored = deserialize_view(serialize_view(head_view))   # 模拟快照往返
    rebuilt = apply_events(events[k:], restored)

    # snapshot+delta 必须与全量在所有断言字段上一致
    _check(rebuilt, case["expected"])
    assert rebuilt.session_status == full.session_status
    assert {t: v.status for t, v in rebuilt.tasks.items()} == \
           {t: v.status for t, v in full.tasks.items()}
    assert set(rebuilt.agents) == set(full.agents)
