"""处理器共享格式化工具（报错截断 + 失败样本格式化）。

P5 提示词渲染要求所有变量值是已格式化的 ``str``——本模块负责把结构化
数据（PBT JSON、tool stderr、统计字典）格式化为字符串，再传给
``render(template_id, **kwargs)``。

P5 §7.9 明确划给 P6 的工作（"报错截断由 P6 完成"、"失败样本格式由 P6 决定"）。
本模块对应 plan §3.2 / §3.3。

接口设计原则：

- 所有函数返回**纯字符串**（直接拼进提示词）
- 截断时附 ``... (N more lines)`` 提示，便于 LLM 知道还有内容
- 失败样本按 JML 子句分类聚合，每类最多 N 个样本（``ErrorLogConfig.failure_sample_max_chars``
  控制总体大小）

**依赖**：仅 Python 标准库 + ``utils.log_utils``。**不**依赖 db / llm /
prompts / processors 内部模块——纯工具函数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors._format_helpers")


# ---------------------------------------------------------------------------
# 报错截断
# ---------------------------------------------------------------------------


def truncate_error_log(
    text: Optional[str],
    max_lines: int,
) -> Tuple[str, int]:
    """按行截断报错日志，便于喂给 LLM 重试。

    Args:
        text: 原始 stdout/stderr 文本；``None`` 或空字符串时返回 ``("", 0)``。
        max_lines: 最大保留行数。``<= 0`` 表示不截断（直接返回原文 + length）。

    Returns:
        ``(truncated_text, full_length)`` —— 截断后的字符串 + 原始字符数。
        当行数 ≤ ``max_lines`` 时 ``truncated_text == text.rstrip()``。
        否则在末尾追加 ``"... (N more lines)"``。
    """
    if not text:
        return ("", 0)

    full_length = len(text)
    if max_lines <= 0:
        return (text, full_length)

    lines = text.splitlines()
    if len(lines) <= max_lines:
        return (text.rstrip(), full_length)

    head = "\n".join(lines[:max_lines])
    remaining = len(lines) - max_lines
    return (f"{head}\n... ({remaining} more lines)", full_length)


def extract_key_syntax_error_info(
    combined_err: str,
    max_lines: int,
) -> Tuple[str, int]:
    """P10: 从 OJ/RAC 语法错误输出中提取关键信息，过滤无用噪音。

    提取规则：
    1. 过滤掉 JVM warning（对 LLM 无用）
    2. 过滤掉 stderr 分隔符
    3. 过滤掉 JAVA_TOOL_OPTIONS
    4. 保留错误行 + 代码行 + 标记行 + 汇总行

    Args:
        combined_err: stdout + stderr 拼接的完整错误输出
        max_lines: 最大保留行数

    Returns:
        (truncated_text, full_length) 与 truncate_error_log 保持一致
    """
    if not combined_err:
        return "", 0

    lines = combined_err.split("\n")
    full_length = len(lines)

    result_lines: list[str] = []
    summary_line: str = ""

    for line in lines:
        # 检测汇总行 (N errors)
        m = re.match(r"^(\d+)\s+errors?$", line.strip())
        if m:
            summary_line = line.strip()
            continue

        # 过滤 JVM warning (OpenJDK 或 Java HotSpot)
        if "warning:" in line and ("OpenJDK" in line or "Java HotSpot" in line):
            continue

        # 过滤 stderr 分隔符
        if line.startswith("=== stderr ==="):
            continue

        # 过滤 JAVA_TOOL_OPTIONS
        if "JAVA_TOOL_OPTIONS" in line:
            continue

        # 保留其他行（错误行、代码行、标记行）
        result_lines.append(line)

    # 计算截断（为汇总行预留 2 行：空行 + 汇总）
    reserved_for_summary = 2 if summary_line else 0
    max_content_lines = max_lines - reserved_for_summary

    if len(result_lines) > max_content_lines:
        result_lines = result_lines[:max_content_lines]
        result_lines.append(f"... (截断)")

    # 添加汇总行
    if summary_line:
        result_lines.append("")
        result_lines.append(summary_line)

    truncated_text = "\n".join(result_lines)
    return truncated_text, full_length


def extract_key_pbt_error_info(
    combined_err: str,
    max_lines: int,
) -> Tuple[str, int]:
    """P10: 从 PBT 编译/运行错误输出中提取关键信息，过滤无用噪音。

    提取规则：
    1. 过滤掉 JVM warning
    2. 过滤掉 stdout/stderr 分隔符
    3. 过滤掉 JAVA_TOOL_OPTIONS
    4. 保留异常信息和关键 stack trace 行

    Args:
        combined_err: stdout + stderr 拼接的完整错误输出
        max_lines: 最大保留行数

    Returns:
        (truncated_text, full_length) 与 truncate_error_log 保持一致
    """
    if not combined_err:
        return "", 0

    lines = combined_err.split("\n")
    full_length = len(lines)

    result_lines: list[str] = []

    for line in lines:
        # 过滤 JVM warning (OpenJDK 或 Java HotSpot)
        if "warning:" in line and ("OpenJDK" in line or "Java HotSpot" in line):
            continue

        # 过滤 stdout/stderr 分隔符
        if line.startswith("=== stdout ===") or line.startswith("=== stderr ==="):
            continue

        # 过滤 JAVA_TOOL_OPTIONS
        if "JAVA_TOOL_OPTIONS" in line:
            continue

        # 过滤 "Picked up JAVA_TOOL_OPTIONS"
        if line.startswith("Picked up JAVA_TOOL_OPTIONS"):
            continue

        result_lines.append(line)

    # 截断
    if len(result_lines) > max_lines:
        result_lines = result_lines[:max_lines]
        result_lines.append(f"... (截断)")

    truncated_text = "\n".join(result_lines)
    return truncated_text, full_length


# ---------------------------------------------------------------------------
# 失败样本（PBT postcondition）格式化
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailureSample:
    """单条 PBT 失败样本（postcondition 违反）。

    Attributes:
        clause: 违反的 JML 子句文本（如 ``//@ ensures \\result >= 0;``）。
        sample_input: 输入参数文本（如 ``[3, -1, 4]``）。
        sample_output: 方法返回值文本（如 ``-1``）；void 方法可为 ``""``。
        error_message: RAC 抛出的 ``JmlAssertionError`` 完整 message。
    """

    clause: str
    sample_input: str
    sample_output: str
    error_message: str


def format_failure_samples(
    samples_by_clause: Mapping[str, Sequence[Any]],
    *,
    max_chars: Optional[int] = None,
    max_samples_per_clause: Optional[int] = None,
) -> str:
    """按 JML 子句分类聚合失败样本，输出给 LLM 看的字符串。

    输入格式（来自 PBT TestDriver 输出 JSON）::

        {
            "clause1": [
                {"sample_input": "...", "sample_output": "...", "error_message": "..."},
                ...
            ],
            "clause2": [...],
        }

    输出格式::

        违反子句 [count=8]:
        //@ ensures \\result >= 0;
        样本 1:
          输入: [3, -1, 4]
          返回: -1
          报错: JmlAssertionError: ...
        样本 2:
          ...

        违反子句 [count=2]:
        //@ ensures \\result <= a + b;
        ...

    Args:
        samples_by_clause: 子句 → 样本列表（样本可以是 ``FailureSample`` 实例
            或 ``dict`` 含 ``sample_input`` / ``sample_output`` / ``error_message``
            键，也接受字符串视为已格式化的样本文本）。
        max_chars: 总输出字符数上限；超出时截到上限并附 ``... (truncated)``。
            ``None`` 表示不限。
        max_samples_per_clause: 每个子句最多保留几个样本；``None`` 表示不限。

    Returns:
        格式化后的字符串。空输入返回 ``""``。
    """
    if not samples_by_clause:
        return ""

    sections: List[str] = []
    for clause, samples in samples_by_clause.items():
        if not samples:
            continue
        count = len(samples)
        chosen = (
            samples[:max_samples_per_clause]
            if max_samples_per_clause is not None
            else list(samples)
        )
        section_lines: List[str] = [
            f"违反子句 [count={count}]:",
            clause.strip() if isinstance(clause, str) else str(clause),
        ]
        for idx, sample in enumerate(chosen, start=1):
            section_lines.append(f"样本 {idx}:")
            section_lines.append(_format_one_sample(sample))
        sections.append("\n".join(section_lines))

    out = "\n\n".join(sections)
    if max_chars is not None and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n... (truncated)"
    return out


def _format_one_sample(sample: Any) -> str:
    """单条样本格式化（处理 dict / FailureSample / str 三种输入）。"""
    if isinstance(sample, FailureSample):
        return _render_sample_fields(
            sample.sample_input,
            sample.sample_output,
            sample.error_message,
        )
    if isinstance(sample, Mapping):
        return _render_sample_fields(
            str(sample.get("sample_input", "")),
            str(sample.get("sample_output", "")),
            str(sample.get("error_message", "")),
        )
    if isinstance(sample, str):
        # 已格式化过的样本文本，按行缩进
        return "\n".join(f"  {line}" for line in sample.splitlines())
    return f"  {sample!r}"


def _render_sample_fields(
    sample_input: str,
    sample_output: str,
    error_message: str,
) -> str:
    """3 个字段渲染成缩进段。空字段保留行并留 ``(none)``。"""
    return (
        f"  输入: {sample_input or '(none)'}\n"
        f"  返回: {sample_output or '(void)'}\n"
        f"  报错: {error_message or '(none)'}"
    )


# ---------------------------------------------------------------------------
# 有效输入统计 + precondition 样本格式化（IG_U_Fail_ValidInput 模板用）
# ---------------------------------------------------------------------------


def format_valid_input_stats(stats: Mapping[str, Any]) -> str:
    """格式化 PBT 统计数据，给 LLM 看（输入生成器质量反馈）。

    Args:
        stats: 字典，常用键：
            - ``total_runs`` (int): 总尝试次数
            - ``valid_inputs`` (int): 有效输入数（通过前置条件并运行的）
            - ``precondition_reject_count`` (int): 被前置条件拒绝的次数
            - ``gen_error_count`` (int): 生成器自身抛异常的次数
            - ``valid_ratio`` (float, optional): 有效输入比例

    Returns:
        多行字符串，每行 ``key: value``。
    """
    if not stats:
        return ""

    fields: List[Tuple[str, str]] = []
    keys_in_order = (
        "total_runs",
        "valid_inputs",
        "precondition_reject_count",
        "gen_error_count",
        "valid_ratio",
    )
    label_map: Dict[str, str] = {
        "total_runs": "总运行次数",
        "valid_inputs": "有效输入数",
        "precondition_reject_count": "前置条件拒绝次数",
        "gen_error_count": "生成器异常次数",
        "valid_ratio": "有效输入比例",
    }

    for key in keys_in_order:
        if key in stats:
            label = label_map[key]
            val = stats[key]
            if key == "valid_ratio" and isinstance(val, (int, float)):
                fields.append((label, f"{val:.4f}"))
            else:
                fields.append((label, str(val)))

    # 其余未列入的键照原样附后
    for key, val in stats.items():
        if key not in label_map:
            fields.append((key, str(val)))

    return "\n".join(f"{label}: {value}" for label, value in fields)


def format_precondition_samples(
    samples: Sequence[Any],
    *,
    max_samples: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> str:
    """格式化 precondition 违反样本（给 LLM 看，让它知道怎么生成更合规的输入）。

    Args:
        samples: 样本列表，每项可以是 ``dict``（含 ``sample_input`` /
            ``error_message``）或字符串。
        max_samples: 最多保留几个样本；``None`` 表示不限。
        max_chars: 总输出字符数上限；超出时附 ``... (truncated)``。

    Returns:
        格式化字符串。空输入返回 ``""``。
    """
    if not samples:
        return ""

    chosen = list(samples) if max_samples is None else list(samples[:max_samples])
    lines: List[str] = []
    for idx, sample in enumerate(chosen, start=1):
        lines.append(f"样本 {idx}:")
        if isinstance(sample, Mapping):
            lines.append(
                f"  输入: {sample.get('sample_input', '') or '(none)'}"
            )
            err = sample.get("error_message", "")
            if err:
                lines.append(f"  违反前置条件: {err}")
        elif isinstance(sample, str):
            for line in sample.splitlines():
                lines.append(f"  {line}")
        else:
            lines.append(f"  {sample!r}")

    out = "\n".join(lines)
    if max_chars is not None and len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n... (truncated)"
    return out
