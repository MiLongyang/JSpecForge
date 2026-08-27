"""javac 编译封装（默认 JDK 21）。

两个平台都是原生执行（不走 WSL）。Windows 平台 ``javac`` 命令位置由
``config.java.javac_cmd`` 决定（PATH 中或完整路径均可）。

错误信息按 ``MAX_ERROR_LINES`` 截断后入库 / 反馈给 LLM，避免占用过多
token（详见 docs/design/外部工具调用技术规格.md §6）。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from jspecforge.config.settings import AppConfig
from jspecforge.tools.tool_runner import ToolResult, run_subprocess
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.javac_tool")

# 错误信息截断行数上限,与 docs §6 的 truncate_compile_error 默认值保持一致
MAX_ERROR_LINES: int = 30

# javac 启动优化参数。仅 ``-J-Xverify:none``——跳过 javac 进程自身的
# 字节码校验,小幅缩短启动开销。**不**追加 ``TieredStopAtLevel=1``,
# javac 是编译器,关闭 JIT 反而拖慢编译。-Xverify:none 仅影响 javac
# 自己加载的 .class,不影响它对源码的语法/类型检查。
_JAVAC_STARTUP_OPTIMIZE_FLAGS: tuple[str, ...] = ("-J-Xverify:none",)

# javac stdout/stderr 强制 UTF-8 输出(2026-05-02 阶段 D.0):
# Windows 中文系统下 javac 默认输出 GBK 编码,导致 stderr 中的中文报错
# (如"错误: 找不到符号")在 UTF-8 解码后变成 ``����: �Ҳ�������`` 乱码。
# JDK 18+ 引入 ``stdout.encoding`` / ``stderr.encoding`` 系统属性,可显式
# 控制输出编码而不依赖 OS locale。我们用 JDK 21,直接加 ``-J-D...`` 透传。
# 永久启用,无副作用(Linux 下原本就是 UTF-8,加了也是 no-op)。
_JAVAC_ENCODING_FLAGS: tuple[str, ...] = (
    "-J-Dstdout.encoding=UTF-8",
    "-J-Dstderr.encoding=UTF-8",
    "-J-Dfile.encoding=UTF-8",  # 备份:JDK 17 兼容
)


class JavacRunner:
    """JDK 21 javac 编译封装。

    Major 工具的 JDK 11 javac 由 :class:`MajorRunner` 单独负责，**不要**用
    本类调用 JDK 11。
    """

    def __init__(self, config: AppConfig) -> None:
        """以全量 ``AppConfig`` 注入，避免每次调用都传一堆零散参数。

        Args:
            config: 已校验通过的 ``AppConfig`` 实例。
        """
        self._config = config

    def build_command(
        self,
        source_files: Sequence[str],
        *,
        classpath: Optional[str] = None,
        output_dir: Optional[str] = None,
        encoding: str = "UTF-8",
        extra_args: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """拼装 javac 命令。

        Args:
            source_files: 待编译的 .java 文件路径列表（至少 1 个）。
            classpath: classpath 字符串，``None`` 表示不传 ``-cp``。
            output_dir: 输出目录（``-d``），``None`` 时编译到源文件同目录。
            encoding: 源码编码，默认 UTF-8。
            extra_args: 额外参数（如 ``-Xlint:none``）。

        Returns:
            可直接交给 ``run_subprocess`` 的命令 list。
        """
        if not source_files:
            raise ValueError("source_files 不能为空")
        cmd: List[str] = [self._config.java.javac_cmd]
        # UTF-8 输出强制(D.0,中文系统乱码修复)—— 永久启用
        cmd.extend(_JAVAC_ENCODING_FLAGS)
        if self._config.java.javac_startup_optimize:
            cmd.extend(_JAVAC_STARTUP_OPTIMIZE_FLAGS)
        if classpath:
            cmd.extend(["-cp", classpath])
        if output_dir:
            cmd.extend(["-d", output_dir])
        cmd.extend(["-encoding", encoding])
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(source_files)
        return cmd

    def run(
        self,
        source_files: Sequence[str],
        *,
        classpath: Optional[str] = None,
        output_dir: Optional[str] = None,
        encoding: str = "UTF-8",
        extra_args: Optional[Sequence[str]] = None,
        timeout: Optional[int] = None,
    ) -> ToolResult:
        """执行 javac 编译。

        Args:
            source_files: 待编译的 .java 文件路径列表。
            classpath: 同 ``build_command``。
            output_dir: 同 ``build_command``。
            encoding: 同 ``build_command``。
            extra_args: 同 ``build_command``。
            timeout: 超时秒数，缺省时取 ``config.java.javac_timeout``。

        Returns:
            ``ToolResult`` —— ``exit_code=0`` 表示编译成功。
        """
        cmd = self.build_command(
            source_files,
            classpath=classpath,
            output_dir=output_dir,
            encoding=encoding,
            extra_args=extra_args,
        )
        actual_timeout = (
            timeout if timeout is not None else self._config.java.javac_timeout
        )
        return run_subprocess(cmd, actual_timeout, log_label="javac")


def truncate_compile_error(stderr: str, max_lines: int = MAX_ERROR_LINES) -> str:
    """截断 javac 编译错误信息，便于喂给 LLM。

    Args:
        stderr: 原始 stderr 内容。
        max_lines: 保留的最大行数，超出部分以 ``... (N more lines)`` 概括。

    Returns:
        截断后的字符串（已 ``rstrip()``）。
    """
    text = (stderr or "").strip()
    if not text:
        return ""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    head = "\n".join(lines[:max_lines])
    return f"{head}\n... ({len(lines) - max_lines} more lines)"
