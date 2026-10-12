"""工具鉴权（Authorizer）+ CapabilityGateway 鉴权集成 的行为测试。

覆盖：
1. AllowAllAuthorizer：放行全部。
2. AllowListAuthorizer：allow_map 白名单 / deny_map 黑名单 / 未登记模板回落放行 / 空集合全拦。
3. CapabilityGateway.invoke：被鉴权器拦截 → 返回 error 且**不调用 provider**；放行 → 正常执行。
4. _get_authorizer 路由：完整 cap.id 命中 > provider 前缀命中 > default。
5. _sanitize：脱敏 headers 中的敏感 key。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

from ctx_weft.protocols.capability import AuthorizationDecision, Authorizer
from ctx_weft.providers.authorizer import AllowAllAuthorizer, AllowListAuthorizer
from ctx_weft.providers.events import InProcessEventBus
from ctx_weft.core.loop.capability_gateway import CapabilityGateway, _sanitize
from ctx_weft.core.loop.driver import LoopContext, LoopState
from ctx_weft.core.capabilities.cache import CapabilityCache
from ctx_weft.protocols import MemoryAddress, ProviderContext
from ctx_weft.protocols.capability import (
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapability,
    ToolCapabilityProvider,
)
from ctx_weft.providers.memory.in_memory import InMemoryMemoryProvider

# ── Helpers ─────────────────────────────────────────────────────────────────────


def _agent(template_id: str = "tmpl_a", session_id: str = "s1"):
    return SimpleNamespace(id="agt_1", template_id=template_id, session_id=session_id)


def _task():
    return SimpleNamespace(id="tsk_1")


def _cap(cid: str = "test:echo", name: str = "echo") -> ToolCapability:
    return ToolCapability(id=cid, name=name, description="Echo back the input.")


def _ctx(template_id: str = "tmpl_a") -> ProviderContext:
    return ProviderContext(
        session_id="s1", tenant_id="default", agent_id="agt_1", agent_template_id=template_id,
    )


# ── 1. AllowAll / 2. AllowList ────────────────────────────────────────────────────


async def _allowed_ids(auth, caps, ctx) -> set[str]:
    """filter 删除后的等价物：逐个 authorize，收集放行的 id。"""
    return {c.id for c in caps if (await auth.authorize(c, ctx)).allowed}


async def test_allow_all_passes_everything() -> None:
    caps = [_cap("a:x", "x"), _cap("b:y", "y")]
    assert await _allowed_ids(AllowAllAuthorizer(), caps, _ctx()) == {"a:x", "b:y"}


async def test_allow_list_whitelist() -> None:
    auth = AllowListAuthorizer(allow_map={"tmpl_a": {"a:x"}})
    caps = [_cap("a:x", "x"), _cap("b:y", "y")]
    assert await _allowed_ids(auth, caps, _ctx()) == {"a:x"}


async def test_allow_list_denylist_wins() -> None:
    auth = AllowListAuthorizer(deny_map={"tmpl_a": {"b:y"}})
    caps = [_cap("a:x", "x"), _cap("b:y", "y")]
    assert await _allowed_ids(auth, caps, _ctx()) == {"a:x"}


async def test_allow_list_unknown_template_falls_back_to_allow() -> None:
    auth = AllowListAuthorizer(allow_map={"other": {"a:x"}})
    caps = [_cap("a:x", "x"), _cap("b:y", "y")]
    # tmpl_a 不在 allow_map → 不限制
    assert await _allowed_ids(auth, caps, _ctx("tmpl_a")) == {"a:x", "b:y"}


async def test_allow_list_empty_set_blocks_all() -> None:
    auth = AllowListAuthorizer(allow_map={"tmpl_a": set()})
    assert await _allowed_ids(auth, [_cap("a:x", "x")], _ctx()) == set()


async def test_authorize_returns_decision() -> None:
    d = await AllowAllAuthorizer().authorize(_cap(), _ctx())
    assert isinstance(d, AuthorizationDecision) and d.allowed is True


async def test_allow_list_deny_carries_message() -> None:
    auth = AllowListAuthorizer(allow_map={"tmpl_a": set()}, deny_message="blocked by policy")
    d = await auth.authorize(_cap("a:x", "x"), _ctx())
    assert d.allowed is False and d.message == "blocked by policy"


# ── 3. Gateway 鉴权集成 ───────────────────────────────────────────────────────────


class _EchoProvider(ToolCapabilityProvider):
    name = "test"

    def __init__(self) -> None:
        self.invoked = False

    async def list(self, ctx):
        return [_cap()]

    async def retrieve(self, ctx):
        return [_cap()]

    async def describe(self, ctx):
        return CapabilityProviderInfo(name=self.name, capability_count=1)

    def invoke(self, capability_id, arguments, ctx) -> AsyncIterator[CapabilityEvent]:
        return self._run(arguments)

    async def _run(self, arguments) -> AsyncIterator[CapabilityEvent]:
        self.invoked = True
        yield CapabilityEvent(kind="result", payload={"content": f"echoed: {arguments.get('text', '')}"})

    async def cancel(self, invocation_id, ctx) -> None:
        return None


def _state_ctx():
    agent = _agent()
    session = SimpleNamespace(id="s1", tenant_id="default")
    task = _task()
    scope = MemoryAddress(session_id="s1", task_id="tsk_1", agent_id="agt_1")
    state = LoopState(run_id="run_1", session=session, task=task, agent=agent, scope=scope, resolved_model=SimpleNamespace(model="mock", account=""))
    ctx = LoopContext(
        assembler=None, llm=None, memory=InMemoryMemoryProvider(),
        event_bus=InProcessEventBus(),
        provider_ctx=ProviderContext(
            session_id="s1", tenant_id="default", task_id="tsk_1", agent_id="agt_1",
            agent_template_id="tmpl_a",
        ),
    )
    return state, ctx


def _gateway(provider: _EchoProvider, authorizers=None) -> tuple[CapabilityGateway, CapabilityCache]:
    cache = CapabilityCache()
    cache.put("agt_1", [_cap()])
    gw = CapabilityGateway(
        capability_cache=cache,
        capability_providers=[provider],
        memory=InMemoryMemoryProvider(),
        event_bus=InProcessEventBus(),
        provider_authorizers=authorizers,
    )
    return gw, cache


async def test_gateway_allows_and_invokes_by_default() -> None:
    provider = _EchoProvider()
    gw, _ = _gateway(provider)
    state, ctx = _state_ctx()
    res = await gw.invoke("test__echo", {"text": "hi"}, state, ctx)
    assert provider.invoked is True
    assert res.is_error is False
    assert res.content == "echoed: hi"


async def test_gateway_blocks_and_skips_provider() -> None:
    provider = _EchoProvider()
    gw, _ = _gateway(provider, authorizers={"test:echo": AllowListAuthorizer(allow_map={"tmpl_a": set()})})
    state, ctx = _state_ctx()
    res = await gw.invoke("test__echo", {"text": "hi"}, state, ctx)
    assert res.is_error is True
    assert "not authorized" in res.content
    assert provider.invoked is False  # 被拦截 → provider 完全没被调用


async def test_gateway_unknown_tool_errors() -> None:
    provider = _EchoProvider()
    gw, _ = _gateway(provider)
    state, ctx = _state_ctx()
    res = await gw.invoke("nonexistent", {}, state, ctx)
    assert res.is_error is True
    assert "unknown tool" in res.content
    assert provider.invoked is False


class _ModifyAuthorizer(Authorizer):
    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=True, message="be careful", modified_arguments={"text": "override"})


class _DenyMsgAuthorizer(Authorizer):
    async def authorize(self, capability, ctx, arguments=None, *, tool_call_id="") -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, message="not in this context")


async def test_gateway_applies_modified_arguments_and_note() -> None:
    provider = _EchoProvider()
    gw, _ = _gateway(provider, authorizers={"test:echo": _ModifyAuthorizer()})
    state, ctx = _state_ctx()
    res = await gw.invoke("test__echo", {"text": "hi"}, state, ctx)
    assert provider.invoked is True
    assert "echoed: override" in res.content   # 用了改写参数，而非原 "hi"
    assert "[Human note: be careful]" in res.content


async def test_gateway_deny_feeds_message_back() -> None:
    provider = _EchoProvider()
    gw, _ = _gateway(provider, authorizers={"test:echo": _DenyMsgAuthorizer()})
    state, ctx = _state_ctx()
    res = await gw.invoke("test__echo", {"text": "hi"}, state, ctx)
    assert res.is_error is True
    assert "not in this context" in res.content   # 拒绝指导回灌
    assert provider.invoked is False


# ── 4. _get_authorizer 路由 ───────────────────────────────────────────────────────


async def test_get_authorizer_resolution_order() -> None:
    exact = AllowAllAuthorizer()
    prefix = AllowAllAuthorizer()
    default = AllowAllAuthorizer()
    gw = CapabilityGateway(
        capability_cache=CapabilityCache(),
        capability_providers=[],
        memory=InMemoryMemoryProvider(),
        event_bus=InProcessEventBus(),
        provider_authorizers={"test:echo": exact, "test": prefix},
        default_authorizer=default,
    )
    assert gw._get_authorizer("test:echo") is exact        # 完整 id 优先
    assert gw._get_authorizer("test:other") is prefix      # 回落到 provider 前缀
    assert gw._get_authorizer("foo:bar") is default        # 都不命中 → default


# ── 5. _sanitize 脱敏 ─────────────────────────────────────────────────────────────


def test_sanitize_redacts_sensitive_headers() -> None:
    args = {"url": "https://x", "headers": {"Authorization": "Bearer secret", "X-Foo": "ok"}}
    out = _sanitize(args)
    assert out["headers"]["Authorization"] == "***"
    assert out["headers"]["X-Foo"] == "ok"
    assert args["headers"]["Authorization"] == "Bearer secret"  # 原 dict 不被改动


def test_sanitize_passes_through_without_headers() -> None:
    args = {"command": "ls"}
    assert _sanitize(args) == {"command": "ls"}


# ── 6. gateway 必须传 ProviderContext（不是 LoopContext）给 authorizer ──────────────


async def test_gateway_passes_provider_context_not_loop_context() -> None:
    """gateway 必须把 ProviderContext（而非 LoopContext）交给 authorizer。

    回归 latent bug：capability_gateway 曾把 LoopContext 传给形参 ctx: ProviderContext。
    三个内置实现都不读 ctx 所以没爆，但 host 自写 authorizer 读 ctx.session_id 会 AttributeError。
    """
    seen: dict = {}

    class _CtxSpy(Authorizer):
        async def authorize(self, capability, ctx, arguments=None, *, tool_call_id=""):
            seen["ctx"] = ctx
            return AuthorizationDecision(allowed=True)

    provider = _EchoProvider()
    gw, _ = _gateway(provider, authorizers={"test:echo": _CtxSpy()})
    state, ctx = _state_ctx()
    await gw.invoke("test__echo", {"text": "hi"}, state, ctx)

    got = seen["ctx"]
    assert isinstance(got, ProviderContext)
    assert got.session_id == "s1"
    assert got.agent_template_id == "tmpl_a"


def test_authorizer_contract_lives_in_protocols() -> None:
    from ctx_weft.protocols import AuthorizationDecision as PD
    from ctx_weft.protocols import Authorizer as PA
    from ctx_weft.protocols.capability import AuthorizationDecision as CD
    from ctx_weft.protocols.capability import Authorizer as CA

    assert PA is CA and PD is CD


def test_protocols_capability_has_no_core_dependency() -> None:
    """契约层不得反向依赖 core（含 TYPE_CHECKING、含相对导入）。"""
    import ast
    import importlib.util
    import pathlib

    import ctx_weft.protocols.capability as m

    def _is_core(name: str) -> bool:
        return name == "ctx_weft.core" or name.startswith("ctx_weft.core.")

    src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=m.__file__)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_core(alias.name):
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                # 相对导入（如 `from ..core import x`）按 __package__ 解析成绝对路径再判定。
                dotted = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(dotted, m.__package__)
            if _is_core(base):
                offenders.append(base)
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                if _is_core(target):
                    offenders.append(target)

    assert offenders == [], f"protocols 反向依赖 core: {offenders}"


def test_builtin_authorizers_live_in_providers() -> None:
    from ctx_weft.providers.authorizer import (
        AllowAllAuthorizer,
        AllowListAuthorizer,
        HumanConfirmationAuthorizer,
    )

    assert issubclass(AllowAllAuthorizer, Authorizer)
    assert issubclass(AllowListAuthorizer, Authorizer)
    assert issubclass(HumanConfirmationAuthorizer, Authorizer)


def test_core_auth_package_is_gone() -> None:
    """不留 re-export shim：旧路径必须彻底消失。"""
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ctx_weft.core.auth")
