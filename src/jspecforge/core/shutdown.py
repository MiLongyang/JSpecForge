"""停机基础设施（P7.0）。

设计依据:

- ``docs/design/系统架构与调度设计.md`` §九 优雅停机
- ``docs/plan/P7-编排器.md`` §七 信号钩子归属(保留 P3 链)
- ``docs/plan/P7-编排器.md`` §二 InfraError 处理(全局滑动窗口)
- P7 启动会话用户决议(2026-05-01)
- **2026-05-02 阶段 A 修订**:用户实测 Ctrl+C 5-6 分钟无响应,加强:
    * 第一次 SIGINT 立即打印 ``[SHUTDOWN]`` 反馈到 ``sys.__stderr__``
      (绕过 logging / rich.Live 任何 redirect)
    * 第二次 SIGINT 触发 force 模式(:func:`is_force_shutdown_requested`)
    * worker 在长任务循环里检查 force 事件,可立即放弃当前任务

**3 件事**:

1. ``shutdown_event``: 全局 :class:`threading.Event`,所有 worker 循环每次取
   任务前检查;set 后 worker 立刻退出循环。提供进程级单例 +
   :func:`get_shutdown_event` / :func:`request_shutdown`。
2. SIGINT / SIGTERM / atexit 钩子: P3 已注册黑名单落盘钩子,P7 在其上**叠加**
   set ``shutdown_event``——保留旧 handler 链,不替换。
3. STOP 文件 watcher: 独立后台线程,每 N 秒检查 ``config.paths.stop_file``,
   存在则 set ``shutdown_event``。
4. :class:`InfraErrorTracker`: 滑动窗口全局 InfraError 计数器。worker 抓
   InfraError 后 ``record()``;达到 ``infra_error_global_max`` → 自动
   ``request_shutdown()``。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("core.shutdown")


# ---------------------------------------------------------------------------
# 全局 shutdown_event 单例 + force shutdown
# ---------------------------------------------------------------------------


_shutdown_event: threading.Event = threading.Event()
_force_shutdown_event: threading.Event = threading.Event()
_signal_count: int = 0
_signal_lock: threading.Lock = threading.Lock()


def get_shutdown_event() -> threading.Event:
    """返回全局 shutdown_event。所有 worker 应该 import 这一个实例。"""
    return _shutdown_event


def get_force_shutdown_event() -> threading.Event:
    """返回 force shutdown event(第二次 SIGINT 后 set)。

    worker 在长循环里(如 PBT 1000 次测试)可以检查这个事件以立即放弃任务。
    """
    return _force_shutdown_event


def is_force_shutdown_requested() -> bool:
    """是否已触发强制停机(第二次 Ctrl+C)。"""
    return _force_shutdown_event.is_set()


def request_shutdown(reason: str = "explicit") -> None:
    """请求优雅停机。幂等(``set()`` 多次无副作用)。

    Args:
        reason: 调用者标识(如 ``"sigterm"`` / ``"stop_file"`` /
            ``"infra_global_max"``),仅日志展示用。
    """
    if not _shutdown_event.is_set():
        _logger.warning("shutdown requested: %s", reason)
    _shutdown_event.set()


def request_force_shutdown(reason: str = "explicit-force") -> None:
    """请求强制停机(放弃当前正在跑的任务)。"""
    _force_shutdown_event.set()
    _shutdown_event.set()
    _logger.error("force shutdown requested: %s", reason)


def reset_shutdown_event_for_testing() -> None:
    """**测试用**: 清空 shutdown / force_shutdown / 信号计数。生产代码不要调。"""
    global _signal_count
    _shutdown_event.clear()
    _force_shutdown_event.clear()
    with _signal_lock:
        _signal_count = 0


# ---------------------------------------------------------------------------
# 信号 handler(保留 P3 已注册的钩子链)
# ---------------------------------------------------------------------------


_signal_hooks_registered: bool = False
_old_sigint: Optional[Callable] = None
_old_sigterm: Optional[Callable] = None


def register_signal_handlers() -> None:
    """注册 SIGINT / SIGTERM handler,叠加在 P3 钩子之上。

    流程(用户实测后增强,2026-05-02):

    1. ``signal.signal()`` 返回旧 handler——P3 阶段 ``blacklist_runtime.
       register_shutdown_hooks`` 已经在这里注册过了
    2. **第一次** SIGINT/SIGTERM:
        a. 立即向 ``sys.__stderr__`` 写一行反馈(绕过 logging / rich.Live
           任何 redirect,确保用户立刻看到反馈)
        b. :func:`request_shutdown`(让 worker 退出)
        c. 调用 P3 注册的旧 handler(让黑名单落盘)
    3. **第二次** SIGINT/SIGTERM(用户连摁):
        :func:`request_force_shutdown` —— 让 worker 立即放弃当前任务

    幂等: 重复调用是 no-op。

    **注意**: 必须在主线程调用(Python 限制)。
    """
    global _signal_hooks_registered, _old_sigint, _old_sigterm

    if _signal_hooks_registered:
        return

    _old_sigint = signal.signal(signal.SIGINT, _on_signal)
    try:
        _old_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        # Windows 下 SIGTERM 可能不可用,不强制
        _old_sigterm = None
    _signal_hooks_registered = True
    _logger.info(
        "P7 signal handlers registered (SIGINT/SIGTERM); old handlers preserved"
    )


def _on_signal(signum: int, frame) -> None:  # type: ignore[no-untyped-def]
    """SIGINT / SIGTERM 收到时的 handler。

    第一次:graceful 模式;第二次及以上:force 模式。
    """
    global _signal_count
    with _signal_lock:
        _signal_count += 1
        count = _signal_count

    sig_name = signal.Signals(signum).name if signum else f"signal-{signum}"

    # **关键**: 立即向 sys.__stderr__ 写反馈,绕过 logging / rich.Live 的 redirect
    # __stderr__ 是 Python 进程启动时的原始 stderr,Live 不会动它
    if count == 1:
        try:
            sys.__stderr__.write(
                f"\n[SHUTDOWN] 收到中断信号 ({sig_name}),"
                f"等 worker 完成手头任务...\n"
                f"         (再摁一次 Ctrl+C 可强制立即退出)\n"
            )
            sys.__stderr__.flush()
        except Exception:  # noqa: BLE001
            pass
        request_shutdown(f"signal/{sig_name}")
    else:
        try:
            sys.__stderr__.write(
                f"\n[FORCE-SHUTDOWN] 强制退出 ({sig_name})"
                f"(可能丢失正在处理的任务)\n"
            )
            sys.__stderr__.flush()
        except Exception:  # noqa: BLE001
            pass
        request_force_shutdown(f"signal/{sig_name}-force")

    # 调旧 handler(P3 注册的 _on_signal,负责黑名单落盘)
    old = _old_sigint if signum == signal.SIGINT else _old_sigterm
    if old is None or old in (signal.SIG_IGN, signal.SIG_DFL):
        return
    try:
        old(signum, frame)
    except Exception as exc:  # noqa: BLE001
        _logger.error("旧 signal handler 抛异常: %s", exc)


def _reset_signal_hooks_for_testing() -> None:
    """**测试用**: 清空注册标记 + 旧 handler 引用。生产不要调。"""
    global _signal_hooks_registered, _old_sigint, _old_sigterm
    _signal_hooks_registered = False
    _old_sigint = None
    _old_sigterm = None


# ---------------------------------------------------------------------------
# STOP 文件 watcher
# ---------------------------------------------------------------------------


class StopFileWatcher:
    """STOP 文件检测(独立后台线程)。

    用法::

        watcher = StopFileWatcher(stop_file_path, check_interval=2.0)
        watcher.start()
        # ... 主程序 ...
        watcher.stop()  # 优雅停机时

    检测到 STOP 文件存在 → :func:`request_shutdown` → 线程退出。
    """

    def __init__(
        self,
        stop_file: str,
        *,
        check_interval: float = 2.0,
    ) -> None:
        """构造。

        Args:
            stop_file: STOP 文件绝对路径(``config.paths.stop_file``)。
            check_interval: 检查间隔秒数(默认 2s)。

        Raises:
            ValueError: ``stop_file`` 为空。
        """
        if not stop_file:
            raise ValueError("stop_file 不能为空")
        self._path = Path(stop_file)
        self._interval = check_interval
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    def start(self) -> None:
        """启动后台检查线程。重复调用是 no-op。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        t = threading.Thread(
            target=self._loop,
            name="stop-file-watcher",
            daemon=True,
        )
        self._thread = t
        t.start()
        _logger.info(
            "StopFileWatcher started (path=%s, interval=%.1fs)",
            self._path,
            self._interval,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """通知线程退出并 join(优雅停机时调用)。"""
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _loop(self) -> None:
        """主循环: 每 ``check_interval`` 检查文件存在。"""
        while not self._stopped.is_set() and not _shutdown_event.is_set():
            try:
                if self._path.is_file():
                    _logger.warning(
                        "STOP file detected: %s -> request shutdown",
                        self._path,
                    )
                    request_shutdown("stop_file")
                    break
            except OSError as exc:
                _logger.error("StopFileWatcher I/O 错误: %s", exc)
            self._stopped.wait(self._interval)


# ---------------------------------------------------------------------------
# InfraError 全局滑动窗口计数器
# ---------------------------------------------------------------------------


class InfraErrorTracker:
    """全局 InfraError 滑动窗口计数器。

    设计:

    - 维护一个 ``deque[float]`` 记录每次 InfraError 的发生时间戳
    - ``record(stage, task_id, error_msg)`` 每次:
        1. 把当前时间戳 append
        2. 把窗口外(``now - window_seconds`` 之前)的时间戳全部 popleft
        3. 若 ``len(deque) >= global_max`` → 自动调用 :func:`request_shutdown`
    - ``current_count()`` 给 monitor 看当前窗口内累计

    线程安全: 内部 ``threading.Lock``。
    """

    def __init__(
        self,
        *,
        global_max: int,
        window_seconds: int,
    ) -> None:
        """构造。

        Args:
            global_max: 滑动窗口内累计上限,达上限触发 ``request_shutdown``。
            window_seconds: 窗口长度(秒)。
        """
        if global_max < 1:
            raise ValueError("global_max 必须 >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds 必须 >= 1")
        self._global_max = global_max
        self._window = window_seconds
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()
        # 记录最后一次 record 的元数据,给 monitor / 日志用
        self._last_record: Optional[dict] = None

    @classmethod
    def from_config(cls, config: AppConfig) -> "InfraErrorTracker":
        """从 ``config.retry`` 直接构造。"""
        return cls(
            global_max=config.retry.infra_error_global_max,
            window_seconds=config.retry.infra_error_global_window_seconds,
        )

    def record(
        self,
        *,
        stage: str,
        task_id: int,
        error_msg: str,
    ) -> int:
        """记录一次 InfraError 发生。

        Args:
            stage: 出错阶段(如 ``"oj"`` / ``"rac"``)。
            task_id: 任务 ID。
            error_msg: 截断后的错误描述。

        Returns:
            滑动窗口内当前累计次数(已含本次)。
            **若达到 ``global_max``,自动触发 ``request_shutdown``**。
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            # 滚出窗口外的旧记录
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            self._timestamps.append(now)
            count = len(self._timestamps)
            self._last_record = {
                "stage": stage,
                "task_id": task_id,
                "error_msg": (error_msg or "")[:300],
                "monotonic_ts": now,
            }

        _logger.warning(
            "InfraError recorded: stage=%s task_id=%d (window count=%d/%d)",
            stage,
            task_id,
            count,
            self._global_max,
        )

        if count >= self._global_max:
            _logger.error(
                "InfraError 全局累计 %d 已达上限 %d (窗口 %ds),触发优雅停机",
                count,
                self._global_max,
                self._window,
            )
            request_shutdown(
                f"infra_global_max ({count}/{self._global_max} in {self._window}s)"
            )

        return count

    def current_count(self) -> int:
        """返回滑动窗口内当前累计次数(给 monitor 用)。"""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return len(self._timestamps)

    def last_record(self) -> Optional[dict]:
        """返回最后一次 record 的元数据(给 monitor 用)。"""
        with self._lock:
            return None if self._last_record is None else dict(self._last_record)

    def reset_for_testing(self) -> None:
        """**测试用**: 清空状态。"""
        with self._lock:
            self._timestamps.clear()
            self._last_record = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def is_main_thread() -> bool:
    """返回当前是否在主线程。``signal.signal()`` 必须在主线程调用。"""
    return threading.current_thread() is threading.main_thread()


def in_test_mode() -> bool:
    """检测是否在 pytest 中运行(避免误注册信号 handler)。"""
    return "PYTEST_CURRENT_TEST" in os.environ
