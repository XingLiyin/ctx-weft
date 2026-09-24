"""agent 存的 template_id 必须是**可路由**的（2026-09-24 线上暴露）。

## bug 的形状

`instantiate` 存的是 `template.id`——provider 内部的 local name（`ipmaster`）。而
`TemplateLookup.get_template` 按前缀精确路由，**裸 id 一律抛 `TemplateNotFoundError`**
（那是有意的，避免跨 provider 歧义）。于是 qualified 形态（`agent:ipmaster`）只在「调用
方传进来」那一刻存在过，一经存储就再也要不回来。

后果在恢复期兑现：`AgentLifecycleManager.load` 拿裸 id 去解析必然失败，每个 agent 静默
降级用 `MemoryConfig()` / `LoopConfig()` 默认值，而不是模板里配的那套。线上一次重启刷了
四条 `template 'ipmaster' unresolvable ... using default configs`。

修法分两半：写入侧存可路由形态；已经存了裸 id 的历史事件改不了，恢复期对裸 id 再补一次
前缀重试。
"""

from __future__ import annotations

import pytest

from ctx_weft.core.orchestrator.lifecycle.agent_manager import AgentLifecycleManager
from ctx_weft.protocols.template import AgentTemplate, IdentityFacet


class _Provider:
    """最小 agent 模板 provider——只认自己名下的一个模板。"""

    def __init__(self, name: str, local: str = "tpl") -> None:
        self.name = name
        self._local = local

    async def get_template(self, local_name, version, ctx):
        if local_name != self._local:
            return None
        from ctx_weft.protocols.template import LoopConfig, MemoryConfig
        return AgentTemplate(
            id=self._local, name=self._local, version="1.0.0",
            identity={"act": IdentityFacet(text="soul")},
            capability_refs=[], memory_config=MemoryConfig(), loop_config=LoopConfig(),
        )

    async def list(self, ctx):
        return []


class _Lookup:
    def __init__(self, providers: list) -> None:
        self._providers = providers

    def agent_providers(self):
        return list(self._providers)

    async def get_template(self, ref, version, ctx):
        """与生产同口径：裸 id / 未知前缀一律抛。"""
        if ":" in ref:
            provider_name, local = ref.rsplit(":", 1)
            for p in self._providers:
                if p.name == provider_name:
                    tpl = await p.get_template(local, version, ctx)
                    if tpl is None:
                        raise LookupError(ref)
                    return tpl
        raise LookupError(ref)


class _SpyBus:
    async def emit(self, event) -> None:
        return None


def _mgr(lookup) -> AgentLifecycleManager:
    return AgentLifecycleManager(template_lookup=lookup, event_bus=_SpyBus(),
                                 model_resolver=lambda a, m: None)


def _alm(providers: list) -> AgentLifecycleManager:
    return _mgr(_Lookup(providers))


# ── 写入侧：存可路由形态 ──────────────────────────────────────────────────────

async def test_instantiate_stores_the_routable_id() -> None:
    alm = _alm([_Provider("agent")])
    agent, tpl = await alm.instantiate(
        template_id="agent:tpl", session_id="s1", tenant_id="default")

    assert agent.template_id == "agent:tpl", "存的必须是能再路由回去的形态"
    assert tpl.id == "tpl", "provider 返回的仍是 local name —— 正是不能直接存的那个"


async def test_stored_id_survives_a_round_trip() -> None:
    """存进去的 id 拿出来必须还能解析——这是整条修复的要害。"""
    lookup = _Lookup([_Provider("agent")])
    alm = _mgr(lookup)
    agent, _ = await alm.instantiate(
        template_id="agent:tpl", session_id="s1", tenant_id="default")

    again = await lookup.get_template(agent.template_id, None, None)
    assert again.id == "tpl"


# ── 恢复侧：存量裸 id 的补前缀重试 ────────────────────────────────────────────

async def test_recovery_resolves_a_bare_legacy_id() -> None:
    """历史事件里存着裸 id，改不了——恢复期补前缀救回来。"""
    alm = _alm([_Provider("agent")])
    tpl = await alm._resolve_for_recovery("tpl", "agt_1", None)
    assert tpl is not None and tpl.id == "tpl"


async def test_recovery_prefers_the_exact_route() -> None:
    """已经是 qualified 的照常走精确路由，不进补前缀那支。"""
    alm = _alm([_Provider("agent")])
    tpl = await alm._resolve_for_recovery("agent:tpl", "agt_1", None)
    assert tpl is not None


async def test_recovery_warns_when_a_bare_id_is_ambiguous(caplog) -> None:
    """两家 provider 都有同名模板：取第一个，但必须留痕——存量数据没记是哪一家。"""
    alm = _alm([_Provider("agent"), _Provider("cowork")])
    with caplog.at_level("WARNING"):
        tpl = await alm._resolve_for_recovery("tpl", "agt_1", None)
    assert tpl is not None
    assert any("matches 2 providers" in r.message for r in caplog.records)


async def test_recovery_gives_up_loudly_when_nothing_matches(caplog) -> None:
    """真解析不出就降级，但要有一条 warning——静默用默认 configs 正是这次的教训。"""
    alm = _alm([_Provider("agent", local="other")])
    with caplog.at_level("WARNING"):
        assert await alm._resolve_for_recovery("tpl", "agt_1", None) is None
    assert any("unresolvable" in r.message for r in caplog.records)


async def test_recovery_tolerates_a_lookup_without_the_accessor() -> None:
    """手构的替身 lookup 没有 `agent_providers`——降级，不崩。"""
    class _Bare:
        async def get_template(self, ref, version, ctx):
            raise LookupError(ref)

    alm = _mgr(_Bare())
    assert await alm._resolve_for_recovery("tpl", "agt_1", None) is None


# ── 入参形态不唯一：LLM 委派给的是 `provider__name` ──────────────────────────
#
# host 建根 agent 时给规范形态（`agent:ipmaster`），而 LLM 委派子 agent 时给的是
# LLM-facing 的 qualified 名（`agent__researcher`，见 delegate_task 的 subagent_template）。
# 两种都得归一到可路由的那个，否则子 agent 恢复时同样降级。

async def test_llm_facing_form_is_normalised() -> None:
    alm = _alm([_Provider("agent")])
    assert await alm._normalise_template_id("agent__tpl", None) == "agent:tpl"


async def test_canonical_form_passes_through() -> None:
    alm = _alm([_Provider("agent")])
    assert await alm._normalise_template_id("agent:tpl", None) == "agent:tpl"


async def test_bare_id_is_left_alone_for_recovery_to_guess() -> None:
    """裸 id 没有分隔符可拆——原样存，恢复期的补前缀重试去兜。"""
    alm = _alm([_Provider("agent")])
    assert await alm._normalise_template_id("tpl", None) == "tpl"


async def test_instantiate_normalises_the_llm_facing_form() -> None:
    """端到端：LLM 给 `agent__tpl`，存进去的必须是 `agent:tpl`。"""
    lookup = _Lookup([_Provider("agent")])
    alm = _mgr(lookup)
    tpl = await _Provider("agent").get_template("tpl", None, None)
    agent, _ = await alm.instantiate(
        template_id="agent__tpl", session_id="s1", tenant_id="default", template=tpl)

    assert agent.template_id == "agent:tpl"
    assert (await lookup.get_template(agent.template_id, None, None)).id == "tpl"
