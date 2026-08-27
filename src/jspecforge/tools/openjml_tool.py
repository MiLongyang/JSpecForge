"""OpenJML 调用封装（ESC 静态检验 + RAC 编译插桩）。

平台分支说明（CLAUDE.md §2.1 / §2.4）：

- ``platform="windows"``：通过 ``wsl -d {distro} -- {openjml_cmd} ...`` 调用，
  Windows 路径需先经 ``path_utils.win_to_wsl_path`` 翻译为 ``/mnt/x/...``
- ``platform="linux"``：直接原生执行，无需 WSL，无需路径转换

参数构造：

- ESC：``--esc`` + ``openjml.esc_extra_args``（含 ``--code-math=bigint`` /
  ``--spec-math=bigint`` / ``--nullable-by-default``）+ ``-J-Xmx{heap}``
- RAC：``--rac`` + ``openjml.rac_extra_args``（**仅** ``--nullable-by-default``，
  绝对**不能含** ``--code-math`` / ``--spec-math``，gotchas §3.2 红线）
  + ``-J-Xmx{heap}`` + ``-d {output_dir}``

本类仅负责执行子进程并返回原始 ``ToolResult``。结果分类（label + 动态黑名单
关键字）由 :func:`tools.oj_classifier.classify_oj_result` 在调用方负责。
"""

from __future__ import annotations

from typing import List, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.tools.path_utils import convert_path_for_tool
from jspecforge.tools.tool_runner import ToolResult, run_subprocess
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.openjml_tool")

# RAC 模式下绝对不能出现的参数（gotchas §3.2 红线）
_RAC_FORBIDDEN_FLAGS: tuple[str, ...] = ("--code-math", "--spec-math")

# OJ ESC + RAC 共用的 JVM 启动开销优化参数（startup_optimize=True 时附加）。
# 通过 OpenJML 的 ``-J`` 前缀透传给底层 JVM。
# 设计依据：单次 OJ ESC / RAC 调用为"短启动 + 短计算"型,默认 JVM 多层 JIT
# 优化 + 字节码校验消耗显著但收益甚微,关闭后单次响应时间下降 30~50%。
# 详见 P7 启动会话决议（hot-fix）。
_OJ_STARTUP_OPTIMIZE_FLAGS: tuple[str, ...] = (
    "-J-XX:TieredStopAtLevel=1",  # 只用 C1 JIT,跳过 C2 完整优化
    "-J-Xverify:none",            # 跳过字节码校验
    "-J-XX:+UseParallelGC",       # 吞吐量优先 GC
    "-J-XX:CICompilerCount=2",    # 限制 JIT 编译线程数
)

# OJ stdout/stderr 强制 UTF-8 输出(2026-05-02 阶段 D.0):
# 同 javac,Windows 中文系统下 OpenJML 内部输出可能含中文(虽然主要是英文)。
# 永久启用,Linux 下原本就 UTF-8,加了 no-op,无副作用。
_OJ_ENCODING_FLAGS: tuple[str, ...] = (
    "-J-Dstdout.encoding=UTF-8",
    "-J-Dstderr.encoding=UTF-8",
    "-J-Dfile.encoding=UTF-8",
)


class OpenJMLConfigError(Exception):
    """OpenJML 配置不合规时抛出（如 RAC 参数含禁用标记）。"""


class OpenJMLRunner:
    """OpenJML ESC + RAC 调用封装。

    Windows 平台的 ``openjml_cmd``（``config.openjml.cmd``）应是 **WSL 内**的
    绝对路径（如 ``/home/mloser/openjml_tool/openjml``），不做 win→wsl 转换。
    Linux 平台的 ``openjml_cmd`` 是原生路径（如 ``/opt/openjml/openjml``）。
    """

    def __init__(self, config: AppConfig) -> None:
        """初始化时对 ``rac_extra_args`` 做防御断言。

        Args:
            config: 已校验通过的 ``AppConfig`` 实例。

        Raises:
            OpenJMLConfigError: ``rac_extra_args`` 包含禁用参数。
        """
        self._config = config
        self._validate_rac_args()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def run_esc(
        self,
        java_file_path: str,
        *,
        timeout: Optional[int] = None,
    ) -> ToolResult:
        """以 ESC 模式调用 OpenJML 静态检验。

        Args:
            java_file_path: 待检验的 .java 文件路径（Windows 平台传 Windows
                风格路径即可，本方法负责翻译为 WSL 路径）。
            timeout: Python 层超时秒数，缺省取 ``config.openjml.timeout``
                （默认 180s）。

        Returns:
            原始 ``ToolResult``。分流交给 ``classify_oj_result``。
        """
        cmd = self._build_command(
            mode_args=["--esc", *self._config.openjml.esc_extra_args],
            java_file_path=java_file_path,
            output_dir=None,
        )
        return run_subprocess(
            cmd,
            timeout if timeout is not None else self._config.openjml.timeout,
            log_label="openjml-esc",
        )

    def run_rac(
        self,
        java_file_path: str,
        output_dir: str,
        *,
        timeout: Optional[int] = None,
    ) -> ToolResult:
        """以 RAC 模式调用 OpenJML 编译插桩。

        Args:
            java_file_path: 待编译的 .java 文件路径。
            output_dir: ``.class`` 输出目录（Windows 平台同样传 Windows
                风格路径，本方法负责翻译）。
            timeout: 同 ``run_esc``。

        Returns:
            原始 ``ToolResult``。RAC 成功判定 = ``exit_code=0`` ∧ ``.class``
            文件存在，由 :class:`tools.rac_runner.RACRunner` 包装。
        """
        cmd = self._build_command(
            mode_args=["--rac", *self._config.openjml.rac_extra_args],
            java_file_path=java_file_path,
            output_dir=output_dir,
        )
        return run_subprocess(
            cmd,
            timeout if timeout is not None else self._config.openjml.rac_timeout,
            log_label="openjml-rac",
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _validate_rac_args(self) -> None:
        """检查 ``rac_extra_args`` 不含 ``--code-math`` / ``--spec-math``。"""
        forbidden_hits: List[str] = []
        for arg in self._config.openjml.rac_extra_args:
            for flag in _RAC_FORBIDDEN_FLAGS:
                if arg == flag or arg.startswith(f"{flag}="):
                    forbidden_hits.append(arg)
        if forbidden_hits:
            raise OpenJMLConfigError(
                f"openjml.rac_extra_args 不能包含 {_RAC_FORBIDDEN_FLAGS}，"
                f"检测到禁用参数: {forbidden_hits}（gotchas §3.2 红线，会改变"
                f"算术溢出语义导致验证结果与运行不一致）"
            )

    def _build_command(
        self,
        *,
        mode_args: List[str],
        java_file_path: str,
        output_dir: Optional[str],
    ) -> List[str]:
        """根据 ``config.platform`` 拼装 OpenJML 命令。

        Args:
            mode_args: ``--esc`` / ``--rac`` 起头的模式参数（含从 config 取
                的 ``esc_extra_args`` / ``rac_extra_args``）。
            java_file_path: .java 文件路径（原始平台路径，由本方法负责翻译）。
            output_dir: ``-d`` 参数值；``None`` 时不附加 ``-d``。

        Returns:
            可直接交给 ``run_subprocess`` 的命令 list。
        """
        platform = self._config.platform
        openjml_cmd = self._config.openjml.cmd
        heap_arg = f"-J-Xmx{self._config.openjml.jvm_heap_max}"

        # 翻译路径（Windows 平台 win→wsl，Linux 平台原样返回）
        java_path_in_tool = convert_path_for_tool(java_file_path, platform)
        out_dir_in_tool = (
            convert_path_for_tool(output_dir, platform) if output_dir else None
        )

        # 拼装工具自身的参数
        tool_args: List[str] = list(mode_args)
        tool_args.append(heap_arg)
        # UTF-8 输出强制(D.0,中文系统乱码修复)—— 永久启用
        tool_args.extend(_OJ_ENCODING_FLAGS)
        if self._config.openjml.startup_optimize:
            tool_args.extend(_OJ_STARTUP_OPTIMIZE_FLAGS)
        if out_dir_in_tool:
            tool_args.extend(["-d", out_dir_in_tool])
        tool_args.append(java_path_in_tool)

        if platform == "windows":
            # 通过 wsl 进入指定发行版后执行 openjml；--  之后的全部参数透传
            wsl_distro = self._config.environment.wsl_distro or "Ubuntu"
            return [
                "wsl",
                "-d",
                wsl_distro,
                "--",
                openjml_cmd,
                *tool_args,
            ]
        if platform == "linux":
            return [openjml_cmd, *tool_args]

        # config.loader.validate_config 已挡住非法 platform，但保险起见
        raise OpenJMLConfigError(
            f"未识别的 platform: {platform!r}（应为 'windows' 或 'linux'）"
        )
