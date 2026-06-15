"""TaskSpecSource：当前 task 的描述。

注：Composer 实际上直接从 request.task 取 title/description/user_prompt 渲染——
此 Source 仅占位以保持架构一致；产出一个高优先级 task_spec block 让 budget 不裁掉。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from loomex_core.core.utils import estimate_tokens, generate_id

if TYPE_CHECKING:
    from loomex_core.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class TaskSpecSource:
    """产出 task_spec block（priority=0 不可裁）。"""

    name = "task_spec"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from loomex_core.core.assembler.assembler import ContextBlock

        task = request.task
        parts: list[str] = []
        if task.title:
            parts.append(task.title)
        if task.description:
            parts.append(task.description)
        if task.user_prompt:
            user_prompt = (
                task.user_prompt
                if isinstance(task.user_prompt, str)
                else ""
            )
            parts.append(user_prompt)
        content = "\n".join(parts) if parts else "[No task spec]"

        yield ContextBlock(
            id=generate_id("blk"),
            source="task_spec",
            kind="task_spec",
            target="messages",
            content=content,
            priority=0,  # 不可裁
            token_estimate=estimate_tokens(content),
            metadata={"task_id": task.id},
        )
