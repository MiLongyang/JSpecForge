"""Major 变异体加载与截断。

解析 Major 3.0.1 输出的 ``mutants.log``（冒号分隔，8 字段），收集
``mutants/{id}/{ClassName}.java`` 变异体源文件，按 ``max_mutants_per_method``
截断。

``mutants.log`` 格式（与 MT_Demo 实测一致）：

  ``major_id:operator:from:to:location:line:column:snippet``

变异体文件位置：``{output_dir}/mutants/{major_id}/{ClassName}.java``
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from jspecforge.config.settings import AppConfig
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("mt.mutant_loader")

# mutants.log 行正则（MT_Demo plan_c 实测格式）
_LOG_PATTERN = re.compile(
    r"^(?P<major_id>\d+):(?P<operator>[^:]*):(?P<from>[^:]*):(?P<to>[^:]*):"
    r"(?P<location>[^:]*):(?P<line>\d+):(?P<column>\d+):(?P<snippet>.*)$"
)


@dataclass(frozen=True)
class MajorMutant:
    """单个 Major 变异体元数据。

    Attributes:
        major_id: Major 内部 ID（与 mutants/ 子目录名一致）。
        operator: 变异算子名称（如 ROR、AOR）。
        from_code: 原始代码片段。
        to_code: 变异后代码片段。
        location: 变异位置描述。
        line: 源码行号。
        column: 源码列号。
        source_file: 变异体 .java 文件路径。
        log_entry: mutants.log 原始行。
    """

    major_id: int
    operator: str
    from_code: str
    to_code: str
    location: str
    line: int
    column: int
    source_file: Path
    log_entry: str


def parse_mutants_log(
    mutants_log: Path,
    mutants_root: Path,
    class_name: str,
) -> List[MajorMutant]:
    """解析 ``mutants.log`` 为 :class:`MajorMutant` 列表。

    Args:
        mutants_log: ``mutants.log`` 文件路径。
        mutants_root: ``mutants/`` 目录路径。
        class_name: Java 类名（用于定位变异体 .java 文件）。

    Returns:
        按 ``major_id`` 升序排列的变异体列表。

    Raises:
        FileNotFoundError: ``mutants.log`` 不存在或某个变异体 .java 文件缺失。
        ValueError: 无法识别的 log 行格式。
    """
    if not mutants_log.is_file():
        raise FileNotFoundError(f"mutants.log 不存在: {mutants_log}")

    text = mutants_log.read_text(encoding="utf-8")
    mutants: List[MajorMutant] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _LOG_PATTERN.match(line)
        if not match:
            _logger.warning("跳过无法识别的 mutants.log 行: %s", line)
            continue

        major_id = int(match.group("major_id"))
        source_file = mutants_root / str(major_id) / f"{class_name}.java"

        mutants.append(
            MajorMutant(
                major_id=major_id,
                operator=match.group("operator"),
                from_code=match.group("from"),
                to_code=match.group("to"),
                location=match.group("location"),
                line=int(match.group("line")),
                column=int(match.group("column")),
                source_file=source_file,
                log_entry=line,
            )
        )

    mutants.sort(key=lambda m: m.major_id)
    return mutants


def select_mutants(
    mutants: List[MajorMutant],
    max_mutants: int,
) -> tuple[List[MajorMutant], Dict[str, int]]:
    """按上限截断变异体列表。

    Args:
        mutants: 全部变异体。
        max_mutants: 每方法最大变异体数（0 表示不限制）。

    Returns:
        (截断后的列表, budget 字典)。
        budget 含 ``mutants_total``、``mutants_used``、``used_full_available``。
    """
    mutants_total = len(mutants)
    if max_mutants > 0 and mutants_total > max_mutants:
        selected = mutants[:max_mutants]
    else:
        selected = mutants
    budget = {
        "mutants_total": mutants_total,
        "mutants_used": len(selected),
        "used_full_available": mutants_total <= max_mutants if max_mutants > 0 else True,
    }
    return selected, budget


def load_mutants(
    major_output_dir: Path,
    class_name: str,
    max_mutants: int = 0,
) -> tuple[List[MajorMutant], Dict[str, int]]:
    """从 Major 输出目录加载变异体并截断。

    Args:
        major_output_dir: Major 输出根目录（含 ``mutants/`` 和 ``mutants.log``）。
        class_name: Java 类名。
        max_mutants: 每方法最大变异体数（0 = 不限制）。

    Returns:
        (变异体列表, budget)。
    """
    mutants = parse_mutants_log(
        major_output_dir / "mutants.log",
        major_output_dir / "mutants",
        class_name,
    )
    selected, budget = select_mutants(mutants, max_mutants)
    _logger.info(
        "加载变异体: total=%d, used=%d, class=%s",
        budget["mutants_total"],
        budget["mutants_used"],
        class_name,
    )
    return selected, budget


def extract_class_name(full_java_with_jml: str) -> str:
    """从 Java 源码中提取顶层类名。

    Major 输出的变异体 .java 文件名 = ``{ClassName}.java``，需要类名来定位。

    匹配策略：扫描每一行（跳过 JML 注释行），用正则匹配 ``[modifiers] class XXX``。
    支持的修饰符组合（顺序任意，可缺省）：

    - ``public class Solution`` (P3 样例风格)
    - ``class Solution``        (P8 实测产出风格——LLM 包装时不加 public)
    - ``public final class Foo``、``abstract class Bar`` 等

    Args:
        full_java_with_jml: 完整 Java 源码（含 JML 注释）。

    Returns:
        类名字符串。

    Raises:
        ValueError: 无法识别类名。
    """
    # 修饰符可选（public/private/protected/final/abstract/static/strictfp 任意组合）
    modifier_group = r"(?:(?:public|private|protected|final|abstract|static|strictfp)\s+)*"
    pattern = re.compile(
        r"^\s*(?P<mods>" + modifier_group + r")"
        r"class\s+(?P<name>[A-Za-z_$][\w$]*)"
    )

    fallback_name: Optional[str] = None
    for line in full_java_with_jml.splitlines():
        stripped = line.strip()
        if stripped.startswith("//@") or stripped.startswith("/*@"):
            continue
        if stripped.startswith("@") or stripped.startswith("//"):
            continue
        if stripped.startswith("/*") or stripped.startswith("*"):
            continue
        m = pattern.match(stripped)
        if not m:
            continue
        name = m.group("name")
        if "public" in m.group("mods").split():
            return name
        if fallback_name is None:
            fallback_name = name

    if fallback_name is not None:
        return fallback_name

    public_inline_pattern = re.compile(
        r"\bpublic\s+(?:(?:final|abstract|static|strictfp)\s+)*"
        r"class\s+([A-Za-z_$][\w$]*)\b[^{;]*\{"
    )
    m = public_inline_pattern.search(full_java_with_jml)
    if m:
        return m.group(1)

    inline_pattern = re.compile(
        r"\b" + modifier_group + r"class\s+([A-Za-z_$][\w$]*)\b[^{;]*\{"
    )
    m = inline_pattern.search(full_java_with_jml)
    if m:
        return m.group(1)
    raise ValueError("无法从 Java 源码中提取 class 名")
