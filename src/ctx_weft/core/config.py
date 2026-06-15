"""RuntimeConfig — core 运行期可调旋钮，由 host 在实例化时注入。

默认值 = 历史行为，使直连 core / 测试零改动。host 始终注入实际值。
core 不自读 os.environ 配置（env 文件读取全在 host；LLM 账号自举见
host 的 LLMProvider.bootstrap_from_env）。
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
