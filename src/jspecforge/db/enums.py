"""数据库枚举与字符串常量定义。

严格对齐 docs/design/数据库表字段设计.md。

- 真正的 MySQL ENUM 字段（tasks.status、task_llm_messages.session_type / role）
  使用 ``str + Enum`` 子类，便于 ``value`` 直接进 SQL 参数。
- VARCHAR 字段中具有有限取值的列（current_step / path / failure_stage 等）
  以模块级字符串常量集合形式给出，DAO 内部仅做参数化插入，不做强校验。

oj_attempts.status 的分流来源详见 docs/oj_result_classification_report.md：
退出码 6 是杂糅码，按输出关键词三路分流为 timeout / unsupported_jml / failed_logic。
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class TaskStatus(str, Enum):
    """tasks.status 枚举值。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


class SessionType(str, Enum):
    """task_llm_messages.session_type 枚举值。"""

    TASK_A = "task_a"
    INPUT_GEN = "input_gen"


class MessageRole(str, Enum):
    """task_llm_messages.role 枚举值。"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


# ─────────────────────────────────────────────────────────────
# VARCHAR 字段的已知取值集合（仅用于参考与上层代码引用）
# ─────────────────────────────────────────────────────────────

# tasks.current_step
CURRENT_STEPS: FrozenSet[str] = frozenset(
    {"task_a", "openjml", "rac", "input_gen", "pbt", "final"}
)

# tasks.path（最终成功路径）
SUCCESS_PATHS: FrozenSet[str] = frozenset({"openjml", "rp"})

# tasks.failure_stage
#
# Phase 4 (2026-05-13) 新增 failed_pbt_insufficient_input:
#   PBT 连续/累计 valid_ratio < min_ratio 命中 task_a_insufficient_input_*_max,
#   表明前置条件可能写得过严或题目值域稀疏, 需要单独标记便于后期人工分析。
#
# Phase 4 (2026-05-13) 新增 failed_oj_bug:
#   OJ ESC 已通过语法校验后 RAC 仍编译失败时视为 OpenJML 自身 bug,
#   按错误首行抽 pattern 写入 oj_bug_reports 表; 双阈值任一触发即终止:
#     - 同一 pattern 连续达 retry.rac_error_pattern_max (A=3);
#     - 不区分 pattern 累计达 retry.rac_oj_bug_total_max (B=8)。
#   2026-05-16 泛化: 不再枚举具体错误模式 (原仅 NoSuchMethodError),
#   黑名单跳过 OJ 的路径不计入本表。
#
# Phase 4 (2026-05-14) 新增 failed_pbt_run:
#   PBT TestDriver 运行失败 (exit != 0 / JSON parse 失败 / timeout 等),
#   不再回 input_gen 重试, 直接终止以便后处理区分。
#   具体根因 (timeout / jvm_crash / stdout_parse_failed) 写在 failure_reason。
#
# 历史: exhausted_pbt_compile 在 D.8 (2026-05-03) 引入,
#   表示 PBT TestDriver 编译失败重试上限耗尽, 当时未补充进本集合。
FAILURE_STAGES: FrozenSet[str] = frozenset(
    {
        "exhausted_syntax",
        "exhausted_rounds",
        "exhausted_pbt_compile",
        "failed_pbt_insufficient_input",
        "failed_pbt_run",
        "failed_oj_bug",
    }
)

# oj_attempts.status
#
# 含义对照（详见 docs/oj_result_classification_report.md §三 / 外部工具调用技术规格 §3.4）：
#   - "pass":              OJ ESC 验证通过（退出码 0）
#   - "failed_syntax":     OJ 语法/解析错误（退出码 1，含 \count / \big_or 等 OJ 不认识的 token）
#   - "failed_logic":      OJ 已成功证明规约与代码矛盾（退出码 6 + 无 timeout/unsupported 关键词 + 耗时未触兜底）
#   - "timeout":           OJ 求解器超时（OPENJML_TIMEOUT 含耗时兜底；HARNESS_TIMEOUT exit=-1）
#   - "unsupported_jml":   OJ 解析后发现 JML 含不支持的功能（退出码 6 + "Not yet supported feature" 关键词）
#   - "skipped_blacklist": JML 命中预置/动态黑名单，跳过 OJ
#
# 注意：OpenJML 不会自然返回退出码 5；不要写 ``if exit_code == 5`` 分支。
OJ_STATUSES: FrozenSet[str] = frozenset(
    {
        "pass",
        "failed_syntax",
        "failed_logic",
        "timeout",
        "unsupported_jml",
        "skipped_blacklist",
    }
)

# oj_attempts.action_taken
#
# enter_rp 同时覆盖 timeout / unsupported_jml 两种来源，不再细分；
# 根因区分通过 status 字段。
OJ_ACTIONS: FrozenSet[str] = frozenset(
    {"success_oj", "retry_syntax", "retry_logic", "enter_rp", "skipped_to_rp"}
)

# rac_attempts.status
RAC_STATUSES: FrozenSet[str] = frozenset({"pass", "failed"})

# rac_attempts.action_taken
RAC_ACTIONS: FrozenSet[str] = frozenset({"continue_to_pbt", "retry_syntax"})

# pbt_attempts.action_taken
#
# Phase 4 (2026-05-13) 重整:
#   - 旧值 retry_insufficient_input 改为回 task_a 的语义 (不再回 input_gen),
#     所以新增 feedback_insufficient_input 作为该路径的 action 值。
#   - 旧值 terminated_input_gen_max / terminated_input_gen_per_round_max 不再产生 (废弃)。
#   - 新增 terminated_insufficient_input_max: PBT 有效输入不足上限触发 final。
PBT_ACTIONS: FrozenSet[str] = frozenset(
    {
        "pbt_pass_retry",
        "retry_post_fail",
        "feedback_insufficient_input",
        "terminated_insufficient_input_max",
        "terminated",
    }
)

# task_llm_messages.compile_status (input_gen assistant 专用)
COMPILE_STATUSES: FrozenSet[str] = frozenset({"pass", "failed"})
