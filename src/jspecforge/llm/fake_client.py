"""伪造 LLM 客户端，整个测试体系的基石。

依据 ``docs/plan/P4-LLM客户端.md`` §3.5：

- 必须实现与 :class:`BaseLLMClient` 完全一致的接口（即只需 ``chat``）
- 必须支持按顺序返回预设回复（多轮对话模拟）
- 必须支持模拟失败（超时、API 错误、速率限制、reasoning-only 等）
- 必须记录完整调用历史（发送了什么 messages、第几次调用、返回了什么）
- 应支持灵活配置方式（回复列表 / 回复生成函数）

**绝不发出真实 HTTP 请求**。P5~P8 所有阶段的测试都依赖本类来模拟 LLM 返回。

Quick reference::

    # 1. 简单字符串列表（按顺序消费）
    fake = FakeLLMClient(responses=["JML 1", "JML 2"])
    fake.chat(messages_a)   # → success, content="JML 1"
    fake.chat(messages_b)   # → success, content="JML 2"
    fake.chat(messages_c)   # → success, content=""（默认兜底）或抛 IndexError

    # 2. 直接给 LLMResult（细粒度控制 token / vendor / failure）
    fake = FakeLLMClient(responses=[
        LLMResult(success=True, content="ok", vendor_name="FakeVendor",
                  model_used="fake-model", prompt_tokens=10, completion_tokens=5),
        FakeLLMClient.timeout_failure(),
        FakeLLMClient.rate_limit_failure(),
    ])

    # 3. 生成函数（动态决定回复）
    def gen(messages, call_index):
        last_user = next(m for m in reversed(messages) if m["role"] == "user")
        return f"回复 #{call_index}：{last_user['content'][:20]}"

    fake = FakeLLMClient(generator=gen)

    # 4. 调用断言
    assert fake.call_count == 3
    assert fake.history[0].messages == messages_a
    assert fake.history[1].result.error.startswith("timeout")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from jspecforge.llm.client import (
    AttemptRecord,
    BaseLLMClient,
    LLMResult,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.fake_client")


# ---------------------------------------------------------------------------
# 调用历史记录
# ---------------------------------------------------------------------------


@dataclass
class FakeCall:
    """单次调用的完整记录。"""

    index: int
    messages: List[Dict[str, str]]
    log_context: Optional[Mapping[str, Any]]
    result: LLMResult


# 类型别名：可以接受的"预设回复"形式
ResponseSpec = Union[
    str,                # 简单字符串 → 包装成 success LLMResult
    LLMResult,          # 直接给完整结果
    Callable[[List[Dict[str, str]], int], Union[str, LLMResult]],
    BaseException,      # 抛异常（虽不推荐，但 BaseLLMClient 契约是不抛）
                        # 这里特别处理：转换为 LLMResult(success=False)
]


# ---------------------------------------------------------------------------
# FakeLLMClient
# ---------------------------------------------------------------------------


class FakeLLMClient(BaseLLMClient):
    """伪造客户端。"""

    def __init__(
        self,
        responses: Optional[Sequence[ResponseSpec]] = None,
        *,
        generator: Optional[
            Callable[[List[Dict[str, str]], int], Union[str, LLMResult]]
        ] = None,
        default_vendor: str = "FakeVendor",
        default_vendor_url: str = "https://fake.example.com",
        default_model: str = "fake-model",
        exhausted_behavior: str = "empty_success",
    ) -> None:
        """初始化。

        Args:
            responses: 预设回复列表，按顺序消费。每项可以是字符串、:class:`LLMResult`、
                生成函数 ``(messages, call_index) -> str | LLMResult`` 或 BaseException。
            generator: 全局生成函数。若提供，``responses`` 耗尽后用它兜底；
                若 ``responses`` 为 None 则全部由 generator 生成。
            default_vendor / default_vendor_url / default_model: 把字符串预设
                包装成 :class:`LLMResult` 时使用的默认 vendor / model 元数据。
            exhausted_behavior: ``responses`` 耗尽且无 generator 时的行为：
                ``"empty_success"`` 返回成功+空 content；``"raise"`` 抛 :class:`IndexError`；
                ``"empty_failure"`` 返回 ``success=False, error="fake_exhausted"``。
        """
        if responses is None and generator is None:
            raise ValueError(
                "FakeLLMClient 需要至少一个 responses 列表或一个 generator"
            )
        self._responses: List[ResponseSpec] = list(responses or [])
        self._generator = generator
        self._default_vendor = default_vendor
        self._default_vendor_url = default_vendor_url
        self._default_model = default_model
        if exhausted_behavior not in ("empty_success", "raise", "empty_failure"):
            raise ValueError(
                f"exhausted_behavior 非法: {exhausted_behavior!r}"
            )
        self._exhausted_behavior = exhausted_behavior

        self._history: List[FakeCall] = []
        self._shutdown = False

    # ------------------------------------------------------------------
    # BaseLLMClient 接口
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        log_context: Optional[Mapping[str, Any]] = None,
    ) -> LLMResult:
        if self._shutdown:
            result = LLMResult(success=False, error="client_shutdown")
            self._record(messages, log_context, result)
            return result

        index = len(self._history)
        spec = self._next_spec(index)
        result = self._materialize(spec, messages, index)
        self._record(messages, log_context, result)
        return result

    def shutdown(self) -> None:
        self._shutdown = True

    # ------------------------------------------------------------------
    # 历史与断言辅助
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self._history)

    @property
    def history(self) -> List[FakeCall]:
        """完整调用历史（深拷贝列表，元素是引用）。"""
        return list(self._history)

    def last_call(self) -> FakeCall:
        if not self._history:
            raise IndexError("FakeLLMClient: 还没有任何调用")
        return self._history[-1]

    def reset(self) -> None:
        """清空历史与 shutdown 状态（保留 responses / generator 配置）。"""
        self._history.clear()
        self._shutdown = False

    # ------------------------------------------------------------------
    # 失败工厂（方便测试构造各种失败场景）
    # ------------------------------------------------------------------

    @staticmethod
    def timeout_failure(
        vendor: str = "FakeVendor", model: str = "fake-model"
    ) -> LLMResult:
        return LLMResult(
            success=False,
            error="timeout",
            vendor_name=vendor,
            model_used=model,
            attempts=[
                AttemptRecord(vendor_name=vendor, model=model, status="timeout"),
            ],
        )

    @staticmethod
    def rate_limit_failure(
        vendor: str = "FakeVendor", model: str = "fake-model"
    ) -> LLMResult:
        return LLMResult(
            success=False,
            error="HTTP 429 (rate limit)",
            vendor_name=vendor,
            model_used=model,
            attempts=[
                AttemptRecord(
                    vendor_name=vendor, model=model, status="rate_limit"
                ),
            ],
        )

    @staticmethod
    def auth_failure(
        vendor: str = "FakeVendor", model: str = "fake-model"
    ) -> LLMResult:
        return LLMResult(
            success=False,
            error="HTTP 401",
            vendor_name=vendor,
            model_used=model,
            attempts=[
                AttemptRecord(
                    vendor_name=vendor, model=model, status="rate_limit",
                    error_reason="HTTP 401 (auth/forbidden)",
                ),
            ],
        )

    @staticmethod
    def reasoning_only_failure(
        vendor: str = "FakeVendor", model: str = "fake-thinking",
        reasoning: str = "(模型只输出了思考)",
    ) -> LLMResult:
        return LLMResult(
            success=False,
            error="reasoning_only_no_content",
            reasoning=reasoning,
            vendor_name=vendor,
            model_used=model,
            attempts=[
                AttemptRecord(
                    vendor_name=vendor,
                    model=model,
                    status="reasoning_only_no_content",
                ),
            ],
        )

    @staticmethod
    def all_models_exhausted(
        candidate_keys: Optional[Sequence[str]] = None,
    ) -> LLMResult:
        return LLMResult(
            success=False,
            error="all_models_exhausted",
            candidate_list=list(candidate_keys or []),
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _next_spec(self, index: int) -> ResponseSpec:
        if index < len(self._responses):
            return self._responses[index]
        if self._generator is not None:
            return self._generator
        # 耗尽
        if self._exhausted_behavior == "raise":
            raise IndexError(
                f"FakeLLMClient 预设回复已耗尽 (call #{index + 1})"
            )
        if self._exhausted_behavior == "empty_failure":
            return LLMResult(success=False, error="fake_exhausted")
        return ""  # empty_success：兜底返回空字符串

    def _materialize(
        self,
        spec: ResponseSpec,
        messages: List[Dict[str, str]],
        index: int,
    ) -> LLMResult:
        # callable / generator
        if callable(spec) and not isinstance(spec, BaseException):
            value = spec(messages, index)
            return self._materialize(value, messages, index)
        if isinstance(spec, LLMResult):
            return spec
        if isinstance(spec, BaseException):
            # 把异常转换成 LLMResult（保持 BaseLLMClient "不抛异常" 的契约）
            return LLMResult(
                success=False,
                error=f"{type(spec).__name__}: {spec}",
                vendor_name=self._default_vendor,
                model_used=self._default_model,
            )
        # 字符串
        if isinstance(spec, str):
            return LLMResult(
                success=True,
                content=spec,
                vendor_name=self._default_vendor,
                vendor_url=self._default_vendor_url,
                model_used=self._default_model,
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                attempts=[
                    AttemptRecord(
                        vendor_name=self._default_vendor,
                        model=self._default_model,
                        status="success",
                    ),
                ],
            )
        raise TypeError(
            f"FakeLLMClient: 不支持的预设回复类型 {type(spec).__name__}"
        )

    def _record(
        self,
        messages: List[Dict[str, str]],
        log_context: Optional[Mapping[str, Any]],
        result: LLMResult,
    ) -> None:
        self._history.append(
            FakeCall(
                index=len(self._history),
                messages=[dict(m) for m in messages],
                log_context=dict(log_context) if log_context else None,
                result=result,
            )
        )
