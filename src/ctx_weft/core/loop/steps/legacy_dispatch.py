"""薄转发：本模块已移至 ctx_weft.protocols._legacy_dispatch（v2 P2a，2026-07-27）。

统一归一化模块（protocols.memory_compat.normalize_view）委托新位置做 dispatch 配对；
本转发仅为既有调用点保兼容，P4 日落时随调用点迁移一并删除。
"""

from __future__ import annotations

from ctx_weft.protocols._legacy_dispatch import normalize_legacy_dispatch

__all__ = ["normalize_legacy_dispatch"]
