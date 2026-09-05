"""CapabilityCache 作为工具面唯一真相源：pin 槽的生命周期 + 渲染层纯函数。

pin 是「运行期把能力加进当前 task 的可用面」的唯一入口（provider yield
`CapabilityEvent(kind="pin")` → gateway → cache.pin）。这里锁死三条结构性事实：

  - `put()` 不碰 pin —— 同一 task 的 retry（重跑 prepare → 重新 put）必须保住 pin，
    否则 agent 每次重试都要重新发现同一批工具。
  - `evict()` 不碰 pin —— evict 是**每个 run** 都跑的（runtime `_run_loop` 的 finally），
    碰了等于 retry 必丢 pin。
  - `get()` 不含 pin —— 语义不变，全量路径（background observe）看不到 actor 循环内
    pin 的工具，是刻意保留的取舍；活工具面走 `available()`。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.sources.capability import build_llm_tools
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols.capability import CapabilityEvent, SkillCapability, ToolCapability


def _tool(cid: str, name: str = "", purposes: list[str] | None = None) -> ToolCapability:
    return ToolCapability(
        id=cid,
        name=name or cid.rsplit(":", 1)[-1],
        description=f"desc of {cid}",
        input_schema={"type": "object"},
        purposes=purposes or ["act"],
    )


# ── pin 查找 ──────────────────────────────────────────────────────────────────


def test_pinned_capability_is_resolvable_only_with_task_id() -> None:
    """pin 进来的能力经 task_id 才可见：不传 task_id 的旧调用点行为一字不变。"""
    cache = CapabilityCache()
    cache.put("agt_1", [_tool("mcp:a:search")])
    cache.pin("agt_1", "tsk_1", [_tool("mcp:b:pinned")])

    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned", "tsk_1").id == "mcp:b:pinned"
    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned") is None
    # 另一个 task 看不到别人的 pin
    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned", "tsk_2") is None


def test_pin_respects_template_forbidden() -> None:
    """模板 forbidden 是硬边界：运行期 pin 不能绕过它。"""
    cache = CapabilityCache()
    cache.put("agt_1", [_tool("mcp:a:search")], forbidden_ids={"mcp:x:danger"})
    cache.pin("agt_1", "tsk_1", [_tool("mcp:x:danger"), _tool("mcp:y:ok")])

    ids = {c.id for c in cache.available("agt_1", "tsk_1")}
    assert "mcp:x:danger" not in ids
    assert "mcp:y:ok" in ids


# ── 生命周期：put / evict 不碰 pin ─────────────────────────────────────────────


def test_put_does_not_clear_pins() -> None:
    """同 task 的 retry 会重跑 prepare（重新 put），pin 必须留着。"""
    cache = CapabilityCache()
    cache.put("agt_1", [_tool("mcp:a:search")])
    cache.pin("agt_1", "tsk_1", [_tool("mcp:b:pinned")])

    cache.put("agt_1", [_tool("mcp:a:search")])  # retry 的 prepare

    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned", "tsk_1") is not None


def test_evict_does_not_clear_pins() -> None:
    """evict 是每个 run 都跑的收尾，不是 task 终态；碰 pin 就等于 retry 必丢 pin。"""
    cache = CapabilityCache()
    cache.put("agt_1", [_tool("mcp:a:search")])
    cache.pin("agt_1", "tsk_1", [_tool("mcp:b:pinned")])

    cache.evict("agt_1")

    assert cache.get_by_qualified_name("agt_1", "mcp__a__search", "tsk_1") is None  # per-agent 没了
    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned", "tsk_1") is not None


def test_clear_pins_drops_them_and_is_idempotent() -> None:
    cache = CapabilityCache()
    cache.pin("agt_1", "tsk_1", [_tool("mcp:b:pinned")])
    cache.clear_pins("tsk_1")
    assert cache.available("agt_1", "tsk_1") == []
    cache.clear_pins("tsk_1")        # 重复清幂等
    cache.clear_pins("tsk_never")    # 不存在的 task 也幂等


def test_pin_lru_evicts_oldest_and_count_does_not_grow() -> None:
    cache = CapabilityCache(max_pins=3)
    for i in range(5):
        cache.pin("agt_1", "tsk_1", [_tool(f"mcp:p:t{i}")])

    ids = [c.id for c in cache.available("agt_1", "tsk_1")]
    assert len(ids) == 3
    assert ids == ["mcp:p:t2", "mcp:p:t3", "mcp:p:t4"]  # 最早的两个被挤掉


def test_pin_dedupes_by_id() -> None:
    cache = CapabilityCache(max_pins=8)
    cache.pin("agt_1", "tsk_1", [_tool("mcp:p:t0")])
    cache.pin("agt_1", "tsk_1", [_tool("mcp:p:t0"), _tool("mcp:p:t1")])
    assert [c.id for c in cache.available("agt_1", "tsk_1")] == ["mcp:p:t0", "mcp:p:t1"]


def test_get_excludes_pins_available_includes_them() -> None:
    """`get()` 语义不变（不含 pin）；活工具面走 `available()`。"""
    cache = CapabilityCache()
    cache.register_global([_tool("control:finish_task")])
    cache.put("agt_1", [_tool("mcp:a:search")])
    cache.pin("agt_1", "tsk_1", [_tool("mcp:b:pinned")])

    assert {c.id for c in cache.get("agt_1")} == {"mcp:a:search", "control:finish_task"}
    assert {c.id for c in cache.available("agt_1", "tsk_1")} == {
        "mcp:a:search", "control:finish_task", "mcp:b:pinned",
    }


# ── 渲染层纯函数 ───────────────────────────────────────────────────────────────


def test_build_llm_tools_filters_by_purpose_and_kind() -> None:
    caps = [
        _tool("mcp:a:act_only", purposes=["act"]),
        _tool("mcp:a:observe_only", purposes=["observe"]),
        SkillCapability(id="local_skill:pdf", name="pdf", description="d", purposes=["act"]),
    ]
    names = [t.name for t in build_llm_tools(caps, "act")]
    assert names == ["mcp__a__act_only"]           # skill 不进工具面，observe 专属被 purpose 挡掉

    assert [t.name for t in build_llm_tools(caps, "observe")] == ["mcp__a__observe_only"]

    tool = build_llm_tools(caps, "act")[0]
    assert tool.description == "desc of mcp:a:act_only"
    assert tool.input_schema == {"type": "object"}


# ── gateway：pin 事件不终止流 ─────────────────────────────────────────────────


async def test_gateway_pin_event_pins_and_keeps_consuming() -> None:
    """与 needs_human 相反：pin 不是流的终点，其后的 result 照常被收集。"""
    from ctx_weft.core.loop.capability_gateway import CapabilityGateway

    cache = CapabilityCache()
    gateway = CapabilityGateway(
        capability_cache=cache,
        capability_providers=[],
        memory=None,
        event_bus=None,
    )
    state = SimpleNamespace(agent=SimpleNamespace(id="agt_1"), task=SimpleNamespace(id="tsk_1"))

    async def _events():
        yield CapabilityEvent(kind="pin", payload={"capabilities": [_tool("mcp:b:pinned")]})
        yield CapabilityEvent(kind="result", payload={"content": "done"})

    parts, _metadata, is_error, ask = await gateway._stream_events(_events(), state, "inv_1")

    assert parts == ["done"]        # pin 之后的事件仍被消费
    assert is_error is False
    assert ask is None
    assert cache.get_by_qualified_name("agt_1", "mcp__b__pinned", "tsk_1").id == "mcp:b:pinned"


# ── 回归：活工具面 == 改造前那条流水线的产物 ─────────────────────────────────

async def test_live_tool_surface_matches_pre_refactor_pipeline() -> None:
    """无 pin 时，cache 现算的工具面与改造前逐个元素相同——**顺序也相同**。

    改造的前提是「只换来源，不换内容」，而这条前提此前没有任何测试钉住：
    「不装 tools_fn 就是旧行为」测的是没装闭包那条路，可生产路径上闭包恒装。

    期望值取 `build_llm_tools(bound, "act")`：`bound` 就是 `resolve_and_bind` 的返回值，
    也正是改造前喂给 CapabilitySource、经 metadata["llm_tool"] 汇成 tools 数组的那一份。
    两边同一个纯函数，差的只有输入的组装方式（一份直传、一份经 cache 的三个槽重组）——
    这正是要钉的东西：控制工具在前、其余在后的顺序不得因为换了来源而改变
    （tools 数组的 KV cache 前缀会跟着变）。
    """
    from ctx_weft.core.capabilities.control_tools import ControlCapabilityProvider
    from ctx_weft.core.capabilities.skill_executor import SkillExecutorCapabilityProvider
    from ctx_weft.core.loop.steps._capabilities import resolve_and_bind
    from ctx_weft.core.models.task import NormalTaskSettings
    from ctx_weft.protocols.capability import CapabilityProvider, CapabilityProviderInfo
    from ctx_weft.protocols.context import ProviderContext

    class _Control(ControlCapabilityProvider):
        async def list(self, ctx):
            return [
                _tool("control:finish_task", purposes=["act"]),
                _tool("control:report_task_outcome", purposes=["observe"]),  # act 面看不到
            ]

    class _SkillExec(SkillExecutorCapabilityProvider):
        def __init__(self) -> None:  # 不需要 registry：本测试只用它的 list()
            pass

        async def list(self, ctx):
            return [_tool("skill_executor:load_skill", purposes=["act"])]

    class _Mcp(CapabilityProvider):
        name = "mcp:github"

        async def list(self, ctx):
            return [
                _tool("mcp:github:create_issue", purposes=["act"]),
                _tool("mcp:github:danger", purposes=["act"]),        # 模板 forbidden
                SkillCapability(id="mcp:github:doc", name="doc", description="d"),  # 非 tool
            ]

        async def describe(self, ctx):
            return CapabilityProviderInfo(name=self.name)

    template = SimpleNamespace(
        id="tpl_1",
        capability_refs=[SimpleNamespace(capability_id="mcp:github:danger", mode="forbidden")],
    )
    state = SimpleNamespace(
        agent=SimpleNamespace(id="agt_1"),
        task=SimpleNamespace(id="tsk_1", settings=NormalTaskSettings(skill_name="pdf")),
        extra={"template": template},
    )
    cache = CapabilityCache()
    ctx = SimpleNamespace(
        capability_cache=cache,
        capability_providers=[_Control(), _SkillExec(), _Mcp()],
        provider_ctx=ProviderContext(session_id="s1", task_id="tsk_1", agent_id="agt_1"),
    )

    bound = await resolve_and_bind(state, ctx)

    expected = build_llm_tools(bound, "act")
    assert [t.name for t in expected] == [
        "control__finish_task",            # 控制工具在前（builtin_caps）
        "skill_executor__load_skill",
        "mcp__github__create_issue",
    ]
    assert build_llm_tools(cache.available("agt_1", "tsk_1"), "act") == expected

    # pin 只**追加**在末尾，不打乱既有前缀（KV cache 前缀因此仍然复用）。
    cache.pin("agt_1", "tsk_1", [_tool("mcp:github:pinned")])
    live = build_llm_tools(cache.available("agt_1", "tsk_1"), "act")
    assert live[: len(expected)] == expected
    assert [t.name for t in live[len(expected):]] == ["mcp__github__pinned"]
