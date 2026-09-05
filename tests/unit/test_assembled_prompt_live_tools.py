"""AssembledPrompt.tools 是**活的**：装上 tools_fn 后每次读都问 CapabilityCache。

改造前 tools 是装配期的一份拷贝，与 cache 各存一份、装配后再无同步；ActStep 的每一轮
读的都是同一个 prompt 对象（PrepareStep 只装配一次），运行期新增的能力永远进不了工具面。
现在 tools 成了 property：没装 tools_fn 时逐字节返回构造时那份（旧行为、旧构造点零改动），
装了就返回 cache 的当前视图。
"""

from __future__ import annotations

from types import SimpleNamespace

from ctx_weft.core.assembler.assembler import (
    AssemblerDeps,
    AssembledPrompt,
    ContextAssembler,
    ContextRequest,
)
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols.capability import ToolCapability
from ctx_weft.protocols.llm import LLMMessage, LLMTool


def _llm_tool(name: str) -> LLMTool:
    return LLMTool(name=name, description="d", input_schema={})


def _cap(cid: str) -> ToolCapability:
    return ToolCapability(id=cid, name=cid.rsplit(":", 1)[-1], description="d",
                          input_schema={}, purposes=["act"])


# ── 回归：不装 tools_fn 就是旧行为 ─────────────────────────────────────────────


def test_tools_without_fn_is_exactly_what_was_passed() -> None:
    tools = [_llm_tool("a"), _llm_tool("b")]
    prompt = AssembledPrompt(system="s", messages=[], tools=tools, token_count=0)
    assert prompt.tools == tools
    assert [t.name for t in prompt.tools] == ["a", "b"]


def test_positional_construction_still_works() -> None:
    """既有构造点有按位置传的，签名与参数名一字不改。"""
    prompt = AssembledPrompt("s", [LLMMessage(role="user", content="hi")], [_llm_tool("a")], 7)
    assert prompt.system == "s"
    assert prompt.token_count == 7
    assert [t.name for t in prompt.tools] == ["a"]
    assert prompt.metadata == {}


# ── 活工具面 ───────────────────────────────────────────────────────────────────


def test_tools_reflect_cache_when_fn_installed() -> None:
    from ctx_weft.core.assembler.sources.capability import build_llm_tools

    cache = CapabilityCache()
    cache.put("agt_1", [_cap("mcp:a:search")])
    prompt = AssembledPrompt(system="s", messages=[], tools=[], token_count=0)
    prompt.tools_fn = lambda: build_llm_tools(cache.available("agt_1", "tsk_1"), "act")

    assert [t.name for t in prompt.tools] == ["mcp__a__search"]

    cache.pin("agt_1", "tsk_1", [_cap("mcp:b:pinned")])  # 运行期新增
    assert [t.name for t in prompt.tools] == ["mcp__a__search", "mcp__b__pinned"]


def test_repeated_reads_do_not_accumulate() -> None:
    """读两次不得越读越长——property 不能就地 extend 构造时那份。"""
    base = [_llm_tool("a")]
    prompt = AssembledPrompt(system="s", messages=[], tools=base, token_count=0)
    prompt.tools_fn = lambda: [_llm_tool("live")]
    first = list(prompt.tools)
    second = list(prompt.tools)
    assert first == second
    assert len(second) == 1
    assert base == [_llm_tool("a")]  # 构造时那份没被就地改


def test_tools_fn_exception_falls_back_to_snapshot() -> None:
    """活来源炸了不该让整轮 LLM 调用炸：回落装配期那份。"""
    def _boom():
        raise RuntimeError("cache gone")

    prompt = AssembledPrompt(system="s", messages=[], tools=[_llm_tool("a")], token_count=0)
    prompt.tools_fn = _boom
    assert [t.name for t in prompt.tools] == ["a"]


# ── assembler 接线 ─────────────────────────────────────────────────────────────


class _StubComposer:
    async def compose(self, blocks, request):
        return AssembledPrompt(system="", messages=[], tools=[], token_count=0)


def _req(purpose: str) -> ContextRequest:
    session = SimpleNamespace(id="s1", context_limit=100_000, reserved_output_tokens=1024)
    return ContextRequest(
        purpose=purpose,
        scope=SimpleNamespace(session_id="s1", task_id="tsk_1", agent_id="agt_1"),
        task=SimpleNamespace(id="tsk_1"),
        agent=SimpleNamespace(id="agt_1"),
        session=session,
        template=None,
        bound_capabilities=[],
        extra={},
    )


def _assembler(cache) -> ContextAssembler:
    from ctx_weft.core.assembler.budget import PriorityBudgetStrategy
    deps = AssemblerDeps(
        memory=None, knowledge_providers=[],
        provider_ctx=SimpleNamespace(session_id="s1", task_id="tsk_1", agent_id="agt_1"),
        capability_cache=cache, agent_id="agt_1",
    )
    return ContextAssembler(sources=[], budget=PriorityBudgetStrategy(),
                            composer=_StubComposer(), deps=deps)


async def test_assemble_installs_live_tools_fn() -> None:
    cache = CapabilityCache()
    cache.put("agt_1", [_cap("mcp:a:search")])
    prompt = await _assembler(cache).assemble(_req("act"))
    assert [t.name for t in prompt.tools] == ["mcp__a__search"]

    cache.pin("agt_1", "tsk_1", [_cap("mcp:b:pinned")])
    assert [t.name for t in prompt.tools] == ["mcp__a__search", "mcp__b__pinned"]


async def test_assemble_leaves_compact_tools_empty() -> None:
    """compact 的工具面恒为空——不装 tools_fn。"""
    cache = CapabilityCache()
    cache.put("agt_1", [_cap("mcp:a:search")])
    prompt = await _assembler(cache).assemble(_req("compact"))
    assert prompt.tools == []


async def test_assemble_without_cache_keeps_composer_tools() -> None:
    cache = None
    prompt = await _assembler(cache).assemble(_req("act"))
    assert prompt.tools == []
