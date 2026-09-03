"""LifecycleManager：Agent 实例化 + spawn 深度检查。

Capability 解析已移至 PrepareStep（CapabilityResolver），
此处只负责从 template 创建 Agent 对象。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ctx_weft.core.control.types import AgentView
from ctx_weft.core.errors import CtxWeftError
from ctx_weft.core.orchestrator.template_lookup import TemplateLookup
from ctx_weft.core.state.models import Agent, LoopGuard
from ctx_weft.core.utils import generate_id, now_utc
from ctx_weft.protocols import LoopConfig, MemoryConfig
from ctx_weft.protocols.context import ProviderContext
from ctx_weft.protocols.events import Event, EventBus, EventType
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


class DuplicateAgentId(CtxWeftError):
    """instantiate(agent_id=...) 撞上已登记的 id——编程错误，不是「水合」。

    这个参数的含义是「这个*新* agent 的 id」（今天只有 SessionManager.create_session
    的 root 分支在用：它得先铸好 id 才能把 root_agent_id 塞进 SESSION_CREATED
    payload，而 SESSION_CREATED 必须先于 AgentInstantiated——见该处调用注释）。
    传一个已存在的 id 不是「可能是水合」的旧 existing_agent_id 语义（那个歧义已被
    Task 3 消灭，水合走 materialize()）——撞上就是调用方算错了 id，直接抛错。
    """


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
    event_bus: EventBus
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

    async def load(
        self,
        agent_views: dict[str, AgentView],
        *,
        session_id: str,
        tenant_id: str,
        fallback_template_id: str,
    ) -> int:
        """恢复期喂入：把 reducer 折出的 `AgentView` 逐条装填进 registry。

        「喂进来，不是查回去」——registry 不订阅 reducer、不订阅总线，装填之后
        只读自己内存，绝不回落 scan 事件（与 `rebuild_hitl` 同一条纪律）。

        `AgentView` 只有 id/spawn_depth/parent_agent_id/template_id 四个字段，
        没有 memory_config/loop_config——这里用 `template_lookup` 重新解析出
        真正的 template 配置（取代 `agents_from_projection` 留下的 dataclass
        默认值，那是本 Task 有意修正的行为）。

        `view.template_id` 为空（存量事件流子 agent 没发过 AgentInstantiated）
        → 回落 `fallback_template_id`；模板解析失败 → `logger.warning` 后用
        `MemoryConfig()`/`LoopConfig()` 默认值继续。**绝不抛**：这条路要在
        `recover()` 的每个 session 上都跑通，抛一次就卡住整条恢复链——回落而非
        报错的口径与 `_register_fallback` 一致。
        """
        self.register_session(
            session_id, tenant_id=tenant_id, fallback_template_id=fallback_template_id,
        )
        ctx = ProviderContext(session_id=session_id, tenant_id=tenant_id)
        n = 0
        for av in agent_views.values():
            template_id = av.template_id or fallback_template_id
            try:
                template = await self.template_lookup.get_template(template_id, None, ctx=ctx)
                resolved_template_id = template.id
                memory_config = template.memory_config
                loop_config = template.loop_config
            except Exception:
                logger.warning(
                    "LifecycleManager.load: template %r unresolvable for agent %s; "
                    "using default configs (recovery-time gap, degrading not crashing)",
                    template_id, av.id,
                )
                resolved_template_id = template_id
                memory_config = MemoryConfig()
                loop_config = LoopConfig()
            self._agents[av.id] = _AgentRecord(
                session_id=session_id,
                tenant_id=tenant_id,
                template_id=resolved_template_id,
                parent_agent_id=av.parent_agent_id,
                spawn_depth=av.spawn_depth,
                memory_config=memory_config,
                loop_config=loop_config,
            )
            n += 1
        return n

    async def instantiate(
        self,
        *,
        template_id: str,
        session_id: str,
        tenant_id: str,
        parent_agent_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        template: AgentTemplate | None = None,
        ctx: ProviderContext | None = None,
    ) -> tuple[Agent, AgentTemplate]:
        """真新建：解析 template（或用调用方预解析的），生成新 id（或用调用方预铸的），
        登记 record，发出身事件。

        template_id 须为规范形式 provider:name；裸 id 由 TemplateLookup 抛 TemplateNotFoundError。
        深度超限发 SpawnRejected 并抛 SpawnDepthExceeded。

        ★ 无 existing_agent_id 参数——水合走 materialize()，两件事不再共用一个入口。

        task_id：AgentSpawned / SpawnRejected 的 envelope 需要——两条事件的主语都是
        「围绕这次 spawn 尝试」，task_id 标的是被 spawn 出来要跑的那个子任务。root
        agent 实例化没有 task（session 尚未建 root task），传 None 即可。

        agent_id：调用方预先铸好的新 agent id，省略则内部照旧 generate_id("agt")。
        目前只有 SessionManager.create_session 的 root 分支会传——它得先知道 id
        才能把 root_agent_id 塞进 SESSION_CREATED payload，而 SESSION_CREATED 必须
        先于这里发出的 AgentInstantiated（因果序 Session → Agent → Task）。传入的
        id 若已登记过 → DuplicateAgentId：见该异常 docstring，这不是「水合」。

        template：调用方已经解析过的 template 对象，传了就直接用，不再自己
        `get_template`。目前只有 SessionManager.create_session 会传——它得先解析
        一次校验 template_id（入口即拒、不落库，必须在任何 emit 之前完成），若这
        里再解析第二次，`LocalAgentTemplateProvider` 每次都重新扫盘（docstring
        明写「热更新友好」），两次调用之间存在真实 TOCTOU 窗口：SESSION_CREATED
        已经落库后，第二次解析可能拿到热更新后的不同版本甚至 TemplateNotFoundError，
        击穿「入口即拒、不落库」。传预解析对象消灭这个窗口——全程只解析一次。
        省略则照旧自己解析（子 agent 路径不受影响，它没有相同的两阶段需求）。
        """
        if agent_id is not None and agent_id in self._agents:
            raise DuplicateAgentId(f"agent_id {agent_id} already registered")
        if template is None:
            resolve_ctx = ctx or ProviderContext(session_id=session_id, tenant_id=tenant_id)
            template = await self.template_lookup.get_template(
                template_id, None, ctx=resolve_ctx,
            )

        spawn_depth = 0
        if parent_agent_id is not None:
            # .get() + 回落而非裸下标：父 agent 若因跨重启未被本进程重新登记
            # （旧调用方直接递 Agent 对象、绕过了 registry），裸下标会把一次
            # 「登记缺口」升级成一次崩溃——与 materialize 的既有口径一致。这里手上
            # 就有本次 instantiate 调用自己的 session_id/tenant_id——子 agent 的父
            # 几乎必然在同一 session，传进去比 `_register_fallback` 内部「猜最近一次
            # register_session 的会话」精确，消掉多 session 并发恢复时猜错 tenant
            # 的那类风险（Task 3 评审 deferred minor，Task 5 顺手收紧这一个调用点）。
            parent_rec = self._agents.get(parent_agent_id) or self._register_fallback(
                parent_agent_id, session_id=session_id, tenant_id=tenant_id,
            )
            spawn_depth = parent_rec.spawn_depth + 1
            if spawn_depth > template.loop_config.max_spawn_depth:
                # SpawnRejected 是 AgentSpawned 的另一半：被拒的 spawn 根本不会有
                # agent 诞生，AgentInstantiated 覆盖不到，不发这条则事件流里看不出
                # 「有人想 spawn 但被挡了」。envelope 的 agent_id 填**父**——子 agent
                # 没诞生，没有 id 可填，而这条事件的主语正是发起 spawn 的那个 agent。
                await self.event_bus.emit(Event(
                    id=generate_id("evt"),
                    run_id=None,
                    sequence=0,
                    session_id=session_id,
                    type=EventType.SPAWN_REJECTED,
                    timestamp=now_utc(),
                    tenant_id=tenant_id,
                    task_id=task_id,
                    agent_id=parent_agent_id or None,
                    payload={
                        "reason": "depth_limit",
                        # 恒 False：spawn 被拒后降级为 inline 执行的能力今天不存在，
                        # 如实反映现状而不是留一个骗人的 True。
                        "fallback_to_inline": False,
                        "attempted_subtask_id": task_id,
                    },
                ))
                raise SpawnDepthExceeded(
                    f"Max spawn depth {template.loop_config.max_spawn_depth} exceeded "
                    f"(current: {spawn_depth})"
                )

        agent_id = agent_id or generate_id("agt")
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

        if parent_agent_id is not None:
            # 先记「这次 spawn 被准了」，再记「诞生的 agent 长这样」——读事件流的
            # 因果顺序。AgentSpawned 的主语是**父 agent 的一次 spawn 动作**（与
            # SpawnRejected 配对，构成对每次 spawn 尝试的完整审计）；下面那条的
            # 主语是这个 agent 自己的出身配置。
            await self.event_bus.emit(Event(
                id=generate_id("evt"),
                run_id=None,
                sequence=0,
                session_id=session_id,
                type=EventType.AGENT_SPAWNED,
                timestamp=now_utc(),
                tenant_id=tenant_id,
                task_id=task_id,
                agent_id=agent_id,
                payload={
                    "parent_agent_id": parent_agent_id,
                    "subtask_id": task_id,
                },
            ))
        # 事件流里唯一记录「该 agent 用的哪个模板」的地方——`_rebuild_agents` 从
        # session/task 树推算 AgentView，推得出 parent/depth，推不出模板。root 与
        # 子 agent 现在共用同一条发射路径，不再分落 session_manager 与 runtime 两处。
        await self.event_bus.emit(Event(
            id=generate_id("evt"),
            run_id=None,
            sequence=0,
            session_id=session_id,
            type=EventType.AGENT_INSTANTIATED,
            timestamp=now_utc(),
            tenant_id=tenant_id,
            task_id=task_id,
            agent_id=agent_id,
            payload={"template_id": template.id, "template_version": template.version},
        ))

        return agent, template

    def materialize(
        self, agent_id: str, *, context_limit: int, reserved_output_tokens: int,
    ) -> Agent:
        """水合：按 id 从 record 造一个新的 Agent 对象。零事件，永不抛。

        未登记的 id（恢复期缺口——比如跨重启后本进程的 registry 是空的）走「按
        session 的 fallback_template_id 就地补登记 + WARNING」而不是 KeyError：
        回落而非报错是刻意的，与 `load()` 装填时模板解析失败的口径一致——
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

    def _register_fallback(
        self, agent_id: str, *, session_id: str | None = None, tenant_id: str | None = None,
    ) -> _AgentRecord:
        """未登记 id 的一次性补登记（materialize 的降级路径，instantiate 的父查找也借用它）。

        本方法**不**代表「除 instantiate 外还有人改已存在的 record」——它只在
        record 从未存在过时创建一条，且创建后立刻幂等（第二次直接命中 `_agents.get`，
        不再触发警告），不会覆盖任何已登记的真实状态。

        `session_id`/`tenant_id`：调用方若手上已经有确凿的 session 语境（比如
        instantiate 的父查找——子 agent 的父几乎必然在同一次 instantiate 调用
        的 session 里），传进来直接用，不猜。省略时才回落「最近一次
        register_session 的会话」这个近似——materialize() 签名里没有 session_id
        参数，是那条路径专属的容忍度（恢复期的一次缺口容忍度本就高于严格授权
        路径），不是本方法本身必须用猜的。
        """
        logger.warning(
            "LifecycleManager: unregistered agent %s; "
            "falling back to a session default (recovery-time gap, degrading not crashing)",
            agent_id,
        )
        if session_id is not None and session_id in self._sessions:
            _sid, defaults = session_id, self._sessions[session_id]
        elif session_id is not None:
            _sid = session_id
            defaults = _SessionDefaults(
                tenant_id=tenant_id or "default", fallback_template_id="",
            )
        elif self._sessions:
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
