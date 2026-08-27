"""模型级熔断器（连续失败计数 + 冷却 + 全员死兜底等待）。

依据 ``docs/design/LLM调用与厂商管理设计.md`` §八（熔断与冷却机制）。

核心规则：

- **熔断粒度 = (vendor, model)**，与 ``vendor_router.ModelChoice.key`` 一致
- 连续失败 ``cooldown_trigger_failures`` 次（默认 2）触发冷却
  ``model_cooldown_seconds`` 秒（默认 300）
- 一次成功调用立即把该 (vendor, model) 的连续失败计数重置为 0
- 全员死时阻塞等待，等待上限 = ``model_cooldown_seconds``（兜底，详见
  ``docs/reference/future_improvements.md`` F4-01）

**并发安全**：所有状态修改用 ``threading.RLock`` 保护；阻塞等待用
``threading.Condition`` 唤醒，不轮询。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("llm.circuit_breaker")


@dataclass
class _ModelEntry:
    """单个 (vendor, model) 的熔断状态。"""

    consecutive_failures: int = 0
    blocked_until: float = 0.0  # epoch seconds; 0 = 可用


class CircuitBreaker:
    """模型级熔断器。

    用法示意（在客户端的循环里）::

        breaker = CircuitBreaker(cooldown_seconds=300, trigger_failures=2)
        for choice in chain:
            if not breaker.is_available(choice.key):
                continue
            try:
                ... # 实际调用
            except Exception:
                breaker.mark_failure(choice.key)
                continue
            breaker.mark_success(choice.key)
            break
        else:
            # 整条链都不可用，等待解封
            woke = breaker.wait_for_any_available(
                [c.key for c in chain],
                max_wait_seconds=300,
            )
            if not woke:
                # all_models_exhausted
                ...
    """

    def __init__(
        self,
        cooldown_seconds: int,
        trigger_failures: int,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化熔断器。

        Args:
            cooldown_seconds: 触发冷却后的封锁时间（秒）。
            trigger_failures: 触发冷却所需的连续失败次数。
            clock: 时间源（测试时可注入虚拟时钟）。
        """
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds 不能为负数")
        if trigger_failures < 1:
            raise ValueError("trigger_failures 必须 >= 1")
        self._cooldown = float(cooldown_seconds)
        self._trigger = int(trigger_failures)
        self._clock = clock

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._entries: Dict[str, _ModelEntry] = {}

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_available(self, model_key: str) -> bool:
        """该 (vendor, model) 当前是否可用（不在冷却中）。"""
        with self._lock:
            entry = self._entries.get(model_key)
            if entry is None:
                return True
            if entry.blocked_until == 0.0:
                return True
            if self._clock() >= entry.blocked_until:
                # 自动解封
                entry.blocked_until = 0.0
                entry.consecutive_failures = 0
                self._cond.notify_all()
                return True
            return False

    def blocked_until(self, model_key: str) -> float:
        """返回 (vendor, model) 的解封时间戳；0 表示当前可用。"""
        with self._lock:
            entry = self._entries.get(model_key)
            if entry is None:
                return 0.0
            return entry.blocked_until

    def consecutive_failures(self, model_key: str) -> int:
        """返回 (vendor, model) 的当前连续失败次数。"""
        with self._lock:
            entry = self._entries.get(model_key)
            return 0 if entry is None else entry.consecutive_failures

    # ------------------------------------------------------------------
    # 状态更新
    # ------------------------------------------------------------------

    def mark_failure(self, model_key: str) -> bool:
        """记录一次失败。返回是否本次触发了冷却。

        触发条件：连续失败次数累计达到 ``trigger_failures``。
        """
        triggered = False
        with self._lock:
            entry = self._entries.setdefault(model_key, _ModelEntry())
            entry.consecutive_failures += 1
            if entry.consecutive_failures >= self._trigger:
                entry.blocked_until = self._clock() + self._cooldown
                triggered = True
        if triggered:
            _logger.warning(
                "circuit_breaker: %s 连续失败 %d 次，冷却 %.0f 秒",
                model_key,
                self._trigger,
                self._cooldown,
            )
        return triggered

    def mark_success(self, model_key: str) -> None:
        """记录一次成功。立即重置连续失败计数与冷却状态。"""
        with self._lock:
            entry = self._entries.get(model_key)
            if entry is None:
                return
            entry.consecutive_failures = 0
            if entry.blocked_until != 0.0:
                entry.blocked_until = 0.0
                self._cond.notify_all()

    def reset(self, model_key: Optional[str] = None) -> None:
        """重置 ``model_key`` 或全部条目（仅供测试 / 运维）。"""
        with self._lock:
            if model_key is None:
                self._entries.clear()
            else:
                self._entries.pop(model_key, None)
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # 阻塞等待
    # ------------------------------------------------------------------

    def wait_for_any_available(
        self,
        model_keys: Iterable[str],
        max_wait_seconds: float,
    ) -> Optional[str]:
        """阻塞等待 ``model_keys`` 中任意一个解封。

        Args:
            model_keys: 候选键列表（即一次任务的完整候选链）。
            max_wait_seconds: 等待上限（秒）。**禁止传 None / 负值 /
                ``math.inf``**——这是 future_improvements F4-01 的兜底，
                调用方负责给上限。

        Returns:
            首个解封的键；超过 ``max_wait_seconds`` 仍无解封返回 ``None``。
        """
        if max_wait_seconds < 0:
            raise ValueError("max_wait_seconds 必须 >= 0")
        keys = list(model_keys)
        if not keys:
            return None

        deadline = self._clock() + max_wait_seconds
        with self._cond:
            while True:
                # 1. 找最早能解封的键
                earliest_unblock = float("inf")
                now = self._clock()
                for key in keys:
                    entry = self._entries.get(key)
                    if entry is None or entry.blocked_until == 0.0:
                        return key
                    if now >= entry.blocked_until:
                        # 已自动解封
                        entry.blocked_until = 0.0
                        entry.consecutive_failures = 0
                        return key
                    if entry.blocked_until < earliest_unblock:
                        earliest_unblock = entry.blocked_until

                # 2. 全员被冻；计算等待时长
                if now >= deadline:
                    return None
                wait_until = min(earliest_unblock, deadline)
                wait_seconds = max(0.0, wait_until - now)
                # 3. Condition.wait 在 mark_success / 自动解封 / 超时被唤醒
                self._cond.wait(timeout=wait_seconds)

    # ------------------------------------------------------------------
    # 调试 / 监控
    # ------------------------------------------------------------------

    def snapshot(self) -> List[dict]:
        """返回所有条目的快照（用于监控面板）。"""
        with self._lock:
            now = self._clock()
            return [
                {
                    "key": key,
                    "consecutive_failures": e.consecutive_failures,
                    "blocked": e.blocked_until > now,
                    "blocked_until": e.blocked_until,
                    "remaining_seconds": max(0.0, e.blocked_until - now),
                }
                for key, e in self._entries.items()
            ]
