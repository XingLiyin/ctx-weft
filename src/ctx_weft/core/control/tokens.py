"""CancelToken / PauseToken.

Phase 6 §6.1. 用于在 step 边界检查控制信号。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class CancelToken:
    """Cooperative cancellation token (hard cancel)."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise asyncio.CancelledError("CancelToken triggered")


class PauseToken:
    """Cooperative pause token（one-shot：只 pause 不复位，run 结束随 RunTokens 注销）。"""

    def __init__(self) -> None:
        self._paused = asyncio.Event()

    def pause(self) -> None:
        self._paused.set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    async def wait_paused(self) -> None:
        """Resolve once paused (mirror of CancelToken.wait for the soft-stop signal)."""
        await self._paused.wait()


@dataclass
class RunTokens:
    """一次 run（单次任务派发）的控制信号对；生命周期与 run 严格对齐（spec 2026-07-05）。"""

    cancel: CancelToken
    pause: PauseToken
