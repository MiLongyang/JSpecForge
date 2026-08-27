"""LLM 客户端抽象基类 + OpenAI 兼容统一实现。

依据 ``docs/design/LLM调用与厂商管理设计.md`` §二（统一调用方案）+ §五（异常
分类）+ §六（厂商轮询）+ §七（双层并发）+ §八（熔断切换）。

**关键不变量**（违反等于不符合实测）：

- ``stream=True``（design §2.2）
- 浏览器 UA（design §2.3）
- ``response.encoding = 'utf-8'``（gotchas）
- ``use_proxy=True`` 的厂商走 ``llm.proxy_address``
- HTTP 重试在熔断计数之前（design §五 5.1 + Q2 决策）
- 全员死阻塞等待 ≤ ``model_cooldown_seconds``（future_improvements F4-01）

**接口契约**：

- :class:`BaseLLMClient` 只定义 ``chat`` 一个抽象方法，``shutdown`` 默认空实现
- :class:`FakeLLMClient` 也实现 :class:`BaseLLMClient`，是测试基石
- ``LLMSession.complete_via(client)``（在 :mod:`jspecforge.llm.session`）
  与之配合：拼好 messages 调 ``client.chat``，再把 assistant 持久化
"""

from __future__ import annotations

import abc
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

import requests

from jspecforge.config.settings import LLMConfig
from jspecforge.llm.circuit_breaker import CircuitBreaker
from jspecforge.llm.response_parser import (
    Verdict,
    judge_response,
    parse_sse_stream,
)
from jspecforge.llm.retry import (
    ClassifiedError,
    HttpAttemptOutcome,
    RetryClass,
    classify_exception,
    classify_http_status,
    run_with_retry,
)
from jspecforge.llm.vendor_router import ModelChoice, Vendor, VendorRouter
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.client")


# ---------------------------------------------------------------------------
# 结果数据类
# ---------------------------------------------------------------------------


@dataclass
class AttemptRecord:
    """单次 (vendor, model) 尝试的记录。

    与 ``call_meta.attempts[]`` JSON 子字段对齐（数据库表字段设计 §四）。
    """

    vendor_name: str
    model: str
    status: str  # "success" / "upstream_error" / "timeout" / "rate_limit" /
                 # "connection_error" / "ssl_error" / "non_retriable" /
                 # "reasoning_only_no_content" / "empty_response" /
                 # "skipped_cooldown" / "unknown_error" / ...
    error_reason: Optional[str] = None
    circuit_breaker_triggered: bool = False
    action_taken: Optional[str] = None  # 由验证步骤回填

    def to_json(self) -> Dict[str, Any]:
        return {
            "vendor_name": self.vendor_name,
            "model": self.model,
            "status": self.status,
            "error_reason": self.error_reason,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "action_taken": self.action_taken,
        }


@dataclass
class LLMResult:
    """LLM 调用结果。

    序列化到 ``task_llm_messages.call_meta`` JSON 时，调用方按需取字段。
    与数据库表字段设计 §四 ``call_meta`` JSON 结构一致。
    """

    success: bool
    content: str = ""
    reasoning: str = ""
    error: str = ""

    vendor_name: str = ""
    vendor_url: str = ""
    model_used: str = ""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    elapsed_seconds: float = 0.0
    candidate_list: List[str] = field(default_factory=list)
    attempts: List[AttemptRecord] = field(default_factory=list)
    total_circuit_breaker_count: int = 0
    vendor_switched: bool = False

    def to_call_meta(self) -> Dict[str, Any]:
        """转换为 ``task_llm_messages.call_meta`` 期望的 JSON。

        注意：键名 ``input_tokens`` / ``output_tokens`` 与数据库表字段
        设计 §四 示例一致（不是 prompt_tokens / completion_tokens）。
        """
        return {
            "vendor_name": self.vendor_name,
            "vendor_url": self.vendor_url,
            "model_used": self.model_used,
            "input_tokens": self.prompt_tokens,
            "output_tokens": self.completion_tokens,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "candidate_list": list(self.candidate_list),
            "attempts": [a.to_json() for a in self.attempts],
            "total_circuit_breaker_count": self.total_circuit_breaker_count,
            "vendor_switched": self.vendor_switched,
        }


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BaseLLMClient(abc.ABC):
    """LLM 客户端抽象基类。

    实现类只需实现 :meth:`chat` 一个方法。``LLMSession.complete_via`` 与
    ``FakeLLMClient`` 都通过这个接口工作。
    """

    @abc.abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        log_context: Optional[Mapping[str, Any]] = None,
    ) -> LLMResult:
        """发送 OpenAI 格式 messages 到 LLM 并返回结果。

        Args:
            messages: ``[{"role": "system|user|assistant", "content": "..."}]``。
            log_context: 仅日志用的上下文（如 ``{"task_id": 42, "session_type":
                "task_a", "a_round": 0}``），不影响调用语义。

        Returns:
            :class:`LLMResult`。``success=False`` 时 ``content=""``，调用方
            按 ``error`` 字段判断失败原因。**绝不抛异常给调用方**。
        """

    def shutdown(self) -> None:  # noqa: B027 — intentional empty default
        """优雅关停，拒绝新请求。默认空实现；子类按需覆盖。"""


# ---------------------------------------------------------------------------
# 双层并发信号量持有者
# ---------------------------------------------------------------------------


@dataclass
class _Semaphores:
    """全局 + 每厂商的双层 :class:`threading.Semaphore`。"""

    global_sem: threading.Semaphore
    per_vendor: Dict[str, threading.Semaphore] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        max_global: int,
        max_per_vendor: int,
        vendor_names: List[str],
    ) -> "_Semaphores":
        return cls(
            global_sem=threading.Semaphore(max_global),
            per_vendor={
                v: threading.Semaphore(max_per_vendor) for v in vendor_names
            },
        )

    @contextmanager
    def acquire(self, vendor_name: str) -> Iterator[None]:
        """先 global 再 per-vendor，先 vendor 释放再 global 释放。"""
        per = self.per_vendor.get(vendor_name)
        # 没注册的厂商（罕见，理论不会发生）兜底创建一个不限并发的虚拟槽
        if per is None:
            self.per_vendor[vendor_name] = threading.Semaphore(1024)
            per = self.per_vendor[vendor_name]
        self.global_sem.acquire()
        try:
            per.acquire()
            try:
                yield
            finally:
                per.release()
        finally:
            self.global_sem.release()


# ---------------------------------------------------------------------------
# LLM 调用 JSONL 日志记录器
# ---------------------------------------------------------------------------


class LLMCallLogger:
    """线程安全的 LLM 调用 JSONL 记录器。

    每次启动生成一个文件：``{log_dir}/llm_calls_{timestamp}.jsonl``。
    """

    def __init__(self, log_dir: str) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(log_dir, exist_ok=True)
        self._path = os.path.join(log_dir, f"llm_calls_{ts}.jsonl")
        self._lock = threading.Lock()
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def log(
        self,
        *,
        log_context: Optional[Mapping[str, Any]],
        vendor: str,
        model: str,
        elapsed_ms: int,
        success: bool,
        error_detail: str = "",
    ) -> None:
        ctx = log_context or {}
        record = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
            "task_id": ctx.get("task_id"),
            "a_round": ctx.get("a_round"),
            "session_type": ctx.get("session_type"),
            "vendor": vendor,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "status": "success" if success else "failed",
            "error_detail": error_detail,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._file.write(line + "\n")

    def close(self) -> None:
        with self._lock:
            self._file.close()


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端
# ---------------------------------------------------------------------------


class OpenAICompatClient(BaseLLMClient):
    """OpenAI 兼容 API 的统一实现（覆盖 DeepSeek / NewAPI 等绝大多数厂商）。

    内部装配：vendor_router + circuit_breaker + retry + response_parser +
    双层 :class:`threading.Semaphore`。

    使用方式::

        router = build_router_from_csv(config.llm.vendors_csv)
        breaker = CircuitBreaker(
            cooldown_seconds=config.llm.model_cooldown_seconds,
            trigger_failures=config.llm.cooldown_trigger_failures,
        )
        client = OpenAICompatClient(config=config.llm, router=router, breaker=breaker)
        result = client.chat([{"role": "user", "content": "hi"}])
    """

    def __init__(
        self,
        *,
        config: LLMConfig,
        router: VendorRouter,
        breaker: CircuitBreaker,
        session_factory: Optional[Callable[[], requests.Session]] = None,
        call_logger: Optional[LLMCallLogger] = None,
    ) -> None:
        self._config = config
        self._router = router
        self._breaker = breaker
        self._call_logger = call_logger
        self._session_factory: Callable[[], requests.Session] = (
            session_factory or requests.Session
        )
        self._sems = _Semaphores.create(
            max_global=config.max_concurrent_global,
            max_per_vendor=config.max_concurrent_per_vendor,
            vendor_names=[v.unique_id for v in router.vendors],
        )
        self._shutdown = threading.Event()

        # 启动期校验：use_proxy=true 的 vendor 必须有 llm.proxy_address，否则
        # _do_one_http_attempt 中 ``proxies = None`` 会让请求绕过代理直连，几乎
        # 必然连接失败。这里 WARNING 一次，不 fail-fast（因为非 proxy 的 vendor
        # 仍可工作）。
        proxy_required = [v for v in router.vendors if v.use_proxy]
        if proxy_required and not (config.proxy_address or "").strip():
            _logger.warning(
                "%d 个 vendor 标记 use_proxy=true，但 llm.proxy_address 为空。"
                "它们的请求会**绕过代理直连**，很可能连接失败。"
                "受影响 vendor: %s",
                len(proxy_required),
                [v.unique_id for v in proxy_required],
            )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        log_context: Optional[Mapping[str, Any]] = None,
    ) -> LLMResult:
        if self._shutdown.is_set():
            return LLMResult(success=False, error="client_shutdown")
        if not messages:
            return LLMResult(success=False, error="empty_messages")

        primary = self._router.next_vendor()
        chain = self._router.build_candidate_chain(primary)
        candidate_keys = [c.key for c in chain]
        ctx = self._format_context(log_context, primary)

        result = LLMResult(success=False, candidate_list=candidate_keys)
        start = time.time()

        winner = self._iter_chain(
            chain, messages, ctx, result, log_context=log_context
        )

        # 全员死时阻塞等待
        if winner is None and not self._shutdown.is_set():
            still_blocked = all(
                not self._breaker.is_available(k) for k in candidate_keys
            )
            if still_blocked:
                _logger.warning(
                    "%s all_models_exhausted_pre_wait: %d candidates all in cooldown",
                    ctx,
                    len(candidate_keys),
                )
                woke = self._breaker.wait_for_any_available(
                    candidate_keys,
                    max_wait_seconds=float(self._config.model_cooldown_seconds),
                )
                if woke is None:
                    result.error = "all_models_exhausted"
                    _logger.error(
                        "%s all_models_exhausted: 等待 %ds 后仍无可用模型",
                        ctx,
                        self._config.model_cooldown_seconds,
                    )
                else:
                    # 解封了一个，再尝试一次（仅这一个；不递归避免极端打转）
                    target = next(
                        (c for c in chain if c.key == woke), None
                    )
                    if target is not None:
                        winner = self._iter_chain(
                            [target], messages, ctx, result,
                            log_context=log_context,
                        )

        # 失败兜底原因
        if winner is None and not result.error:
            last = next(
                (a for a in reversed(result.attempts)
                 if a.status != "skipped_cooldown"),
                None,
            )
            result.error = (
                last.error_reason if last and last.error_reason
                else "all_candidates_failed"
            )

        # 写入成功字段
        if winner is not None:
            choice, payload = winner
            result.success = True
            result.vendor_name = choice.vendor.unique_id
            result.vendor_url = choice.vendor.base_url
            result.model_used = choice.model
            result.content = payload.get("content", "")
            result.reasoning = payload.get("reasoning", "")
            result.prompt_tokens = payload.get("prompt_tokens")
            result.completion_tokens = payload.get("completion_tokens")
            result.total_tokens = payload.get("total_tokens")
            result.vendor_switched = choice.vendor.unique_id != primary.unique_id

        result.elapsed_seconds = time.time() - start

        return result

    def shutdown(self) -> None:
        """置位拒绝新请求。已 in-flight 的请求自然完成。"""
        self._shutdown.set()
        _logger.info("OpenAICompatClient: shutdown requested")

    # ------------------------------------------------------------------
    # 链遍历（顺序尝试候选）
    # ------------------------------------------------------------------

    def _iter_chain(
        self,
        chain: List[ModelChoice],
        messages: List[Dict[str, str]],
        ctx: str,
        result: LLMResult,
        log_context: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Tuple[ModelChoice, Dict[str, Any]]]:
        """按顺序尝试 chain 中每个 ModelChoice。

        - 跳过当前不可用的 (cooldown) → 记 ``skipped_cooldown`` 一项
        - 调用 :meth:`_try_one_choice` 完成一次"逻辑调用"（含 HTTP 重试）
        - 把每次 attempt 写入 ``result.attempts``
        - 第一个成功的返回 ``(choice, payload)``；全部失败返回 None
        """
        for choice in chain:
            if self._shutdown.is_set():
                break
            if not self._breaker.is_available(choice.key):
                result.attempts.append(
                    AttemptRecord(
                        vendor_name=choice.vendor.unique_id,
                        model=choice.model,
                        status="skipped_cooldown",
                        error_reason="circuit breaker active",
                    )
                )
                continue

            attempt, payload = self._try_one_choice(
                choice, messages, ctx, log_context=log_context
            )
            if attempt.circuit_breaker_triggered:
                result.total_circuit_breaker_count += 1
            result.attempts.append(attempt)

            if attempt.status == "success" and payload is not None:
                return (choice, payload)
        return None

    # ------------------------------------------------------------------
    # 单 (vendor, model) 调用 + 重试包装
    # ------------------------------------------------------------------

    def _try_one_choice(
        self,
        choice: ModelChoice,
        messages: List[Dict[str, str]],
        ctx: str,
        log_context: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[AttemptRecord, Optional[Dict[str, Any]]]:
        """对一个 (vendor, model) 完成一次"逻辑调用"（含 HTTP 内部重试）。

        Returns:
            ``(record, payload)``；成功时 payload 是含 content/reasoning/tokens
            的 dict，失败时 payload 为 None。
        """
        vendor = choice.vendor
        model = choice.model
        logger = self._call_logger

        def _logged_attempt() -> "HttpAttemptOutcome":
            start = time.time()
            outcome = self._do_one_http_attempt(choice, messages, ctx)
            elapsed_ms = int((time.time() - start) * 1000)
            if logger:
                try:
                    logger.log(
                        log_context=log_context,
                        vendor=vendor.unique_id,
                        model=model,
                        elapsed_ms=elapsed_ms,
                        success=outcome.success,
                        error_detail=(
                            outcome.classified.detail
                            if outcome.classified else ""
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
            return outcome

        with self._sems.acquire(vendor.unique_id):
            outcome = run_with_retry(
                _logged_attempt,
                max_retries=self._config.http_retry_max,
                rate_limit_wait_seconds=float(
                    self._config.rate_limit_wait_seconds
                ),
            )

        if outcome.success and isinstance(outcome.payload, dict):
            self._breaker.mark_success(choice.key)
            record = AttemptRecord(
                vendor_name=vendor.unique_id,
                model=model,
                status="success",
            )
            return record, outcome.payload

        # 失败：登记到熔断器（+1 连续失败计数；可能触发冷却）
        triggered = self._breaker.mark_failure(choice.key)
        status, reason = self._classified_to_status(outcome.classified)
        return (
            AttemptRecord(
                vendor_name=vendor.unique_id,
                model=model,
                status=status,
                error_reason=reason,
                circuit_breaker_triggered=triggered,
            ),
            None,
        )

    def _do_one_http_attempt(
        self,
        choice: ModelChoice,
        messages: List[Dict[str, str]],
        ctx: str,
    ) -> HttpAttemptOutcome:
        """一次完整的 HTTP 请求（含 SSE 解析与判定）。

        **重要**：本函数对网络/HTTP 错误做异常 → :class:`ClassifiedError` 的
        转换，**不**把异常抛回 :func:`run_with_retry`。
        """
        vendor = choice.vendor
        model = choice.model
        url = vendor.chat_completions_url()
        headers = {
            "Authorization": f"Bearer {vendor.api_key}",
            "Content-Type": "application/json",
            "User-Agent": self._config.user_agent,
        }
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": self._config.max_tokens,
            "temperature": self._config.temperature,
        }
        proxies: Optional[Dict[str, str]] = None
        if vendor.use_proxy and self._config.proxy_address:
            proxies = {
                "http": self._config.proxy_address,
                "https": self._config.proxy_address,
            }

        sess = self._session_factory()
        try:
            resp = sess.post(
                url,
                headers=headers,
                json=payload,
                proxies=proxies,
                stream=True,
                timeout=self._config.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — 网络异常一律分类
            classified = classify_exception(exc)
            _logger.info(
                "%s %s/%s 请求异常: %s",
                ctx,
                vendor.unique_id,
                model,
                classified.detail,
            )
            return HttpAttemptOutcome(success=False, classified=classified)

        try:
            if resp.status_code != 200:
                try:
                    body_snippet = resp.text[:500]
                except Exception:  # noqa: BLE001
                    body_snippet = ""
                base = classify_http_status(resp.status_code, resp)
                classified = ClassifiedError(
                    cls=base.cls,
                    status_code=base.status_code,
                    retry_after_seconds=base.retry_after_seconds,
                    detail=f"{base.detail} body={body_snippet!r}",
                )
                _logger.info(
                    "%s %s/%s HTTP 非 200: %d (%s)",
                    ctx,
                    vendor.unique_id,
                    model,
                    resp.status_code,
                    classified.cls.value,
                )
                return HttpAttemptOutcome(success=False, classified=classified)

            # 流式解析
            # 2026-05-02 阶段 B 修复(用户实测 ChunkedEncodingError 反馈):
            # 流式读取中途断连(ChunkedEncodingError / ProtocolError)
            # 必须在这里 catch + classify,否则会冒泡到 BaseProcessor.run
            # 被当成"非 InfraError 非预期异常"丢弃,任务被吞掉不重试。
            try:
                parsed = parse_sse_stream(
                    resp,
                    model_name=model,
                    thinking_keywords=self._config.thinking_model_keywords,
                    thinking_tag=self._config.thinking_tag,
                    thinking_end_tag=self._config.thinking_end_tag,
                )
            except Exception as exc:  # noqa: BLE001 — 流式读取异常一律分类
                classified = classify_exception(exc)
                _logger.info(
                    "%s %s/%s 流式读取异常: %s",
                    ctx,
                    vendor.unique_id,
                    model,
                    classified.detail,
                )
                return HttpAttemptOutcome(success=False, classified=classified)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

        verdict: Verdict = judge_response(parsed)
        if verdict.success:
            return HttpAttemptOutcome(
                success=True,
                payload={
                    "content": parsed.content,
                    "reasoning": parsed.reasoning,
                    "prompt_tokens": parsed.prompt_tokens,
                    "completion_tokens": parsed.completion_tokens,
                    "total_tokens": parsed.total_tokens,
                },
            )

        # 解析层失败：业务层失败不重试（重试也不会解决）
        classified = ClassifiedError(
            cls=RetryClass.NON_RETRIABLE,
            detail=f"{verdict.error_kind}: {verdict.error_detail}",
        )
        return HttpAttemptOutcome(success=False, classified=classified)

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    @staticmethod
    def _format_context(
        log_context: Optional[Mapping[str, Any]],
        primary: Vendor,
    ) -> str:
        if not log_context:
            return f"[primary={primary.unique_id}]"
        parts = [f"{k}={v}" for k, v in log_context.items()]
        return f"[{', '.join(parts)}, primary={primary.unique_id}]"

    @staticmethod
    def _classified_to_status(
        cls: Optional[ClassifiedError],
    ) -> Tuple[str, Optional[str]]:
        if cls is None:
            return "unknown_error", None
        detail = cls.detail or ""
        low = detail.lower()
        if cls.cls is RetryClass.RATE_LIMIT:
            return "rate_limit", detail
        if "timeout" in low:
            return "timeout", detail
        if "ssl" in low:
            return "ssl_error", detail
        if "connection" in low or "proxy" in low:
            return "connection_error", detail
        if "reasoning_only_no_content" in detail:
            return "reasoning_only_no_content", detail
        if "empty_response" in detail:
            return "empty_response", detail
        if "upstream_error" in detail:
            return "upstream_error", detail
        if cls.cls is RetryClass.NON_RETRIABLE:
            return "non_retriable", detail
        return "retriable_other", detail
