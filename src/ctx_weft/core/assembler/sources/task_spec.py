"""Collect the current Task as a raw task_spec block."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.utils.ids import generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class TaskSpecSource:
    """Return the Task object without prompt projection."""

    name = "task_spec"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        task = request.task
        yield ContextBlock(
            id=generate_id("blk"),
            source="task_spec",
            kind="task_spec",
            value=task,
        )
