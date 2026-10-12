from types import SimpleNamespace
import pytest

from ctx_weft.core.assembler.composer import DefaultComposer
from ctx_weft.core.assembler.assembler import ContextBlock
from ctx_weft.core.utils.content import content_to_text

pytestmark = pytest.mark.asyncio


def test_composer_renders_no_progress_from_process_report():
    # 即使 task.process_report 有值，也不再单独渲染 ## Progress So Far（改由段摘要承载）
    task = SimpleNamespace(id="t1", title="标题", description="", user_prompt="做 X",
                           user_prompt_in_memory=False, process_report="陈旧进度",
                           process_report_at=None, outputs=None)
    req = SimpleNamespace(task=task, purpose="act")
    msgs = DefaultComposer()._build_actor_messages([], req)
    joined = "\n".join(content_to_text(m.content) for m in msgs)
    assert "陈旧进度" not in joined
    assert "Progress So Far" not in joined
