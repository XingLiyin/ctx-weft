"""HITL 的两条不随实现走的钉子：事件类型注册 + RuntimeConfig 旋钮接线。

本文件原先是 `HitlManager` 的生命周期测试（request → approve/answer/reject/超时、
两个触发点的端到端）。该类已删除，其行为在新子系统里逐条另有归属，不在此重复：

- 登记 / 幂等 / 决定缓存 / GC / pending 过滤 → `test_hitl_registry.py`
- 终局 / 事件载荷 / 幂等 no-op / cancel 带理由 → `test_hitl_service.py`
- 热等待、驱逐、热冷竞态 → `test_hitl_waiter.py`
- approval 门控端到端（放行 / 拒绝 / 改参 / 冷决定短路）→ `test_gateway_authz_hitl.py`
- ask_user 端到端（needs_human → 答复即结果，含多模态）→ `test_gateway_tool_needs_human.py`
  与 `test_hitl_ask_user_multimodal.py`
"""

from __future__ import annotations


def test_hitl_cancelled_is_registered_event() -> None:
    from ctx_weft.protocols.events import EVENT_TYPES, EventType
    assert EventType.HITL_CANCELLED == "HitlCancelled"
    assert "HitlCancelled" in EVENT_TYPES


def test_hitl_config_defaults() -> None:
    """RuntimeConfig default leaves hitl_timeout_sec as None (no eviction)."""
    from ctx_weft.core.config import RuntimeConfig
    cfg = RuntimeConfig()
    assert cfg.hitl_timeout_sec is None
    assert cfg.hitl_max_resolved == 1000


def test_runtime_wires_hitl_timeout() -> None:
    """CtxWeftRuntime injects hitl knobs from RuntimeConfig.

    旋钮的落点随重设计换了对象——超时归管栈的 `HitlWaiter`（runtime 在装配
    `LoopContext` 时用 `self._hitl_timeout_sec` 现造），保留上限归管账的
    `HitlRegistry`。旧版断言 `hitl_manager._timeout_sec`，钉的是「值到达了真正在等的
    那个对象」；这里必须走完同样的最后一跳——只断言 `rt._hitl_timeout_sec` 等于只测了
    一次属性赋值，`HitlWaiter(...)` 那行掉了实参照样绿。
    """
    from ctx_weft.core.config import RuntimeConfig
    from ctx_weft.protocols import ProviderContext
    from tests.integration.test_minimal_loop import InlineAgentTemplateProvider, make_runtime
    cfg = RuntimeConfig(hitl_timeout_sec=45)
    rt = make_runtime(agent_provider=InlineAgentTemplateProvider(), config=cfg)
    assert rt._hitl_timeout_sec == 45
    loop_ctx = rt._build_loop_ctx(
        assembler=None, llm=None, memory=None,
        provider_ctx=ProviderContext(session_id="s1", tenant_id="default"),
        gateway=None, skill_index={}, cancel_token=None, task_manager=None,
    )
    assert loop_ctx.waiter is not None and loop_ctx.waiter._timeout_sec == 45
