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

    await cm.escalating_compact(_state(), SimpleNamespace(memory=None, provider_ctx=None),
                                token_estimate=900, trigger="compact")
    assert calls == ["L1", "L2", "L3"]


async def test_noop_when_no_context_limit(monkeypatch):
    st = _state(limit=0)
    events = await cm.escalating_compact(st, SimpleNamespace(memory=None, provider_ctx=None),
                                         token_estimate=900, trigger="compact")
    assert events == []


# 测试辅助：把常量/序列包装成 awaitable
async def _const(v):
    return v


async def _anext(it):
    return next(it)
