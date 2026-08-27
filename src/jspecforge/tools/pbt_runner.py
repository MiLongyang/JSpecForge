"""PBT 测试驱动运行器。

P3 阶段的范围（与 docs/plan/P3-外部工具封装.md 对齐）：

- (a) 测试驱动模板注入 → **不在本阶段**，由 P5/P6 阶段的
  ``tools/test_driver_builder.py`` 负责
- (b) javac 编译 TestDriver → 本类
- (c) java 运行 + 解析 stdout JSON → 本类

JVM 运行参数（必须，docs/design/外部工具调用技术规格.md §5.2）：

- ``-Dorg.jmlspecs.openjml.rac=exception``：让 RAC 在违规时抛
  ``JmlAssertionError``，否则只 ``System.out.println()`` 不抛异常
- ``-ea``：启用 Java assert
- ``-Dfile.encoding=UTF-8``：避免 Windows 默认 GBK 编码

classpath 必须包含（docs §4.3）：

  ::

    pbt_bin_dir + rac_output_dir + jmlruntime.jar + jmllang.jar + lib_dir

缺少任何一项都会触发 ``NoClassDefFoundError: org/jmlspecs/lang/internal/bigint``
等运行期错误。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from jspecforge.config.settings import AppConfig
from jspecforge.tools.javac_tool import JavacRunner
from jspecforge.tools.path_utils import build_classpath, convert_path_for_tool
from jspecforge.tools.tool_runner import ToolResult, run_subprocess
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.pbt_runner")

# PBT TestDriver 启动优化参数。仅 ``-Xverify:none``——TestDriver 要跑
# ``pbt.test_runs`` 次（默认 1000）循环,需要 JIT 完整优化才跑得快;
# 关闭 JIT（如 TieredStopAtLevel=1）会让 PBT 整体变慢 3~10 倍。
_PBT_STARTUP_OPTIMIZE_FLAGS: tuple[str, ...] = ("-Xverify:none",)


@dataclass(frozen=True)
class PBTCompileResult:
    """PBT TestDriver 编译结果。"""

    tool_result: ToolResult
    success: bool


@dataclass(frozen=True)
class PBTRunResult:
    """PBT TestDriver 运行结果。

    Attributes:
        tool_result: 底层 :class:`ToolResult`。
        success: 退出码 = 0。
        parsed_json: stdout 解析得到的 JSON 字典；解析失败时为 ``None``。
        parse_error: JSON 解析错误（``parsed_json is None`` 时给出）。
    """

    tool_result: ToolResult
    success: bool
    parsed_json: Optional[dict]
    parse_error: Optional[str]


class PBTRunner:
    """PBT 测试驱动编译 + 运行封装。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        javac_runner: Optional[JavacRunner] = None,
    ) -> None:
        """构造。

        Args:
            config: ``AppConfig``。
            javac_runner: 可注入复用，缺省自建。
        """
        self._config = config
        self._javac = javac_runner or JavacRunner(config)

    # ------------------------------------------------------------------
    # 编译
    # ------------------------------------------------------------------

    def build_classpath_for_test_driver(self, rac_output_dir: str) -> str:
        """构建 TestDriver **编译** 阶段的 classpath。

        TestDriver 引用 RAC 编译产物中的目标类、JML 运行时与内部类型类。

        Args:
            rac_output_dir: RAC 输出 .class 所在目录。

        Returns:
            ``os.pathsep`` 分隔的 classpath 字符串。
        """
        lib_dir = self._config.paths.lib_dir
        return build_classpath(
            rac_output_dir,
            str(Path(lib_dir) / "jmlruntime.jar"),
            str(Path(lib_dir) / "jmllang.jar"),
            lib_dir,
        )

    def compile_test_driver(
        self,
        test_driver_java: str,
        rac_output_dir: str,
        pbt_bin_dir: str,
        *,
        timeout: Optional[int] = None,
    ) -> PBTCompileResult:
        """用 javac 编译 TestDriver_*.java。

        Args:
            test_driver_java: 已注入 ``generateValidInput`` 的 .java 路径。
            rac_output_dir: RAC 编译输出目录（含目标类 .class）。
            pbt_bin_dir: TestDriver 编译输出目录（``-d``）。
            timeout: 缺省取 ``config.java.javac_timeout``。

        Returns:
            :class:`PBTCompileResult`。
        """
        classpath = self.build_classpath_for_test_driver(rac_output_dir)
        result = self._javac.run(
            [test_driver_java],
            classpath=classpath,
            output_dir=pbt_bin_dir,
            timeout=timeout,
        )
        return PBTCompileResult(
            tool_result=result,
            success=result.exit_code == 0,
        )

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------

    def build_run_command(
        self,
        test_driver_class: str,
        pbt_bin_dir: str,
        rac_output_dir: str,
        program_args: Sequence[str],
    ) -> List[str]:
        """拼装 ``java`` 运行命令。

        优先使用 OpenJML 提供的 ``openjml-java``（config.paths.openjml_java_cmd），
        以支持 RAC model method（如 String.numsplit）。未配置时回退到系统 java。

        Args:
            test_driver_class: 全限定类名（默认包则直接是类名）。
            pbt_bin_dir: TestDriver .class 所在目录。
            rac_output_dir: RAC .class 所在目录。
            program_args: 传给 ``main(String[])`` 的位置参数（如
                ``[totalRuns, earlyStop, earlyStopMaxFails, sampleMax]``）。

        Returns:
            可直接交给 ``run_subprocess`` 的命令 list。
        """
        jvm_args = list(self._config.java.test_driver_jvm_args)
        heap = self._config.java.test_driver_heap_max
        if heap:
            jvm_args.append(f"-Xmx{heap}")
        if self._config.pbt.startup_optimize:
            jvm_args.extend(_PBT_STARTUP_OPTIMIZE_FLAGS)

        lib_dir = self._config.paths.lib_dir
        platform = self._config.platform
        openjml_java = self._config.paths.openjml_java_cmd

        if openjml_java and platform == "windows":
            # 用 openjml-java (WSL) 运行，路径需 win→wsl 转换
            # 注意：WSL 内 classpath 必须用 : 分隔，不能用 Windows 的 ;
            wsl_distro = self._config.environment.wsl_distro or "Ubuntu"
            classpath = ":".join([
                convert_path_for_tool(pbt_bin_dir, platform),
                convert_path_for_tool(rac_output_dir, platform),
                convert_path_for_tool(str(Path(lib_dir) / "jmlruntime.jar"), platform),
                convert_path_for_tool(str(Path(lib_dir) / "jmllang.jar"), platform),
                convert_path_for_tool(lib_dir, platform),
            ])
            # WSL 命令格式：wsl -d <distro> <command> <args>（不需要 -- 分隔符）
            cmd: List[str] = [
                "wsl", "-d", wsl_distro,
                openjml_java, *jvm_args, "-cp", classpath, test_driver_class,
            ]
        elif openjml_java and platform == "linux":
            classpath = build_classpath(
                pbt_bin_dir, rac_output_dir,
                str(Path(lib_dir) / "jmlruntime.jar"),
                str(Path(lib_dir) / "jmllang.jar"),
                lib_dir,
            )
            cmd = [openjml_java, *jvm_args, "-cp", classpath, test_driver_class]
        else:
            # 未配置 openjml_java_cmd，回退到系统 java
            java_cmd = self._config.java.java_cmd
            classpath = build_classpath(
                pbt_bin_dir, rac_output_dir,
                str(Path(lib_dir) / "jmlruntime.jar"),
                str(Path(lib_dir) / "jmllang.jar"),
                lib_dir,
            )
            cmd = [java_cmd, *jvm_args, "-cp", classpath, test_driver_class]

        cmd.extend(str(arg) for arg in program_args)
        return cmd

    def run_test_driver(
        self,
        test_driver_class: str,
        pbt_bin_dir: str,
        rac_output_dir: str,
        program_args: Sequence[str],
        *,
        timeout: Optional[int] = None,
    ) -> PBTRunResult:
        """运行 TestDriver 并解析 stdout JSON。

        Args:
            test_driver_class: 同 ``build_run_command``。
            pbt_bin_dir: 同上。
            rac_output_dir: 同上。
            program_args: 同上。
            timeout: 缺省取 ``config.java.test_driver_timeout``。

        Returns:
            :class:`PBTRunResult`。
        """
        cmd = self.build_run_command(
            test_driver_class,
            pbt_bin_dir,
            rac_output_dir,
            program_args,
        )
        actual_timeout = (
            timeout
            if timeout is not None
            else self._config.java.test_driver_timeout
        )
        tool_result = run_subprocess(cmd, actual_timeout, log_label="pbt-run")

        success = tool_result.exit_code == 0
        parsed: Optional[dict] = None
        parse_error: Optional[str] = None
        if success:
            stripped = (tool_result.stdout or "").strip()
            if not stripped:
                parse_error = "TestDriver stdout is empty"
            else:
                # 尝试直接解析整个 stdout
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict):
                        parsed = obj
                    else:
                        parse_error = (
                            f"TestDriver stdout 不是 JSON 对象: type="
                            f"{type(obj).__name__}"
                        )
                except json.JSONDecodeError:
                    # 直接解析失败，尝试从混合输出中提取 JSON
                    # 找到以 {" 开始的行（JSON 对象开始）
                    lines = stripped.split("\n")
                    json_start_idx = -1
                    for i, line in enumerate(lines):
                        if line.strip().startswith("{"):
                            # 尝试解析这一行
                            try:
                                obj = json.loads(line.strip())
                                if isinstance(obj, dict):
                                    json_start_idx = i
                                    parsed = obj
                                    break
                            except json.JSONDecodeError:
                                continue
                    if parsed is None:
                        parse_error = "TestDriver stdout 中未找到有效 JSON 对象"
                    else:
                        _logger.debug("pbt-run: extracted JSON from line %d", json_start_idx)
            if parse_error:
                _logger.warning("pbt-run: %s", parse_error)

        return PBTRunResult(
            tool_result=tool_result,
            success=success,
            parsed_json=parsed,
            parse_error=parse_error,
        )
