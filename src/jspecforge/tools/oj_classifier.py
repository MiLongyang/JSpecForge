"""OpenJML ESC 结果分类与动态黑名单关键字提取（纯函数）。

依据 ``docs/oj_result_classification_report.md`` §3.4 / §四 与 ``docs/design/
外部工具调用技术规格.md`` §3.4。

退出码 6 是 OpenJML 的"杂糅码"，必须按 stdout/stderr 关键词做四级优先分流：

  1. ``aborted proof: timeout`` / ``time or memory limit reached``
     → ``OPENJML_TIMEOUT``
  2. ``not yet supported feature`` / ``not implemented for static checking``
     → ``UNSUPPORTED_JML``（同时正则提取 ``using \\xxx`` 关键词供动态黑名单）
  3. 耗时 ≥ ``timeout_threshold_seconds`` → ``OPENJML_TIMEOUT``（耗时兜底）
  4. 默认 → ``LOGIC_ERROR``

**禁止** ``if exit_code == 5`` 分支：OpenJML 不会自然返回 5（gotchas §4.3）。

label 与 ``oj_attempts.status`` 的映射（参见 enums.py / 技术规格 §3.4）：

  ============================  =============================
  label                         oj_attempts.status
  ============================  =============================
  SUCCESS                       pass
  SYNTAX_ERROR                  failed_syntax
  OPENJML_TIMEOUT               timeout
  HARNESS_TIMEOUT               timeout
  UNSUPPORTED_JML               unsupported_jml
  LOGIC_ERROR                   failed_logic
  INFRA_ERROR                   （不写入 oj_attempts，上抛）
  ============================  =============================
"""

from __future__ import annotations

import re
from typing import Final, FrozenSet, List, Tuple

# 退出码 6 的 timeout 分流关键词（小写匹配）
_TIMEOUT_KEYWORDS: Final[Tuple[str, ...]] = (
    "aborted proof: timeout",
    "time or memory limit reached",
)

# 退出码 6 的 unsupported 分流关键词（小写匹配）
_UNSUPPORTED_KEYWORDS: Final[Tuple[str, ...]] = (
    "not yet supported feature",
    "not implemented for static checking",
)

# label 集合（与 docs §3.4 / §4.4 对齐）
LABELS: Final[FrozenSet[str]] = frozenset(
    {
        "SUCCESS",
        "SYNTAX_ERROR",
        "OPENJML_TIMEOUT",
        "UNSUPPORTED_JML",
        "LOGIC_ERROR",
        "HARNESS_TIMEOUT",
        "INFRA_ERROR",
        "UNKNOWN",
    }
)

# label → oj_attempts.status 映射（INFRA_ERROR 不入库）
LABEL_TO_OJ_STATUS: Final[dict[str, str]] = {
    "SUCCESS": "pass",
    "SYNTAX_ERROR": "failed_syntax",
    "OPENJML_TIMEOUT": "timeout",
    "HARNESS_TIMEOUT": "timeout",
    "UNSUPPORTED_JML": "unsupported_jml",
    "LOGIC_ERROR": "failed_logic",
}

# "using \xxx" 提取正则（关键字以反斜杠开头）
_UNSUPPORTED_KEYWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"using\s+(\\\w+)"
)


def classify_oj_result(
    returncode: int,
    stdout: str,
    stderr: str,
    duration: float,
    timeout_threshold_seconds: float,
) -> Tuple[str, List[str]]:
    """对 OpenJML ESC 结果进行分类。

    Args:
        returncode: 子进程退出码（Python 层超时为 -1，由 ``run_subprocess``
            统一标记）。
        stdout: 子进程标准输出。
        stderr: 子进程标准错误。
        duration: 实际耗时（秒），用于退出码 6 的耗时兜底分流。
        timeout_threshold_seconds: 耗时兜底阈值，来自
            ``config.openjml.timeout_threshold_seconds``（默认 60 秒）。

    Returns:
        ``(label, new_blacklist_keywords)``：

        - ``label`` ∈ ``LABELS``，对应 docs §3.4 的分流标签
        - ``new_blacklist_keywords``：仅 ``UNSUPPORTED_JML`` 时非空，包含
          ``\\xxx`` 形式的字符串（**未做** PROTECTED 过滤，由调用方再过一次）
    """
    # 早期分支：明确退出码（不依赖输出关键词）
    if returncode == 0:
        return "SUCCESS", []
    if returncode == 2:
        return "INFRA_ERROR", []
    if returncode == -1:
        return "HARNESS_TIMEOUT", []
    if returncode == 1:
        return "SYNTAX_ERROR", []

    # 退出码 6：四级优先分流
    if returncode == 6:
        combined_lower = f"{stdout}\n{stderr}".lower()

        # 第 1 步：timeout 关键词（最高优先级）
        if any(kw in combined_lower for kw in _TIMEOUT_KEYWORDS):
            return "OPENJML_TIMEOUT", []

        # 第 2 步：unsupported 关键词（必须在 logic 之前判定，docs §3.2）
        if any(kw in combined_lower for kw in _UNSUPPORTED_KEYWORDS):
            new_keywords = extract_unsupported_keywords(f"{stdout}\n{stderr}")
            return "UNSUPPORTED_JML", new_keywords

        # 第 3 步：耗时兜底（无关键词但耗时长，认定为 OJ 内部慢但未明说超时）
        if duration >= timeout_threshold_seconds:
            return "OPENJML_TIMEOUT", []

        # 第 4 步：默认 = OJ 已成功证明规约与代码矛盾
        return "LOGIC_ERROR", []

    # 其他未知退出码：暂归 UNKNOWN，由调用方决定如何处理（通常上抛）
    return "UNKNOWN", []


def extract_unsupported_keywords(oj_output: str) -> List[str]:
    """从 OpenJML 输出中提取 ``using \\xxx`` 形式的关键词。

    用于 UNSUPPORTED_JML 时扩充动态黑名单（docs/design/核心工作流程设计.md
    §7.4）。提取后由调用方再做 PROTECTED 列表过滤。

    Args:
        oj_output: OpenJML 的 stdout + stderr（推荐传 ``stdout + "\\n" +
            stderr`` 拼接结果）。

    Returns:
        去重后的关键字列表（保持每次调用稳定的输出顺序，便于断言/日志）。
    """
    if not oj_output:
        return []
    matches = _UNSUPPORTED_KEYWORD_PATTERN.findall(oj_output)
    # 用 dict.fromkeys 实现"保持首次出现顺序"的去重
    return list(dict.fromkeys(matches))
