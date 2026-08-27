"""日志配置模块。

提供 setup_logging 函数，同时输出到控制台和日志文件。

**2026-05-03 改动**:
- 每次启动用时间戳生成独立日志文件 (jspecforge_YYYYMMDD_HHMMSS.log),
  不再覆盖。``config.filename`` 作为基础名 (去掉 .log 后缀作前缀)。
- 不使用 RotatingFileHandler (单次启动通常不会跑到 50MB 上限)。
- 历史日志全部保留, 用户自行清理。

**2026-05-02 阶段 A 增强**:
- :func:`pause_console_handler` / :func:`resume_console_handler`:
  monitor 启用时暂停 console 输出避免与 rich.Live 冲突
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from jspecforge.config.settings import LoggingConfig

# 模块级标记：防止重复初始化
_initialized: bool = False
# 持有 console handler 引用,供 pause/resume 用
_console_handler: Optional[logging.Handler] = None
# 本次启动实际使用的日志文件路径 (供调试 / 测试)
_current_log_file: Optional[Path] = None

_LOG_FORMAT_DETAILED = "%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s"
_LOG_FORMAT_SIMPLE = "%(message)s"

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _build_log_filename(base_filename: str) -> str:
    """根据 config.filename 生成本次启动的时间戳文件名。

    规则: 取 base_filename 去掉扩展名作前缀,加时间戳,固定 .log 扩展名。

    Examples:
        >>> _build_log_filename("jspecforge.log")
        'jspecforge_20260503_184927.log'
        >>> _build_log_filename("custom")
        'custom_20260503_184927.log'
    """
    base = Path(base_filename).stem if base_filename else "jspecforge"
    if not base:
        base = "jspecforge"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base}_{ts}.log"


def setup_logging(
    config: LoggingConfig,
    log_dir: str,
) -> None:
    """初始化日志系统。

    配置根 logger 同时输出到控制台和**带时间戳的独立文件**。
    每次启动生成新文件,历史日志全部保留。
    重复调用时跳过（防重复初始化）。

    Args:
        config: logging 配置对象。
        log_dir: 日志文件存放目录。
    """
    global _initialized, _console_handler, _current_log_file
    if _initialized:
        return

    log_level = _LEVEL_MAP.get(config.level.upper(), logging.INFO)
    console_level = _LEVEL_MAP.get(config.console_level.upper(), logging.INFO)
    fmt = _LOG_FORMAT_DETAILED if config.format == "detailed" else _LOG_FORMAT_SIMPLE

    formatter = logging.Formatter(fmt)

    root_logger = logging.getLogger("jspecforge")
    root_logger.setLevel(log_level)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    _console_handler = console_handler

    # 文件 handler: 每次启动一个独立文件 (时间戳命名,不覆盖)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_filename = _build_log_filename(config.filename)
        log_file = Path(log_dir) / log_filename
        file_handler = logging.FileHandler(
            log_file,
            mode="w",
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        _current_log_file = log_file
        # 启动时记录本次日志文件位置, 方便用户排查
        root_logger.info("日志文件: %s", log_file)

    _initialized = True


def get_current_log_file() -> Optional[Path]:
    """返回本次启动实际使用的日志文件路径。``None`` 表示未初始化或无 log_dir。"""
    return _current_log_file


def pause_console_handler() -> bool:
    """暂停 console handler 的输出(monitor 启用时调用,避免日志撕烂面板)。

    日志仍会写文件,只是不再打到 stderr/stdout。

    Returns:
        ``True`` 表示成功暂停;``False`` 表示没找到 console handler。
    """
    if _console_handler is None:
        return False
    root_logger = logging.getLogger("jspecforge")
    if _console_handler in root_logger.handlers:
        root_logger.removeHandler(_console_handler)
        return True
    return False


def resume_console_handler() -> bool:
    """恢复 console handler(monitor 停止时调用)。

    Returns:
        ``True`` 表示成功恢复;``False`` 表示没 handler 可恢复。
    """
    if _console_handler is None:
        return False
    root_logger = logging.getLogger("jspecforge")
    if _console_handler not in root_logger.handlers:
        root_logger.addHandler(_console_handler)
        return True
    return False


def get_logger(name: str) -> logging.Logger:
    """获取 jspecforge 命名空间下的 logger。

    Args:
        name: 模块名（如 'config.loader'）。

    Returns:
        Logger 实例。
    """
    return logging.getLogger(f"jspecforge.{name}")


def reset_logging() -> None:
    """重置日志初始化标记（仅用于测试）。"""
    global _initialized, _console_handler, _current_log_file
    _initialized = False
    _console_handler = None
    _current_log_file = None
    root = logging.getLogger("jspecforge")
    root.handlers.clear()
    root.setLevel(logging.WARNING)
