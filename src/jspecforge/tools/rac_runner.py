"""RAC 编译运行器（在 :class:`OpenJMLRunner` 之上加 .class 存在性校验）。

依据 ``docs/design/外部工具调用技术规格.md`` §3.2 / §3.3。

RAC 成功的判定 = ``exit_code == 0`` **且** 输出目录下存在
``{ClassName}.class`` 文件。两个条件必须同时满足，否则上层（P6 处理器）
应回退 Task A 走语法重试分支（见 docs/design/核心工作流程设计.md §5.2）。

`-d` 参数指向 Windows 文件系统（通过 ``/mnt/c/...``，由 ``OpenJMLRunner``
内部翻译），所以 .class 最终落在 Windows 一侧，本机 javac/java 可以直接
读取，**无需**再做 wsl→win 反向翻译。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jspecforge.config.settings import AppConfig
from jspecforge.tools.openjml_tool import OpenJMLRunner
from jspecforge.tools.tool_runner import ToolResult
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("tools.rac_runner")


@dataclass(frozen=True)
class RACResult:
    """RAC 编译结果。

    Attributes:
        tool_result: 底层 :class:`ToolResult`，含完整 stdout/stderr。
        success: ``True`` 仅当 ``exit_code == 0`` 且 ``.class`` 文件存在。
        class_file: 期望生成的 ``.class`` 文件路径（无论是否存在均给出）。
        class_exists: ``.class`` 文件是否真的存在。
    """

    tool_result: ToolResult
    success: bool
    class_file: Path
    class_exists: bool


class RACRunner:
    """RAC 编译运行器。

    本类不复用 ``OpenJMLRunner`` 的实例时，可以让上层注入；缺省自己 new 一个。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        openjml_runner: Optional[OpenJMLRunner] = None,
    ) -> None:
        """构造。

        Args:
            config: ``AppConfig``。
            openjml_runner: 可注入已存在的 ``OpenJMLRunner``，便于复用单实例。
        """
        self._config = config
        self._openjml = openjml_runner or OpenJMLRunner(config)

    def run(
        self,
        java_file_path: str,
        output_dir: str,
        class_name: str,
        *,
        timeout: Optional[int] = None,
    ) -> RACResult:
        """执行 RAC 编译并校验 .class 是否生成。

        Args:
            java_file_path: 待编译的 .java 文件路径（Windows 平台传 Windows
                风格路径，由底层负责翻译给 WSL）。
            output_dir: ``-d`` 输出目录的 **Windows 端路径**（即本机文件
                系统中的真实位置，用于 ``.class`` 存在性校验）。
            class_name: 待校验的目标类名（不含 ``.class`` 后缀）。
            timeout: 超时秒数，缺省走 ``OpenJMLRunner`` 的默认值。

        Returns:
            :class:`RACResult`。
        """
        tool_result = self._openjml.run_rac(
            java_file_path,
            output_dir,
            timeout=timeout,
        )
        class_file = Path(output_dir) / f"{class_name}.class"
        class_exists = class_file.is_file()
        success = tool_result.exit_code == 0 and class_exists

        if tool_result.exit_code == 0 and not class_exists:
            _logger.warning(
                "RAC exit_code=0 but %s missing — 视为编译失败（docs §5.2）",
                class_file,
            )

        return RACResult(
            tool_result=tool_result,
            success=success,
            class_file=class_file,
            class_exists=class_exists,
        )
