"""FinalProcessor —— 最终裁定处理器。

P11 改造后职责：

1. 从 OJ/RP 两个候选池中各选最优（按 mutation_score 降序，回退按 a_round 降序）
2. 有任一候选 → 选 mutation_score 更高者 mark_success；相同则优先 OJ
3. 无候选 → mark_failed
4. 不碰 .jsonl 落盘（导出由 scripts/export_dataset.py 负责）
"""

from __future__ import annotations

from typing import Any, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.oj_attempt_dao import OJAttemptDAO
from jspecforge.db.pbt_attempt_dao import PBTAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors.base import (
    NEXT_DONE,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.final")


class FinalProcessor(BaseProcessor):
    """最终裁定处理器（终态写 DB）。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        task_dao: type = TaskDAO,
        pbt_attempt_dao: type = PBTAttemptDAO,
        oj_attempt_dao: type = OJAttemptDAO,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._task_dao = task_dao
        self._pbt_attempt_dao = pbt_attempt_dao
        self._oj_attempt_dao = oj_attempt_dao

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        oj_best = self._oj_attempt_dao.get_best_passing(self._conn, task.id)
        rp_best = self._pbt_attempt_dao.get_best_passing(self._conn, task.id)

        has_oj = oj_best is not None and oj_best.get("full_java_with_jml")
        has_rp = rp_best is not None and rp_best.get("full_java_with_jml")

        if has_oj or has_rp:
            return self._on_success(task, oj_best if has_oj else None, rp_best if has_rp else None)
        return self._on_failed(task)

    # ------------------------------------------------------------------
    # 成功路径
    # ------------------------------------------------------------------

    def _on_success(
        self,
        task: Task,
        oj_best: Optional[dict],
        rp_best: Optional[dict],
    ) -> ProcessResult:
        if oj_best and rp_best:
            oj_score = oj_best.get("mutation_score") or -1
            rp_score = rp_best.get("mutation_score") or -1
            if oj_score >= rp_score:
                winner, path = oj_best, "openjml"
            else:
                winner, path = rp_best, "rp"
        elif oj_best:
            winner, path = oj_best, "openjml"
        else:
            winner, path = rp_best, "rp"

        final_java = winner["full_java_with_jml"]
        final_a_round = int(winner.get("a_round") or 0)

        task.status = "success"
        task.current_step = None

        self._task_dao.mark_success(
            self._conn,
            task.id,
            path=path,
            final_full_java_with_jml=final_java,
            final_a_round=final_a_round,
            final_selected_candidate_index=None,
        )
        self._conn.commit()

        score_display = winner.get("mutation_score")
        _logger.info(
            "Final[%s] success(%s): a_round=%d, mutation_score=%s",
            task.id, path, final_a_round, score_display,
        )
        return ProcessResult(
            next_stage=NEXT_DONE,
            task=task,
            diagnostic=f"final → success({path}, score={score_display})",
        )

    def _on_failed(self, task: Task) -> ProcessResult:
        """无候选 → failed。"""
        failure_stage = task.pending_termination or "exhausted_rounds"
        failure_reason = self._build_failure_reason(task, failure_stage)
        last_error = self._build_last_error(task)

        task.status = "failed"
        task.current_step = None

        self._task_dao.mark_failed(
            self._conn,
            task.id,
            failure_stage=failure_stage,
            failure_reason=failure_reason,
            last_error_detail=last_error,
        )
        self._conn.commit()
        _logger.info(
            "Final[%s] failed(%s): %s",
            task.id,
            failure_stage,
            failure_reason,
        )
        return ProcessResult(
            next_stage=NEXT_DONE,
            task=task,
            diagnostic=f"final → failed({failure_stage})",
        )

    # ------------------------------------------------------------------
    # 内部：失败信息构造
    # ------------------------------------------------------------------

    @staticmethod
    def _build_failure_reason(task: Task, failure_stage: str) -> str:
        """生成人类可读的 failure_reason 字符串。

        包含 a_round + 4 个核心计数器的最终值。
        """
        if failure_stage == "exhausted_syntax":
            head = "Task A 语法错误重试上限耗尽"
        elif failure_stage == "exhausted_rounds":
            head = "Task A 全局轮次或逻辑/输入生成器重试上限耗尽"
        elif failure_stage == "exhausted_pbt_compile":
            head = "PBT TestDriver 编译/运行重试上限耗尽"
        elif failure_stage == "failed_pbt_insufficient_input":
            head = "PBT 有效输入不足上限耗尽(前置条件可能过严或值域稀疏)"
        elif failure_stage == "failed_oj_bug":
            pattern = task.extra.get("oj_bug_pattern") if task.extra else None
            head = (
                f"RAC 疑似 OpenJML 自身 bug 打标达阈值 (pattern={pattern})"
                if pattern
                else "RAC 疑似 OpenJML 自身 bug 打标达阈值"
            )
        elif failure_stage == "failed_pbt_run":
            sub = getattr(task, "last_pbt_run_failure_reason", None)
            head = (
                f"PBT TestDriver 运行失败终止 ({sub})"
                if sub
                else "PBT TestDriver 运行失败终止"
            )
        elif failure_stage == "failed_major_compile":
            head = "Major 变异体生成失败，无法评估规约强度"
        elif failure_stage == "terminated_mt_oj_max":
            head = "OJ 路径 MT 反馈轮次耗尽，规约未达标"
        elif failure_stage == "terminated_mt_rp_max":
            head = "RP 路径 MT 反馈轮次耗尽，规约未达标"
        else:
            head = f"未知终止原因: {failure_stage}"

        return (
            f"{head}（"
            f"a_round={task.a_round}, "
            f"syntax_consec={task.task_a_syntax_retry_consecutive}, "
            f"syntax_total={task.task_a_syntax_retry_total}, "
            f"logic={task.task_a_logic_retry}, "
            f"pbt_pass={task.pbt_pass_count}, "
            f"ii_consec={task.task_a_insufficient_input_consecutive}, "
            f"ii_total={task.task_a_insufficient_input_total}, "
            f"ig_total={task.input_gen_retry_total}）"
        )[:500]

    @staticmethod
    def _build_last_error(task: Task) -> Optional[str]:
        """从 last_* 字段中挑非空的拼成 last_error_detail。

        优先级:postcondition > pbt_compile > rac > oj > input_gen compile
        """
        parts = []
        if task.last_pbt_failure_text:
            parts.append("=== 最后一次 PBT 失败样本 ===")
            parts.append(task.last_pbt_failure_text)
        # D.5 (2026-05-03): PBT TestDriver 编译错误用独立字段 last_pbt_compile_error,
        # 不再混用 last_rac_error。修复 task 13 的标签错位 bug。
        if task.last_pbt_compile_error:
            parts.append("=== 最后一次 PBT TestDriver 编译错误 ===")
            parts.append(task.last_pbt_compile_error)
        if task.last_rac_error:
            parts.append("=== 最后一次 RAC 编译错误 ===")
            parts.append(task.last_rac_error)
        if task.last_oj_error:
            parts.append(
                f"=== 最后一次 OJ 错误 (label={task.last_oj_label}) ==="
            )
            parts.append(task.last_oj_error)
        if task.last_compile_error:
            parts.append("=== 最后一次 InputGen 编译错误 ===")
            parts.append(task.last_compile_error)
        # Phase 4 (2026-05-13): PBT 有效输入不足触发 failed_pbt_insufficient_input 时,
        # last_pbt_valid_input_stats / last_pbt_precondition_text 非空, 写入便于排查。
        if task.last_pbt_valid_input_stats:
            parts.append("=== 最后一次 PBT 有效输入统计 ===")
            parts.append(task.last_pbt_valid_input_stats)
        if task.last_pbt_precondition_text:
            parts.append("=== 最后一次 PBT precondition 拒绝样本 ===")
            parts.append(task.last_pbt_precondition_text)
        if not parts:
            return None
        return "\n".join(parts)
