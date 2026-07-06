"""Per-run token registry：随派发登记、随 run 注销；_pausing 闩锁下出生即 paused。"""

import pytest

from ctx_weft.core import CtxWeftRuntime
from ctx_weft.core.control import RunTokens
from ctx_weft.providers.llm.mock import MockLLMAdapter
from tests.integration.test_minimal_loop import InMemoryTemplateResolver

pytestmark = pytest.mark.asyncio


def _rt():
    return CtxWeftRuntime(llm=MockLLMAdapter(responses=[]),
                          template_resolver=InMemoryTemplateResolver())


async def test_register_and_deregister_run_tokens():
    rt = _rt()
    tokens = rt._register_run_tokens("s1", "t1")
    assert isinstance(tokens, RunTokens)
    assert rt._run_tokens["s1"]["t1"] is tokens
    assert not tokens.pause.is_paused and not tokens.cancel.is_cancelled
    rt._deregister_run_tokens("s1", "t1")
    assert "s1" not in rt._run_tokens          # 空桶随手回收


async def test_born_paused_under_pausing_latch():
    rt = _rt()
    rt._pausing.add("s1")
    tokens = rt._register_run_tokens("s1", "t1")
    assert tokens.pause.is_paused is True


async def test_non_root_run_born_cancel_not_paused_under_pausing_latch():
    # M-1：闩锁窗口内重排出的非 root agent run（多级委派被 _try_resume_parent 唤醒的中间
    # 父任务）须出生即 cancel、绝不 born-pause——检查点 pause 先于 cancel，双信号会让
    # 中间 agent park 出气泡抢走续跑点。cancel 后逐级级联，直到 root agent 的任务 park。
    rt = _rt()
    rt._pausing.add("s1")
    tokens = rt._register_run_tokens("s1", "t1", root_run=False)
    assert tokens.cancel.is_cancelled is True
    assert tokens.pause.is_paused is False
