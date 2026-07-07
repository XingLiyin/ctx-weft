"""GuidanceSource：act 运行时态势 guidance → kind="guidance" block。

文本由 PrepareStep 构建（loop/steps/act_guidance.py：session 任务树 + 已完成
子任务清单 + 指针级收尾提醒）并经 request.extra["act_guidance"] 传入——数据源
是 task_manager，走 extra 传值使装配层不反向依赖 orchestrator（与
skill_instructions → directive 同一模式）。

composer 恒把它拼到**末条 user message 最尾部**（Capabilities 之后，整个 prompt
的收口；仅 act purpose 渲染）。收编进装配管线后，它参与 budget 裁剪与
AssembledPrompt.token_count 记账——此前由 ActStep 事后注入时二者均不可见。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from ctx_weft.core.assembler.priority import slot_priority
from ctx_weft.core.utils import estimate_tokens, generate_id

if TYPE_CHECKING:
    from ctx_weft.core.assembler.assembler import AssemblerDeps, ContextBlock, ContextRequest


class GuidanceSource:
    """把 request.extra["act_guidance"] 包成 guidance block；无文本时不产块。"""

    name = "guidance"

    async def fetch(
        self,
        request: "ContextRequest",
        deps: "AssemblerDeps",
    ) -> AsyncIterator["ContextBlock"]:
        from ctx_weft.core.assembler.assembler import ContextBlock

        text: str = (request.extra or {}).get("act_guidance", "")
        if not text:
            return
        yield ContextBlock(
            id=generate_id("blk"),
            source="guidance",
            kind="guidance",
            target="messages",
            content=text,
            priority=slot_priority("guidance"),
            token_estimate=estimate_tokens(text),
            metadata={},
        )
