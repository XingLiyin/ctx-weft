"""BudgetStrategy：context token 预算裁剪。

详见设计文档 §5.4 / §6.8.7。
"""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from loomex_core.core.errors import ContextOverflowError

if TYPE_CHECKING:
    from loomex_core.core.assembler.assembler import ContextBlock, ContextRequest


@runtime_checkable
class BudgetStrategy(Protocol):
    """token 预算裁剪策略。"""

    @abstractmethod
    async def apply(
        self,
        blocks: list["ContextBlock"],
        token_limit: int,
        request: "ContextRequest",
    ) -> list["ContextBlock"]:
        """根据 token_limit 裁剪 blocks；不够时抛 ContextOverflowError。"""
        ...


class PriorityBudgetStrategy(BudgetStrategy):
    """V1 默认策略：按 priority 升序保留；同 priority 内按 token_estimate 降序裁剪。"""

    async def apply(
        self,
        blocks: list["ContextBlock"],
        token_limit: int,
        request: "ContextRequest",
    ) -> list["ContextBlock"]:
        total = sum(b.token_estimate for b in blocks)
        if total <= token_limit:
            return blocks

        # 排序：priority 升序优先保留；同 priority 内 token 大的先裁
        # 即：要裁掉的目标顺序是 (priority desc, token desc)
        sorted_for_drop = sorted(
            blocks,
            key=lambda b: (-b.priority, -b.token_estimate),
        )

        kept_ids: set[str] = {b.id for b in blocks}
        for blk in sorted_for_drop:
            if total <= token_limit:
                break
            if blk.priority == 0:
                # priority=0 不可裁；其他都裁完仍超限 → overflow
                continue
            kept_ids.discard(blk.id)
            total -= blk.token_estimate

        if total > token_limit:
            raise ContextOverflowError(
                f"Context overflow: total={total} tokens > limit={token_limit}"
            )

        return [b for b in blocks if b.id in kept_ids]
