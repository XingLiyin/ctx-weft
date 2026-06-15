"""RuntimeConfig — core 运行期可调旋钮，由 host 在实例化时注入。

默认值 = 历史行为，使直连 core / 测试零改动。host 始终注入实际值。
core 不再自读 os.environ；LLM 凭证/模型的 env 自举（bootstrap_from_env）由 host 子类实现。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    hitl_timeout_sec: int | None = None
    hitl_max_resolved: int = 1000
    task_max_concurrent: int = 4
    task_max_retries: int = 3
    default_token_budget: int = 200_000
    default_task_timeout_ms: int = 60_000
