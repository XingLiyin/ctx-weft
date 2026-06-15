"""CapabilityCache：per-session capability 快照。

实例化时填充（LifecycleManager.instantiate_agent），运行期只读。
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
        """获取 agent 的完整 capability 列表（快照）。"""
        caps = self._store.get(agent_id)
        if caps is None:
            raise KeyError(f"No capability snapshot for agent {agent_id}")
        return caps

    def get_by_name(self, agent_id: str, name: str) -> Capability | None:
        """按裸名查找（skill 加载等内部路径）。"""
        return self._by_name.get((agent_id, name))

    def get_by_qualified_name(self, agent_id: str, qualified_name: str) -> Capability | None:
        """按 LLM 所见的 qualified 名查找（gateway 工具调用路径）。"""
        return self._by_qualified.get((agent_id, qualified_name))

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
