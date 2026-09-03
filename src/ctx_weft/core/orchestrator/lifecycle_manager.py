"""LifecycleManager：Agent 实例化 + spawn 深度检查。

Capability 解析已移至 PrepareStep（CapabilityResolver），
此处只负责从 template 创建 Agent 对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ctx_weft.core.errors import CtxWeftError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.state.models import Agent, LoopGuard
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import LoopConfig, MemoryConfig
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.template import AgentTemplate

# LoopGuard() 的字段默认值——instantiate() 末尾水合时没有调用方给的真实窗口参数
# 可用（那要到派发时才知道），借用 dataclass 默认值当占位；真正生效的窗口由后续
# materialize() 调用（派发点）按 session/llm 传入覆盖。
_DEFAULT_CONTEXT_LIMIT = LoopGuard().context_limit
_DEFAULT_RESERVED_OUTPUT_TOKENS = LoopGuard().reserved_output_tokens

logger = logging.getLogger(__name__)


class UnknownCapabilityError(CtxWeftError):
    pass


class SpawnDepthExceeded(CtxWeftError):
    pass


@dataclass
class _SessionDefaults:
    tenant_id: str
    fallback_template_id: str


@dataclass
class _AgentRecord:
    session_id: str
    tenant_id: str
    template_id: str
    parent_agent_id: str | None
    spawn_depth: int
    memory_config: MemoryConfig
    loop_config: LoopConfig


@dataclass
class LifecycleManager:
    """Agent 实例化 + 注册表。

    从前是「runtime.py 里 new 五次、用完即弃的无状态 dataclass」，现在是
    runtime 级长生命周期组件，`_agents` 是 agent 身份与配置的唯一住所——
    与 SessionManager 在 2026-09-02 做过的那次晋升同形（docs/events-v2.md §2.1.1）。
    Capability 解析已移至 PrepareStep（CapabilityResolver），此处只负责从
    template 创建 Agent 对象并登记。
    """

    template_lookup: "TemplateLookup"
    _agents: dict[str, _AgentRecord] = field(default_factory=dict)
    _sessions: dict[str, _SessionDefaults] = field(default_factory=dict)

    def register_session(
        self, session_id: str, *, tenant_id: str, fallback_template_id: str,
    ) -> None:
        """纳入管理。已存在则保留原状态（重入安全），与 SessionManager 同口径。"""
        self._sessions.setdefault(
            session_id, _SessionDefaults(tenant_id=tenant_id, fallback_template_id=fallback_template_id),
        )

    def release_session(self, session_id: str) -> None:
        for aid in [k for k, r in self._agents.items() if r.session_id == session_id]:
            self._agents.pop(aid, None)
        self._sessions.pop(session_id, None)

    def has(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def template_id_of(self, agent_id: str) -> str:
        return self._agents[agent_id].template_id

    async def instantiate(
        self,
        *,
        template_id: str,
        session_id: str,
        tenant_id: str,
        parent_agent_id: str | None = None,
        ctx: ProviderContext | None = None,
    ) -> tuple[Agent, AgentTemplate]:
        """真新建：解析 template，生成新 id，登记 record。

        template_id 须为规范形式 provider:name；裸 id 由 TemplateLookup 抛 TemplateNotFoundError。
        深度超限抛 SpawnDepthExceeded。

        ★ 无 existing_agent_id 参数——水合走 materialize()，两件事不再共用一个入口。
        """
        resolve_ctx = ctx or ProviderContext(session_id=session_id, tenant_id=tenant_id)
        template: AgentTemplate = await self.template_lookup.get_template(
            template_id, None, ctx=resolve_ctx,
        )

        spawn_depth = 0
        if parent_agent_id is not None:
            # .get() + 回落而非裸下标：父 agent 若因跨重启未被本进程重新登记
            # （旧调用方直接递 Agent 对象、绕过了 registry），裸下标会把一次
            # 「登记缺口」升级成一次崩溃——与 materialize 的既有口径一致。
            parent_rec = self._agents.get(parent_agent_id) or self._register_fallback(parent_agent_id)
            spawn_depth = parent_rec.spawn_depth + 1
            if spawn_depth > template.loop_config.max_spawn_depth:
                raise SpawnDepthExceeded(
                    f"Max spawn depth {template.loop_config.max_spawn_depth} exceeded "
                    f"(current: {spawn_depth})"
                )

        agent_id = generate_id("agt")
        self._agents[agent_id] = _AgentRecord(
            session_id=session_id,
            tenant_id=tenant_id,
            template_id=template.id,
            parent_agent_id=parent_agent_id,
            spawn_depth=spawn_depth,
            memory_config=template.memory_config,
            loop_config=template.loop_config,
        )

        logger.info(
            "LifecycleManager: instantiated agent %s (template=%s, depth=%d)",
            agent_id, template_id, spawn_depth,
        )
        agent = self.materialize(
            agent_id,
            context_limit=_DEFAULT_CONTEXT_LIMIT,
            reserved_output_tokens=_DEFAULT_RESERVED_OUTPUT_TOKENS,
        )
        return agent, template

    def materialize(
        self, agent_id: str, *, context_limit: int, reserved_output_tokens: int,
    ) -> Agent:
        """水合：按 id 从 record 造一个新的 Agent 对象。零事件，永不抛。

        未登记的 id（恢复期缺口——比如跨重启后本进程的 registry 是空的）走「按
        session 的 fallback_template_id 就地补登记 + WARNING」而不是 KeyError：
        回落而非报错是刻意的，见 `agents_from_projection` docstring 的同一口径——
        授权按模板做策略，重启后把未知模板判成「无权限」会让老会话直接跑不动，
        把恢复期的一个缺口变成崩溃是净损失。

        每次调用产出一个新实例（不是共享引用）：Agent 带一次 run 的可变量
        （loop_guard.context_tokens 由 act.py 改写），派发时各自持有自己的份是对的。
        """
        rec = self._agents.get(agent_id)
        if rec is None:
            rec = self._register_fallback(agent_id)
        return Agent(
            id=agent_id,
            session_id=rec.session_id,
            tenant_id=rec.tenant_id,
            template_id=rec.template_id,
            parent_agent_id=rec.parent_agent_id,
            spawn_depth=rec.spawn_depth,
            memory_config=rec.memory_config,
            loop_config=rec.loop_config,
            loop_guard=LoopGuard(
                context_limit=context_limit,
                reserved_output_tokens=reserved_output_tokens,
            ),
            created_at=now_utc(),
        )

    def _register_fallback(self, agent_id: str) -> _AgentRecord:
        """未登记 id 的一次性补登记（materialize 的降级路径，instantiate 的父查找也借用它）。

        本方法**不**代表「除 instantiate 外还有人改已存在的 record」——它只在
        record 从未存在过时创建一条，且创建后立刻幂等（第二次直接命中 `_agents.get`，
        不再触发警告），不会覆盖任何已登记的真实状态。

        session 语境（tenant/fallback 模板）取「最近一次 register_session 的会话」：
        materialize() 签名里没有 session_id 参数，多会话并发登记时这是有意的近似——
        恢复期的一次缺口容忍度本就高于严格授权路径。
        """
        logger.warning(
            "LifecycleManager: materialize() saw unregistered agent %s; "
            "falling back to a session default (recovery-time gap, degrading not crashing)",
            agent_id,
        )
        if self._sessions:
            _sid, defaults = next(reversed(self._sessions.items()))
        else:
            defaults = _SessionDefaults(tenant_id="default", fallback_template_id="")
            _sid = ""
        rec = _AgentRecord(
            session_id=_sid,
            tenant_id=defaults.tenant_id,
            template_id=defaults.fallback_template_id,
            parent_agent_id=None,
            spawn_depth=0,
            memory_config=MemoryConfig(),
            loop_config=LoopConfig(),
        )
        self._agents[agent_id] = rec
        return rec
