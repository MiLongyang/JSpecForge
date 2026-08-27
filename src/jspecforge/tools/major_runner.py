"""Major 3.0.1 变异测试运行器（JDK 11 javac + Major 插件）。

依据 ``CLAUDE.md`` §2.3 / gotchas §2.4 与 **MT_Demo plan_c probe_suppression.py
的实测可用调用方式**（design §7.1 的命令实际上无法跑通——module 名错误）。

关键约束：

- **必须用 JDK 11 javac**（``mt.jdk11_javac``）。Major 3.0.1 不兼容 JDK 21
- **两个平台都原生执行**（不走 WSL）。Windows 上 Major 在原生 JVM 中跑
- ``major_home`` 指向解压后的 ``major-3.0.1_jre11/major`` 目录，包含
  ``bin/`` ``lib/`` ``mml/``。本类引用 ``lib/major.jar`` 作为 classpath
- 必须给 ``jdk.compiler`` 模块开 9 项 ``--add-opens``（JDK 9+ 强封装：
  Major 用反射访问 javac 内部 API，必须显式打开访问权）

输出：

- ``{output_dir}/mutants/`` — 每个变异体一个 .java（保留 JML 注释）
- ``{output_dir}/mutants.log`` — 变异清单（ID / 位置 / 原始 → 变异）
- ``{output_dir}/classes/``  — javac 顺带产出的原始 .class（不使用，但需要 -d）
- ``{output_dir}/suppression.log`` — EMS 过滤记录（**可忽略，不解析**）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from jspecforge.config.settings import AppConfig
from jspecforge.tools.tool_runner import ToolResult, run_subprocess
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.major_runner")

# Major 默认超时（单文件变异通常在数秒到几十秒）
_DEFAULT_TIMEOUT_SECONDS: int = 120

# Major JVM 启动开销优化参数（与 OJ/RAC/PBT 同源，通过 -J 透传）。
# Major 每个变异体都重启 JVM，启动密集 + 短任务，开启后单次响应时间显著下降。
_MT_STARTUP_OPTIMIZE_FLAGS: List[str] = [
    "-J-XX:TieredStopAtLevel=1",
    "-J-Xverify:none",
    "-J-XX:+UseParallelGC",
    "-J-XX:CICompilerCount=2",
]

# JDK 9+ 模块系统下，Major 反射访问 javac 内部 API 必须打开的 9 个包。
# 来自 MT_Demo plan_c probe_suppression.py 实测可用配置——不开任何一项都会
# 触发 ``InaccessibleObjectException: module jdk.compiler does not "opens ..."``。
_JAVAC_ADD_OPENS_FLAGS: List[str] = [
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.code=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.comp=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.jvm=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.main=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.processing=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED",
    "-J--add-opens=jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED",
]


@dataclass(frozen=True)
class MajorResult:
    """Major 变异生成结果。

    Attributes:
        tool_result: 底层 :class:`ToolResult`。
        success: 退出码 = 0 且 ``mutants.log`` 文件存在。
        mutants_dir: 期望的 ``{output_dir}/mutants`` 目录。
        mutants_log: 期望的 ``{output_dir}/mutants.log`` 文件。
        log_exists: ``mutants.log`` 是否真的生成。
    """

    tool_result: ToolResult
    success: bool
    mutants_dir: Path
    mutants_log: Path
    log_exists: bool


class MajorRunner:
    """Major 3.0.1 变异测试运行器（生成变异体）。"""

    def __init__(self, config: AppConfig) -> None:
        """构造时校验 ``mt.jdk11_javac`` 与 ``mt.major_home`` 已配置。

        若 ``mt.enabled=false``，本类仍可被实例化但 ``run`` 会抛错（避免
        运行时被无意调用）。
        """
        self._config = config

    def build_command(
        self,
        source_files: Sequence[str],
        output_dir: str,
        *,
        extra_args: Optional[Sequence[str]] = None,
    ) -> List[str]:
        """拼装 Major 命令（JDK 11 javac + ``-Xplugin:MajorPlugin``）。

        命令结构（参考 MT_Demo plan_c probe_suppression.py 实测可用形式）：

          ::

            <jdk11_javac>
              -J--add-opens=jdk.compiler/...  (× 9 项)
              [启动优化 -J 参数]
              -cp  <major_home>/lib/major.jar
              -Xplugin:MajorPlugin export.mutants
                                   mutants.directory:<output_dir>/mutants
                                   mutants.log:<output_dir>/mutants.log
                                   strict.checks
              -d <output_dir>/classes
              <source_files...>

        Args:
            source_files: 待变异的 .java 文件路径（至少 1 个）。
            output_dir: Major 产物输出根目录。``mutants/`` ``mutants.log``
                ``classes/`` 都落在此目录下。
            extra_args: 额外参数（追加到 javac 命令末尾，源文件之前）。

        Returns:
            可直接交给 ``run_subprocess`` 的命令 list。

        Raises:
            ValueError: ``source_files`` 为空，或 jdk11_javac / major_home
                未配置。
        """
        if not source_files:
            raise ValueError("source_files 不能为空")
        jdk11_javac = self._config.mt.jdk11_javac
        major_home = self._config.mt.major_home
        if not jdk11_javac:
            raise ValueError(
                "mt.jdk11_javac 未配置；Major 3.0.1 不兼容 JDK 21，必须显式"
                "指定 JDK 11 javac 路径"
            )
        if not major_home:
            raise ValueError("mt.major_home 未配置")

        major_jar = str(Path(major_home) / "lib" / "major.jar")
        out_path = Path(output_dir)
        mutants_dir = str(out_path / "mutants")
        mutants_log = str(out_path / "mutants.log")
        classes_dir = str(out_path / "classes")

        # 拼装 -Xplugin:MajorPlugin 单个字符串参数（plugin 选项以空格分隔）
        plugin_arg = (
            "-Xplugin:MajorPlugin export.mutants "
            f"mutants.directory:{mutants_dir} "
            f"mutants.log:{mutants_log} "
            "strict.checks"
        )

        cmd: List[str] = [jdk11_javac]
        # 9 项 add-opens 必须放在所有其他 -J 参数之前
        cmd.extend(_JAVAC_ADD_OPENS_FLAGS)
        if self._config.mt.startup_optimize:
            cmd.extend(_MT_STARTUP_OPTIMIZE_FLAGS)
        cmd.extend(["-cp", major_jar, plugin_arg, "-d", classes_dir])
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(source_files)
        return cmd

    def run(
        self,
        source_files: Sequence[str],
        output_dir: str,
        *,
        extra_args: Optional[Sequence[str]] = None,
        timeout: Optional[int] = None,
    ) -> MajorResult:
        """执行 Major 变异生成。

        Args:
            source_files: 同 ``build_command``。
            output_dir: 同 ``build_command``。``mutants/`` 与 ``mutants.log``
                会落在此目录下。
            extra_args: 同 ``build_command``。
            timeout: 缺省 ``_DEFAULT_TIMEOUT_SECONDS``（120 秒），单文件
                变异通常在数秒到几十秒。

        Returns:
            :class:`MajorResult`。

        Raises:
            RuntimeError: ``mt.enabled=false`` 时调用本方法。
        """
        if not self._config.mt.enabled:
            raise RuntimeError(
                "mt.enabled=false，禁止调用 MajorRunner.run()"
            )
        cmd = self.build_command(
            source_files,
            output_dir,
            extra_args=extra_args,
        )
        actual_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT_SECONDS
        tool_result = run_subprocess(cmd, actual_timeout, log_label="major")

        out_path = Path(output_dir)
        mutants_dir = out_path / "mutants"
        mutants_log = out_path / "mutants.log"
        log_exists = mutants_log.is_file()

        success = tool_result.exit_code == 0 and log_exists
        if tool_result.exit_code == 0 and not log_exists:
            _logger.warning(
                "Major exit_code=0 but %s missing — 视为变异生成失败",
                mutants_log,
            )

        return MajorResult(
            tool_result=tool_result,
            success=success,
            mutants_dir=mutants_dir,
            mutants_log=mutants_log,
            log_exists=log_exists,
        )
