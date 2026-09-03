"""CapabilityCache：per-session capability 快照。

实例化时填充（AgentRegistry.instantiate），运行期只读。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ctx_weft.protocols.capability import Capability, qualify

logger = logging.getLogger(__name__)


class DuplicateCapabilityName(Exception):
    pass


@dataclass
class CapabilityCache:
    """Per-session in-memory capability 快照。

    key: agent_id → list[Capability]
    支持 by-id 和 by-name 查找。
    """

    _store: dict[str, list[Capability]] = field(default_factory=dict)
    _by_name: dict[tuple[str, str], Capability] = field(default_factory=dict)
    _by_qualified: dict[tuple[str, str], Capability] = field(default_factory=dict)
    # session 全局区：内置控制工具——对所有 agent 可见、生命周期为 session、**不随 evict 逐出**。
    # 与 per-agent（_store/_by_*）互补：put 存 per-run 解析的 skill/mcp/agent；register_global 存
    # 控制工具（report_task_outcome / collect_process_report / finish_task …）。
    _global_by_name: dict[str, Capability] = field(default_factory=dict)
    _global_by_qualified: dict[str, Capability] = field(default_factory=dict)

    def register_global(self, capabilities: list[Capability]) -> None:
        """注册 session 全局能力（控制工具）。幂等覆盖；不进 per-agent，故 evict 不逐出。"""
        for cap in capabilities:
            self._global_by_name[cap.name] = cap
            self._global_by_qualified[qualify(cap.id)] = cap

    def put(self, agent_id: str, capabilities: list[Capability]) -> None:
        """存入 agent 的 capability 快照，按 cap.id 去重（qualified 名作 LLM 入口键）。"""
        seen_ids: set[str] = set()
        for cap in capabilities:
            if cap.id in seen_ids:
                raise DuplicateCapabilityName(
                    f"Duplicate capability id '{cap.id}' for agent {agent_id}"
                )
            seen_ids.add(cap.id)

        self._store[agent_id] = list(capabilities)
        for cap in capabilities:
            self._by_name[(agent_id, cap.name)] = cap          # bare: skill lookup / display
            self._by_qualified[(agent_id, qualify(cap.id))] = cap  # qualified: LLM tool calls

    def get(self, agent_id: str) -> list[Capability]:
        """获取 agent 的完整 capability 列表（per-agent 快照 + session 全局控制工具）。

        per-agent 快照缺失（未 put 过，或已被 evict）时不再 raise：退化为「只有全局控制工具」而
        非整体报错——调用方（如 fire-and-forget 的 background observe）常在 evict 之后才真正跑到
        这里，此时仍应能解析全局控制工具（register_global 不随 evict 逐出，见 get_by_qualified_name
        同一注释）。
        """
        caps = self._store.get(agent_id, [])
        if not self._global_by_qualified:
            return list(caps)
        seen = {c.id for c in caps}
        return list(caps) + [c for c in self._global_by_qualified.values() if c.id not in seen]

    def get_by_name(self, agent_id: str, name: str) -> Capability | None:
        """按裸名查找（skill 加载等内部路径）；per-agent miss 回退 session 全局区。"""
        return self._by_name.get((agent_id, name)) or self._global_by_name.get(name)

    def get_by_qualified_name(self, agent_id: str, qualified_name: str) -> Capability | None:
        """按 LLM 所见的 qualified 名查找（gateway 工具调用路径）；per-agent miss 回退 session
        全局区——控制工具非 per-agent 可逐出资源，evict 后仍能解析（修 background observe 竞态）。"""
        return (
            self._by_qualified.get((agent_id, qualified_name))
            or self._global_by_qualified.get(qualified_name)
        )

    def get_by_id(self, agent_id: str, capability_id: str) -> Capability | None:
        """按 id 查找。"""
        for cap in self._store.get(agent_id, []):
            if cap.id == capability_id:
                return cap
        return None

    def evict(self, agent_id: str) -> None:
        """session 结束时清理。"""
        caps = self._store.pop(agent_id, [])
        for cap in caps:
            self._by_name.pop((agent_id, cap.name), None)
            self._by_qualified.pop((agent_id, qualify(cap.id)), None)

    def get_by_kind(self, agent_id: str, kind: str) -> list[Capability]:
        return [c for c in self._store.get(agent_id, []) if c.kind == kind]

    def has_agent(self, agent_id: str) -> bool:
        return agent_id in self._store
