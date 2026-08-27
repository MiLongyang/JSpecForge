"""LLM 客户端模块。

公开 API（按依赖顺序）：

- :mod:`vendor_router`：vendors.csv 加载 + 跨任务轮询路由
- :mod:`circuit_breaker`：模型级冷却 + 全员死阻塞等待
- :mod:`response_parser`：SSE 流解析 + reasoning 分离 + 成功判定
- :mod:`retry`：HTTP 错误分类 + 指数退避
- :mod:`client`：:class:`BaseLLMClient` 抽象 + :class:`OpenAICompatClient` 实现
- :mod:`session`：:class:`LLMSession` 会话管理 + 持久化（task_a / input_gen 隔离）
- :mod:`fake_client`：:class:`FakeLLMClient` 测试基石

整套依赖图（``A → B`` 表示 A import B）::

    fake_client → client
    session     → client + db.message_dao
    client      → vendor_router + circuit_breaker + response_parser + retry
    response_parser, retry, vendor_router, circuit_breaker → utils.log_utils

**使用边界声明（与 P6/P7 阶段任务书对齐）**：

- P6 处理器**禁止**直接调 :class:`MessageDAO.insert` 写消息，必须经过
  :class:`LLMSession` 的 ``append_*`` / ``record_assistant`` /
  ``complete_via`` 方法。详见 :mod:`session` 文档。
- P7 编排器中断恢复时使用 :meth:`LLMSession.from_db` 重建会话状态。
- P7 编排器**不要**注册关于 LLM 客户端的额外 atexit/signal 钩子；
  ``OpenAICompatClient.shutdown()`` 是显式调用。
"""

from jspecforge.llm.circuit_breaker import CircuitBreaker
from jspecforge.llm.client import (
    AttemptRecord,
    BaseLLMClient,
    LLMResult,
    OpenAICompatClient,
)
from jspecforge.llm.fake_client import FakeCall, FakeLLMClient
from jspecforge.llm.response_parser import (
    ParsedResponse,
    Verdict,
    is_thinking_model,
    judge_response,
    parse_sse_stream,
    split_thinking_tag,
)
from jspecforge.llm.retry import (
    ClassifiedError,
    HttpAttemptOutcome,
    RetryClass,
    classify_exception,
    classify_http_status,
    compute_backoff,
    run_with_retry,
)
from jspecforge.llm.session import LLMSession
from jspecforge.llm.vendor_router import (
    ModelChoice,
    Vendor,
    VendorRouter,
    all_model_keys,
    build_router_from_csv,
    load_vendors_csv,
)

__all__ = [
    # vendor_router
    "Vendor",
    "ModelChoice",
    "VendorRouter",
    "load_vendors_csv",
    "build_router_from_csv",
    "all_model_keys",
    # circuit_breaker
    "CircuitBreaker",
    # response_parser
    "ParsedResponse",
    "Verdict",
    "parse_sse_stream",
    "judge_response",
    "is_thinking_model",
    "split_thinking_tag",
    # retry
    "RetryClass",
    "ClassifiedError",
    "HttpAttemptOutcome",
    "classify_exception",
    "classify_http_status",
    "compute_backoff",
    "run_with_retry",
    # client
    "BaseLLMClient",
    "LLMResult",
    "AttemptRecord",
    "OpenAICompatClient",
    # session
    "LLMSession",
    # fake_client
    "FakeLLMClient",
    "FakeCall",
]
