"""HTTP 错误分类 + 指数退避重试。

依据 ``docs/design/LLM调用与厂商管理设计.md`` §五（5.1 异常分类捕获）+
``docs/reference/future_improvements.md`` F4-02。

**P4 当前策略**（与 P4 启动会话 Q2/Q3 一致）：

- **可重试**：429（限流）/ 401（认证错）/ 403（权限/短时限流）/ 5xx 服务端错 /
  ``requests.Timeout`` / ``requests.SSLError`` / ``requests.ConnectionError``
- **不可重试**：4xx 中除 401/403/429 外（400/404/422 等明显是请求体错误）+
  其他无法识别的异常（兜底为 NON_RETRIABLE，避免无脑重试导致雪崩）
- **限流型**：429 + 403。专门 sleep 一段时间（``rate_limit_wait_seconds``，
  优先用响应头 ``Retry-After``）后重试
- **常规型**：超时 / SSL / 5xx。指数退避（base=1s，cap=10s）

每次"逻辑调用"内最多重试 ``http_retry_max`` 次（design §五 5.1）。
重试全部失败后由调用方 +1 到熔断计数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple

import requests

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.retry")


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


class RetryClass(str, Enum):
    """异常分类。"""

    RATE_LIMIT = "rate_limit"     # 429 / 403：限流型，专用等待
    RETRIABLE = "retriable"       # 超时 / SSL / 连接 / 5xx：指数退避
    NON_RETRIABLE = "non_retriable"  # 4xx（除上述）：直接失败


@dataclass
class ClassifiedError:
    """对已捕获异常的分类结果。"""

    cls: RetryClass
    status_code: Optional[int] = None
    retry_after_seconds: Optional[float] = None
    detail: str = ""

    @property
    def is_retriable(self) -> bool:
        return self.cls in (RetryClass.RATE_LIMIT, RetryClass.RETRIABLE)


def _parse_retry_after(header_value: Optional[str]) -> Optional[float]:
    """解析 ``Retry-After`` 响应头（秒数；HTTP-date 简化处理略过）。"""
    if not header_value:
        return None
    try:
        return max(0.0, float(header_value))
    except (TypeError, ValueError):
        # HTTP-date 格式暂不支持，忽略
        return None


def classify_http_status(
    status_code: int,
    response: Optional[requests.Response] = None,
) -> ClassifiedError:
    """根据 HTTP 状态码分类。仅在已成功收到 response 但状态码 != 2xx 时调用。"""
    retry_after = None
    if response is not None:
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))

    detail = f"HTTP {status_code}"
    if status_code == 429:
        return ClassifiedError(
            cls=RetryClass.RATE_LIMIT,
            status_code=status_code,
            retry_after_seconds=retry_after,
            detail=f"{detail} (rate limit)",
        )
    if status_code in (401, 403):
        # 401/403 在 P4 当前策略中按"短时限流"对待：sleep + 重试
        # 详见 future_improvements.md F4-02
        return ClassifiedError(
            cls=RetryClass.RATE_LIMIT,
            status_code=status_code,
            retry_after_seconds=retry_after,
            detail=f"{detail} (auth/forbidden — treated as transient)",
        )
    if 500 <= status_code < 600:
        return ClassifiedError(
            cls=RetryClass.RETRIABLE,
            status_code=status_code,
            detail=f"{detail} (server error)",
        )
    return ClassifiedError(
        cls=RetryClass.NON_RETRIABLE,
        status_code=status_code,
        detail=f"{detail} (client error)",
    )


def classify_exception(exc: BaseException) -> ClassifiedError:
    """对捕获到的异常做分类。

    network / timeout / ssl / chunked-encoding 全部归为
    :data:`RetryClass.RETRIABLE`;其余归为 :data:`RetryClass.NON_RETRIABLE`。

    HTTP 状态码相关错误请用 :func:`classify_http_status` 处理(在
    ``response.raise_for_status()`` 抛 ``HTTPError`` 前)。
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return ClassifiedError(cls=RetryClass.RETRIABLE, detail=f"timeout: {exc}")
    if isinstance(exc, requests.exceptions.SSLError):
        return ClassifiedError(cls=RetryClass.RETRIABLE, detail=f"ssl_error: {exc}")
    if isinstance(exc, requests.exceptions.ProxyError):
        return ClassifiedError(cls=RetryClass.RETRIABLE, detail=f"proxy_error: {exc}")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ClassifiedError(
            cls=RetryClass.RETRIABLE, detail=f"connection_error: {exc}"
        )
    # 2026-05-02 阶段 B(用户实测反馈): SSE 流式读取中途连接断开
    # 抛 ChunkedEncodingError(``Response ended prematurely``)。
    # 是 RequestException 子类但不是 ConnectionError 子类,显式归 RETRIABLE
    # 让日志更明确(否则会落到下方 RequestException fallback)。
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return ClassifiedError(
            cls=RetryClass.RETRIABLE,
            detail=f"chunked_encoding_error (stream interrupted): {exc}",
        )
    if isinstance(exc, requests.exceptions.HTTPError):
        # 已经走过 raise_for_status,理论由 classify_http_status 先处理
        resp = getattr(exc, "response", None)
        if resp is not None and resp.status_code is not None:
            return classify_http_status(resp.status_code, resp)
        return ClassifiedError(cls=RetryClass.NON_RETRIABLE, detail=f"http_error: {exc}")
    if isinstance(exc, requests.RequestException):
        # 未细分的 requests 异常族,保守归为可重试
        return ClassifiedError(cls=RetryClass.RETRIABLE, detail=f"request_error: {exc}")
    # 其他异常一律不可重试(包括我们自己抛的 ValueError 等)
    return ClassifiedError(
        cls=RetryClass.NON_RETRIABLE, detail=f"{type(exc).__name__}: {exc}"
    )


# ---------------------------------------------------------------------------
# 退避计算
# ---------------------------------------------------------------------------


def compute_backoff(
    classified: ClassifiedError,
    attempt_index: int,
    *,
    rate_limit_wait_seconds: float,
    base: float = 1.0,
    cap: float = 10.0,
) -> float:
    """计算下一次重试前的等待时长（秒）。

    Args:
        classified: 错误分类结果。
        attempt_index: 当前已失败的次数（0-based；第一次失败传 0 算第二次重试）。
        rate_limit_wait_seconds: ``llm.rate_limit_wait_seconds`` 配置。
        base: 指数退避基数（秒）。
        cap: 单次退避上限（秒）。

    Returns:
        建议等待秒数。
    """
    if classified.cls is RetryClass.RATE_LIMIT:
        # 优先用 Retry-After，否则用配置默认
        if classified.retry_after_seconds is not None:
            return max(0.0, classified.retry_after_seconds)
        return max(0.0, rate_limit_wait_seconds)
    # RETRIABLE：指数退避 base * 2^attempt_index，上限 cap
    delay = base * (2 ** max(0, attempt_index))
    return min(delay, cap)


# ---------------------------------------------------------------------------
# 重试执行器
# ---------------------------------------------------------------------------


@dataclass
class HttpAttemptOutcome:
    """单次 HTTP 尝试的结果（重试循环用）。"""

    success: bool
    classified: Optional[ClassifiedError] = None
    # 调用者可在 success=True 时返回任意业务对象
    payload: object = None


def run_with_retry(
    do_request: Callable[[], HttpAttemptOutcome],
    *,
    max_retries: int,
    rate_limit_wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpAttemptOutcome:
    """在 ``max_retries`` 预算内反复尝试 ``do_request``。

    ``do_request`` 必须把网络/HTTP 状态码错误已分类好封进
    :class:`HttpAttemptOutcome.classified`，**不要**抛异常给
    ``run_with_retry``——这是为了让调用方对"是否成功"有完全控制。

    Args:
        do_request: 单次尝试的回调。每次调用应自洽地完成一次完整的 HTTP
            请求（含 SSE 流读取），并返回 :class:`HttpAttemptOutcome`。
        max_retries: 重试次数预算（不含首次）。``http_retry_max=2`` →
            最多 3 次尝试。
        rate_limit_wait_seconds: 限流型错误的等待时长。
        sleep: 测试时可注入虚拟睡眠。

    Returns:
        成功时返回 ``success=True`` 的 outcome；
        所有重试耗尽后返回最后一次失败的 outcome。
    """
    if max_retries < 0:
        raise ValueError("max_retries 必须 >= 0")

    last: Optional[HttpAttemptOutcome] = None
    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        outcome = do_request()
        last = outcome
        if outcome.success:
            return outcome

        classified = outcome.classified
        if classified is None:
            # 调用方契约错误，保守不重试
            _logger.warning(
                "run_with_retry: 第 %d 次失败但未提供 classified，停止重试",
                attempt + 1,
            )
            return outcome

        if not classified.is_retriable:
            _logger.info(
                "run_with_retry: 第 %d 次失败且不可重试 (%s)，停止",
                attempt + 1,
                classified.detail,
            )
            return outcome

        # 还有重试预算才退避
        if attempt < total_attempts - 1:
            delay = compute_backoff(
                classified,
                attempt_index=attempt,
                rate_limit_wait_seconds=rate_limit_wait_seconds,
            )
            _logger.info(
                "run_with_retry: 第 %d/%d 次失败 (%s)，%.1fs 后重试",
                attempt + 1,
                total_attempts,
                classified.detail,
                delay,
            )
            if delay > 0:
                sleep(delay)
        else:
            _logger.warning(
                "run_with_retry: 第 %d/%d 次失败 (%s)，预算耗尽",
                attempt + 1,
                total_attempts,
                classified.detail,
            )

    assert last is not None  # max_retries+1 >= 1，循环必至少跑一次
    return last
