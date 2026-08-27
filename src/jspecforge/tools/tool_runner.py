"""外部工具调用通用基类。

封装 ``subprocess.run`` 的统一行为：

- UTF-8 容错解码（``errors='replace'``，避免 Windows GBK 输出炸 stdout 解析）
- 超时强制打断 → ``exit_code=-1`` + ``timed_out=True``（对应 ``HARNESS_TIMEOUT``）
- ``shell=False``、显式 ``timeout``、捕获 ``TimeoutExpired`` /
  ``CalledProcessError``（CLAUDE.md §4.3 红线）
- 环境变量注入 ``JAVA_TOOL_OPTIONS=-Dfile.encoding=UTF-8`` 让 Java 工具按
  UTF-8 输出（gotchas §8.2）
- 日志：INFO 级记命令 + exit_code + 耗时；DEBUG 级记完整 stdout/stderr，
  超长内容截断到 ``MAX_LOG_LINES`` 行

返回 ``ToolResult``（5 字段 dataclass）。所有 Runner 都通过 ``run_subprocess``
间接调用 ``subprocess``，**不要**在 Runner 里直接 ``subprocess.run``。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.tool_runner")

# 日志中 stdout/stderr 的截断行数上限
MAX_LOG_LINES: int = 30

# Python 层超时时使用的退出码标记，对应 docs §3.1 的 HARNESS_TIMEOUT
TIMEOUT_EXIT_CODE: int = -1


@dataclass(frozen=True)
class ToolResult:
    """子进程调用的统一返回结构。

    Attributes:
        exit_code: 子进程退出码；Python 层超时时为 ``TIMEOUT_EXIT_CODE`` (-1)。
        stdout: 标准输出内容（已 UTF-8 解码，errors='replace'）。
        stderr: 标准错误内容（已 UTF-8 解码，errors='replace'）。
        elapsed: 实际耗时（秒），用于 ``classify_oj_result`` 的耗时兜底。
        timed_out: 是否因 Python 层超时被强制打断。
    """

    exit_code: int
    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool


def _safe_decode(data: object) -> str:
    """容错解码 stdout/stderr。

    Args:
        data: 可能是 ``None`` / ``bytes`` / ``str``。

    Returns:
        解码后的字符串;``None`` 返回空串。
    """
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _looks_like_replacement_garbage(text: str, threshold: float = 0.05) -> bool:
    """检测字符串是否含大量 Unicode 替换字符 ``\ufffd``。

    2026-05-02 阶段 D.0:
    Windows 中文系统下某些 Java 工具(尤其是 JDK 11 javac、Major)不响应
    ``-J-Dstdout.encoding=UTF-8`` 之类 JVM flag,实际 stderr 是 GBK 字节流。
    被 ``encoding='utf-8' + errors='replace'`` 解码后会变成 ``����: ...`` 这种
    全替换字符的乱码。检测到则触发二次解码尝试。

    阈值 0.05 = 替换字符占比 ≥ 5% 视为乱码。
    """
    if not text:
        return False
    repl_count = text.count("\ufffd")
    if repl_count == 0:
        return False
    return (repl_count / len(text)) >= threshold


def _smart_decode_stderr(raw_text: str, raw_bytes: Optional[bytes]) -> str:
    """智能 stderr 解码:UTF-8 失败时 fallback 到 GBK / CP936。

    本函数在 :func:`_safe_decode` 已经用 UTF-8+replace 解出 ``raw_text`` 之后,
    检测是否含大量替换字符;是 → 用原始 bytes 重新尝试 GBK 解码,如果 GBK
    解出的结果**不含或极少替换字符**,认为它才是正确编码,返回 GBK 解码结果。

    Args:
        raw_text: 已被 UTF-8+replace 解码的 stderr 字符串。
        raw_bytes: 原始字节(若 ``subprocess.run(text=True)`` 直接拿不到,
            传 ``None`` 则跳过 fallback,直接返回 raw_text)。

    Returns:
        最佳解码结果。
    """
    if not _looks_like_replacement_garbage(raw_text):
        return raw_text
    if raw_bytes is None:
        # 没原始字节就只能 prepend 一行警告
        return raw_text
    # 尝试 GBK / CP936
    for encoding in ("gbk", "cp936", "gb18030"):
        try:
            decoded = raw_bytes.decode(encoding, errors="replace")
            if not _looks_like_replacement_garbage(decoded, threshold=0.05):
                _logger.info(
                    "stderr fallback decode: utf-8 失败,用 %s 解码成功", encoding
                )
                return decoded
        except Exception:  # noqa: BLE001
            continue
    return raw_text


def _truncate_for_log(text: str, max_lines: int = MAX_LOG_LINES) -> str:
    """将多行文本截断到指定行数，超出部分以省略提示替代。"""
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = "\n".join(lines[:max_lines])
    return f"{head}\n... ({len(lines) - max_lines} more lines truncated)"


def _build_env(extra_env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """构建子进程环境变量。

    默认在已有 ``JAVA_TOOL_OPTIONS`` 后追加 ``-Dfile.encoding=UTF-8``，
    避免 Windows 中文系统下 Java 工具输出 GBK 字节流。

    Args:
        extra_env: 额外的环境变量覆盖，优先级最高。

    Returns:
        新的环境变量字典（不修改 ``os.environ``）。
    """
    env = dict(os.environ)
    existing_jto = env.get("JAVA_TOOL_OPTIONS", "").strip()
    flag = "-Dfile.encoding=UTF-8"
    if flag not in existing_jto:
        env["JAVA_TOOL_OPTIONS"] = (
            f"{existing_jto} {flag}".strip() if existing_jto else flag
        )
    if extra_env:
        env.update(extra_env)
    return env


def run_subprocess(
    cmd: Sequence[str],
    timeout: Optional[float],
    *,
    cwd: Optional[str] = None,
    extra_env: Optional[Mapping[str, str]] = None,
    log_label: str = "subprocess",
) -> ToolResult:
    """统一的子进程调用入口。

    Args:
        cmd: 命令列表（``shell=False``，**不接受**字符串形式）。
        timeout: Python 层超时秒数；``None`` 表示不设超时（**慎用**，仅
            CLAUDE.md §4.3 明确允许的场景下才能传 ``None``）。
        cwd: 子进程工作目录，``None`` 表示继承当前进程。
        extra_env: 额外的环境变量。
        log_label: 日志中的工具标识（如 ``"javac"`` / ``"openjml-esc"``）。

    Returns:
        ``ToolResult`` 实例。

    Raises:
        FileNotFoundError: 命令可执行文件不存在（基础设施异常上抛，对应
            ``INFRA_ERROR``）。
        ValueError: ``cmd`` 为空或包含非字符串元素。
    """
    if not cmd:
        raise ValueError("cmd 不能为空列表")
    if not all(isinstance(arg, str) for arg in cmd):
        raise ValueError(f"cmd 所有元素必须是 str：{cmd!r}")

    env = _build_env(extra_env)
    cmd_str = " ".join(cmd)
    _logger.info("[%s] exec: %s", log_label, cmd_str)

    start = time.perf_counter()
    timed_out = False
    exit_code: int
    stdout: str
    stderr: str

    # Linux: 用 setsid 创建新进程组，超时时 killpg 杀整棵进程树（含 z3 孙进程）
    use_pgid = sys.platform != "win32"
    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
        "env": env,
        "shell": False,
    }
    if use_pgid:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(cmd), **popen_kwargs)
    try:
        raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = TIMEOUT_EXIT_CODE
        # 杀整棵进程树
        if use_pgid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                proc.kill()
        else:
            proc.kill()
        raw_stdout, raw_stderr = proc.communicate()
        _logger.warning(
            "[%s] timeout after %.2fs (limit=%s): %s",
            log_label,
            time.perf_counter() - start,
            timeout,
            cmd_str,
        )

    raw_stdout_bytes = raw_stdout if isinstance(raw_stdout, bytes) else None
    raw_stderr_bytes = raw_stderr if isinstance(raw_stderr, bytes) else None
    stdout = _safe_decode(raw_stdout)
    stderr = _safe_decode(raw_stderr)
    stdout = _smart_decode_stderr(stdout, raw_stdout_bytes)
    stderr = _smart_decode_stderr(stderr, raw_stderr_bytes)

    elapsed = time.perf_counter() - start

    _logger.info(
        "[%s] exit=%d elapsed=%.2fs timed_out=%s",
        log_label,
        exit_code,
        elapsed,
        timed_out,
    )
    if stdout:
        _logger.debug(
            "[%s] stdout (%d lines):\n%s",
            log_label,
            len(stdout.splitlines()),
            _truncate_for_log(stdout),
        )
    if stderr:
        _logger.debug(
            "[%s] stderr (%d lines):\n%s",
            log_label,
            len(stderr.splitlines()),
            _truncate_for_log(stderr),
        )

    return ToolResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        elapsed=elapsed,
        timed_out=timed_out,
    )
