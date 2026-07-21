"""Token 估算运行时自校准：按模型维护「实际/估算」比值的 EMA。

启发式费率（estimate_tokens）对未知 tokenizer 必有系统性偏差。循环里每轮 usage 都带
真实 prompt_tokens，与发送前挂在请求上的估算段一比即得该模型的真实费率比——用 EMA
平滑后乘回后续估算段，自适配任意 tokenizer，无需知道其词表。

口径（与 gateway.request_prompt_estimate 配合）：
  - 估算段 = 请求估算里"启发式估出来"的部分：增量路径是本轮新增尾段，整份路径是全部。
    真实基线（上一轮 usage 实测）**不**参与比值，也不被 factor 乘。
  - 发送前 request_prompt_estimate 把 (基线, 原始估算段) 记进 request.metadata；
    usage 到达后 observe_request_outcome 用 (真实 prompt − 基线) / 原始估算段 喂 EMA。
  - factor 夹在 [0.5, 3.0]：向上适配低估 tokenizer，向下有限回收高估浪费的窗口
    （安全兜底交给比例 margin）。估算段太小（< min_sample_tokens）时 framing/噪声
    占主导，跳过不更新。

进程内状态、按 model 名分桶；不持久化（一个会话几轮内即收敛）。
"""
from __future__ import annotations

# request.metadata 里的瞬态记录键（request_prompt_estimate 写、observe_request_outcome 读；
# adapter 显式挑字段拼 payload，不会上线）。
PROMPT_EST_BASE_KEY = "prompt_est_base"
PROMPT_EST_RAW_KEY = "prompt_est_raw"


class TokenCalibration:
    """按模型维护 实际/估算 比值 EMA；无样本时 factor=1.0。"""

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
        self._ema: dict[str, float] = {}

    def observe(self, model: str, estimated: int, actual: int) -> None:
        """喂一个 (估算段, 真实段) 样本。样本太小或真实值非正时忽略。"""
        if estimated < self._min_sample_tokens or actual <= 0:
            return
        ratio = actual / estimated
        prev = self._ema.get(model)
        # 首样本直接种入（避免从 1.0 慢启动）；此后按 alpha 平滑
        self._ema[model] = ratio if prev is None else self._alpha * ratio + (1 - self._alpha) * prev

    def factor(self, model: str) -> float:
        ema = self._ema.get(model)
        if ema is None:
            return 1.0
        return max(self._factor_min, min(self._factor_max, ema))

    def reset(self) -> None:
        self._ema.clear()


# 进程级默认实例：request_prompt_estimate / act 循环直接用；测试用 reset_calibration 隔离。
_default = TokenCalibration()


def observe_estimate(model: str, estimated: int, actual: int) -> None:
    _default.observe(model, estimated, actual)


def calibration_factor(model: str) -> float:
    return _default.factor(model)


def reset_calibration() -> None:
    _default.reset()


def observe_request_outcome(request, usage) -> None:
    """usage 到达后回喂 EMA：真实增量 = prompt_tokens − 基线，与原始估算段作比。

    metadata 缺记录（未走 request_prompt_estimate 的请求）或 usage 无 prompt_tokens
    时静默跳过。
    """
    prompt_tokens = getattr(usage, "prompt_tokens", 0)
    if prompt_tokens <= 0:
        return
    md = request.metadata or {}
    raw = md.get(PROMPT_EST_RAW_KEY)
    base = md.get(PROMPT_EST_BASE_KEY)
    if raw is None or base is None:
        return
    observe_estimate(request.model, raw, prompt_tokens - base)
