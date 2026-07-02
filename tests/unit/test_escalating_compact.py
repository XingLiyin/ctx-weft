# ctx-weft/tests/unit/test_escalating_compact.py
from types import SimpleNamespace
import pytest

from ctx_weft.core.loop.steps import compact as cm

pytestmark = pytest.mark.asyncio


def _state(ratio_trigger=0.8, ratio_target=0.6, limit=1000):
    agent = SimpleNamespace(
        id="a",
        loop_config=SimpleNamespace(
            compact_keep_last=6, collapse_keep_last=3,
            compact_token_ratio=ratio_trigger, compact_target_ratio=ratio_target),
        loop_guard=SimpleNamespace(context_limit=limit))
    return SimpleNamespace(scope=SimpleNamespace(), task=SimpleNamespace(id="t1"),
                           agent=agent, session=SimpleNamespace(id="s1", tenant_id="tn"),
                           extra={}, run_id="r1", sequence_counter=0)


async def test_stops_after_l1_when_under_target(monkeypatch):
    calls = []
    # L1 折后活跃 token 从 900 掉到 500（< target 600）→ 不进 L2/L3
    seq = iter([900, 500])
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _anext(seq))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(10))
    monkeypatch.setattr(cm, "summarize_for_compact", lambda s, c, *, scope="task": _const(f"sum-{scope}"))
    monkeypatch.setattr(cm, "fold_root_experience", lambda s, c, k, t: (calls.append("L1") or 5))
    monkeypatch.setattr(cm, "demote_kept_capsules", lambda s, c, o: (calls.append("L2") or 0))
    monkeypatch.setattr(cm, "collapse_task_layer", lambda s, c, k, t: (calls.append("L3") or 0))

    events = await cm.escalating_compact(_state(), SimpleNamespace(memory=None, provider_ctx=None),
                                         token_estimate=900, trigger="compact")
    assert calls == ["L1"]
    assert any(e.payload.get("layer") == "agent" for e in events)


async def test_escalates_l1_l2_l3(monkeypatch):
    calls = []
    seq = iter([900, 850, 820, 500])  # L1 后仍高、L2 后仍高、L3 后达标
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _anext(seq))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(10))
    monkeypatch.setattr(cm, "summarize_for_compact", lambda s, c, *, scope="task": _const(f"sum-{scope}"))
    monkeypatch.setattr(cm, "fold_root_experience", lambda s, c, k, t: (calls.append("L1") or 3))
    monkeypatch.setattr(cm, "demote_kept_capsules", lambda s, c, o: (calls.append("L2") or 2))
    monkeypatch.setattr(cm, "collapse_task_layer", lambda s, c, k, t: (calls.append("L3") or 4))
    monkeypatch.setattr(cm, "_kept_origin_ids", lambda s, c, keep: _const({"c1"}))
    # L3 现有可折性 guard：collapse_keep_last=3，须让 count_recent 报 > 3 条 task 层材料，L3 才会跑。
    memory = SimpleNamespace(count_recent=lambda scope, types, ctx: _const(10))

    await cm.escalating_compact(_state(), SimpleNamespace(memory=memory, provider_ctx=None),
                                token_estimate=900, trigger="compact")
    assert calls == ["L1", "L2", "L3"]


async def test_noop_when_no_context_limit(monkeypatch):
    st = _state(limit=0)
    events = await cm.escalating_compact(st, SimpleNamespace(memory=None, provider_ctx=None),
                                         token_estimate=900, trigger="compact")
    assert events == []


async def test_finished_event_aggregates(monkeypatch):
    # L1 折 5 条、活跃 token 900→500（freed=400）→ 达标即停
    seq = iter([900, 500])
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _anext(seq))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(10))
    monkeypatch.setattr(cm, "summarize_for_compact", lambda s, c, *, scope="task": _const(f"sum-{scope}"))
    monkeypatch.setattr(cm, "fold_root_experience", lambda s, c, k, t: 5)

    events = await cm.escalating_compact(
        _state(), SimpleNamespace(memory=None, provider_ctx=None),
        token_estimate=900, trigger="compact")

    types = [e.type for e in events]
    assert types[0] == "MemoryCompactStarted"
    assert types[-1] == "MemoryCompactFinished"
    fin = events[-1].payload
    assert fin["total_superseded"] == 5
    assert fin["freed_tokens"] == 400
    assert fin["levels"] == ["root_experience"]
    assert fin["est_before"] == 900 and fin["target_tokens"] == 600


async def test_no_started_no_finished_when_under_target():
    # est 500 < target 600 → 顶部早退，无 Started/Finished
    events = await cm.escalating_compact(
        _state(), SimpleNamespace(memory=None, provider_ctx=None),
        token_estimate=500, trigger="compact")
    assert events == []


async def test_finished_emitted_when_nothing_folded(monkeypatch):
    # est 达标但各级 guard 全跳过 → 仍发 Started+Finished，total_superseded=0
    monkeypatch.setattr(cm, "_active_memory_tokens", lambda s, c: _const(900))
    monkeypatch.setattr(cm, "_count_root_residues", lambda s, c: _const(0))       # L1 guard fail
    monkeypatch.setattr(cm, "_kept_origin_ids", lambda s, c, keep: _const(set())) # L2 no kept
    memory = SimpleNamespace(count_recent=lambda scope, types, ctx: _const(0))    # L3 no material

    events = await cm.escalating_compact(
        _state(), SimpleNamespace(memory=memory, provider_ctx=None),
        token_estimate=900, trigger="compact")

    assert [e.type for e in events] == ["MemoryCompactStarted", "MemoryCompactFinished"]
    assert events[-1].payload["total_superseded"] == 0
    assert events[-1].payload["levels"] == []


# 测试辅助：把常量/序列包装成 awaitable
async def _const(v):
    return v


async def _anext(it):
    return next(it)
