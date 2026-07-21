"""HeuristicTokenizer：启发式费率 × 伺服校准的默认 Tokenizer 实现。

adapter 组合此组件实现 LLMClient.tokenizer 硬契约（见 protocols.llm.Tokenizer）。
count 返回**已校准**估算——「原始估算 × factor」的内部结构对 core 不可见；observe
收 (已校准估算段, 真实段)，在对数空间伺服修正：factor *= (act/est)^α，均衡点即
「校准后估算 = 真实值」。首样本直接种入（此时 factor=1，act/est 就是原始比值），
避免慢启动。恒 clamp [factor_min, factor_max]：异常样本危害有界。est <
min_sample_tokens 或 act ≤ 0 的样本噪声占主导，跳过。

进程内状态、不持久化（一个会话几轮内收敛）；将来接真实 tokenizer（tiktoken/HF
词表）只需 adapter 内部换实现，协议不动。
"""
from __future__ import annotations

from ctx_weft.core.utils import estimate_tokens


class HeuristicTokenizer:
    def __init__(
        self,
        alpha: float = 0.3,
        min_sample_tokens: int = 512,
        factor_min: float = 0.5,
        factor_max: float = 3.0,
    ) -> None:
        self._alpha = alpha
        self._min_sample_tokens = min_sample_tokens
        self._factor_min = factor_min
        self._factor_max = factor_max
        self._factor = 1.0
        self._seeded = False

    @property
    def factor(self) -> float:
        return self._factor

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, int(estimate_tokens(text) * self._factor))

    def observe(self, estimated: int, actual: int) -> None:
        if estimated < self._min_sample_tokens or actual <= 0:
            return
        ratio = actual / estimated
        if not self._seeded:
            self._factor = ratio
            self._seeded = True
        else:
            self._factor *= ratio ** self._alpha
        self._factor = max(self._factor_min, min(self._factor_max, self._factor))
