"""MTProcessor —— 内联变异测试处理器。

P11 MT 反馈循环核心组件：接收 OJ-pass 或 PBT-pass 的候选，执行变异测试，
计算分数，构建存活变异体摘要，并做出路由决策（达标终止 / 达标继续 / MT 反馈
/ 轮次耗尽终止）。

流程：
1. 从 task.last_mt_path 判断走 OJ 还是 RP 路径
2. 将 task.current_jml_java 写入临时 .java 文件
3. 调 MajorRunner.run() 生成变异体
4. 调 mutant_loader.load_mutants() 加载变异体
5. 按路径调 OJPathRunner.verify_all / RPPathRunner.verify_all
6. 调 compute_mutation_score 计算分数
7. 回填 mutation_score 到 DB
8. 路由决策
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.message_dao import MessageDAO
from jspecforge.db.oj_attempt_dao import OJAttemptDAO
from jspecforge.db.pbt_attempt_dao import PBTAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.mt.mutant_loader import MajorMutant, extract_class_name, load_mutants
from jspecforge.mt.oj_path_runner import OJPathRunner
from jspecforge.mt.rp_path_runner import RPPathRunner
from jspecforge.mt.score_calculator import SURVIVED, MutantVerdict, compute_mutation_score
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.tools.major_runner import MajorRunner
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.mt")


class MTInfraError(Exception):
    """MT 基础设施级错误（Major 失败、WSL 问题等）。"""


class MTProcessor(BaseProcessor):
    """内联变异测试处理器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        major_runner: Optional[MajorRunner] = None,
        oj_path_runner: Optional[OJPathRunner] = None,
        rp_path_runner: Optional[RPPathRunner] = None,
        task_dao: type = TaskDAO,
        oj_attempt_dao: type = OJAttemptDAO,
        pbt_attempt_dao: type = PBTAttemptDAO,
        message_dao: type = MessageDAO,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._major = major_runner if major_runner is not None else MajorRunner(config)
        self._oj_runner = oj_path_runner if oj_path_runner is not None else OJPathRunner(config)
        self._rp_runner = rp_path_runner if rp_path_runner is not None else RPPathRunner(config)
        self._task_dao = task_dao
        self._oj_attempt_dao = oj_attempt_dao
        self._pbt_attempt_dao = pbt_attempt_dao
        self._message_dao = message_dao

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        if not task.current_jml_java:
            raise MTInfraError(
                f"Task[{task.id}] MTProcessor 进入时 current_jml_java 为空"
            )
        path = task.last_mt_path
        if path not in ("oj", "rp"):
            raise MTInfraError(
                f"Task[{task.id}] MTProcessor 进入时 last_mt_path={path!r}，应为 'oj' 或 'rp'"
            )

        a_round_for_attempt = max(task.a_round - 1, 0)
        class_name = extract_class_name(task.current_jml_java)

        # 准备工作目录
        work_dir = (
            Path(self._config.paths.tmp_dir)
            / "mt_work"
            / f"{task.id}_{a_round_for_attempt}_{path}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)

        # 写源文件
        source_file = work_dir / f"{class_name}.java"
        source_file.write_text(task.current_jml_java, encoding="utf-8", newline="\n")

        # Major 生成变异体
        major_output = work_dir / "major_out"
        major_output.mkdir(parents=True, exist_ok=True)
        major_result = self._major.run(
            source_files=[str(source_file)],
            output_dir=str(major_output),
        )

        if not major_result.success:
            return self._on_major_fail(task, path, a_round_for_attempt, major_result)

        # 加载变异体
        max_mutants = self._config.mt.max_mutants_per_method
        mutants, _budget = load_mutants(major_output, class_name, max_mutants)
        if not mutants:
            return self._on_no_mutants(task, path, a_round_for_attempt)

        # 按路径验证
        if path == "oj":
            oj_result = self._oj_runner.verify_all(mutants)
            verdicts = oj_result.verdicts
        else:
            rp_result = self._rp_runner.verify_all(
                mutants,
                target_class_name=class_name,
                method_signature_text=task.method_code or "",
                input_generator_code=task.current_input_gen_code or "",
                work_dir=work_dir,
                candidate_index=0,
            )
            verdicts = rp_result.verdicts

        # 计算分数
        score, killed, survived, _invalid = compute_mutation_score(verdicts)

        # 构建存活摘要（既写库也喂下轮 LLM）
        feedback_text = self._build_survived_summary(
            mutants, verdicts, self._config.mt.feedback_max_survived
        )

        # 回填 DB（score + MT 4 字段 detail）
        self._backfill_score(task, path, a_round_for_attempt, score)
        self._backfill_mt_detail(
            task,
            path,
            a_round_for_attempt,
            killed=killed,
            survived=survived,
            total=killed + survived,
            feedback_text=feedback_text,
        )

        # 路由决策
        return self._decide_route(
            task, score, killed, survived, path, mutants, verdicts,
            feedback_text=feedback_text,
        )


    # ------------------------------------------------------------------
    # Major 失败兜底
    # ------------------------------------------------------------------

    def _on_major_fail(
        self, task: Task, path: str, a_round: int, major_result: Any
    ) -> ProcessResult:
        _logger.warning(
            "Major 变异体生成失败 (task_id=%d, exit=%d): major_compile_failed",
            task.id,
            major_result.tool_result.exit_code,
        )
        self._backfill_score(task, path, a_round, -1.0)
        self._update_message_action(task, "major_compile_failed")
        task.pending_termination = "failed_major_compile"
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic="Major 变异体生成失败 → final",
        )

    def _on_no_mutants(
        self, task: Task, path: str, a_round: int
    ) -> ProcessResult:
        _logger.warning(
            "Major 输出 0 个变异体 (task_id=%d): mutation_score=1.0 (无可变异点)", task.id
        )
        self._backfill_score(task, path, a_round, 1.0)
        self._update_message_action(task, "major_no_mutants")
        task.pending_termination = "failed_major_compile"
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic="Major 输出 0 个变异体 → final",
        )

    # ------------------------------------------------------------------
    # 路由决策
    # ------------------------------------------------------------------

    def _decide_route(
        self,
        task: Task,
        score: float,
        killed: int,
        survived: int,
        path: str,
        mutants: List[MajorMutant],
        verdicts: List[MutantVerdict],
        *,
        feedback_text: str,
    ) -> ProcessResult:
        cfg_mt = self._config.mt
        threshold = cfg_mt.score_threshold
        terminate_on_threshold = cfg_mt.terminate_on_threshold

        if score == -1.0:
            self._update_message_action(task, f"mt_invalid_{path}")
            task.pending_termination = "failed_major_compile"
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL, task=task,
                diagnostic=f"MT score=-1 → final",
            )

        path_label = "oj" if path == "oj" else "rp"

        if score >= threshold:
            action = f"mt_pass_{path_label}"
            self._update_message_action(task, action)

            # 达标也消耗一轮 MT 计数
            self._increment_feedback_round(task, path)

            # P13: RP 路径 MT 达标 ≠ 任务成功。RP 答案是降级/低可信结果,
            # 达标只能说明"这个 RP 候选可接受", 不应终止任务。
            # 关闭本任务 RP 通道, 回 task_a 继续逼 OJ(后续 OJ 失败回 task_a
            # 而非进 RP)。OJ 路径达标则保持原终止逻辑(金答案, 可直接成功)。
            if path == "rp":
                task.rp_disabled = True
                self._task_dao.update_rp_disabled(self._conn, task.id, True)
                task.last_mt_feedback_text = feedback_text
                task.last_mt_path = path
                task.last_mt_score = score
                task.last_mt_killed = killed
                task.last_mt_survived = survived
                task.last_mt_total = killed + survived
                self._task_dao.update_step(self._conn, task.id, "task_a")
                self._conn.commit()
                return ProcessResult(
                    next_stage=NEXT_TASK_A, task=task,
                    diagnostic=(
                        f"MT score={score:.4f} >= {threshold} (RP 路径达标)"
                        f" → 关闭 RP 通道, 继续逼 OJ"
                    ),
                )

            # OJ 路径达标
            if terminate_on_threshold:
                task.pending_termination = None
                self._task_dao.update_step(self._conn, task.id, "final")
                self._conn.commit()
                return ProcessResult(
                    next_stage=NEXT_FINAL, task=task,
                    diagnostic=f"MT score={score:.4f} >= {threshold} → 达标终止",
                )

            # 达标继续：检查轮次是否耗尽
            if self._rounds_exhausted(task, path):
                term_reason = f"terminated_mt_{path_label}_max"
                task.pending_termination = term_reason
                self._update_message_action(task, term_reason)
                self._task_dao.update_step(self._conn, task.id, "final")
                self._conn.commit()
                return ProcessResult(
                    next_stage=NEXT_FINAL, task=task,
                    diagnostic=f"MT 达标但轮次耗尽 → final ({term_reason})",
                )

            # 达标继续且轮次未耗尽 → 回 task_a
            task.last_mt_feedback_text = feedback_text
            task.last_mt_path = path
            task.last_mt_score = score
            task.last_mt_killed = killed
            task.last_mt_survived = survived
            task.last_mt_total = killed + survived
            self._task_dao.update_step(self._conn, task.id, "task_a")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_TASK_A, task=task,
                diagnostic=f"MT score={score:.4f} >= {threshold} 达标继续 → task_a",
            )

        # score < threshold
        if self._rounds_exhausted(task, path):
            term_reason = f"terminated_mt_{path_label}_max"
            action = term_reason
            self._update_message_action(task, action)
            task.pending_termination = term_reason
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL, task=task,
                diagnostic=f"MT score={score:.4f} < {threshold} 轮次耗尽 → final",
            )

        # MT 反馈：消耗一轮
        self._increment_feedback_round(task, path)
        action = f"mt_feedback_{path_label}"
        self._update_message_action(task, action)

        task.last_mt_feedback_text = feedback_text
        task.last_mt_path = path
        task.last_mt_score = score
        task.last_mt_killed = killed
        task.last_mt_survived = survived
        task.last_mt_total = killed + survived
        self._task_dao.update_step(self._conn, task.id, "task_a")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_TASK_A, task=task,
            diagnostic=f"MT score={score:.4f} < {threshold} → MT 反馈 → task_a",
        )


    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _backfill_score(
        self, task: Task, path: str, a_round: int, score: float
    ) -> None:
        if path == "oj":
            self._oj_attempt_dao.update_mutation_score(
                self._conn, task.id, a_round, score
            )
        else:
            ig_round = self._get_latest_ig_round(task.id, a_round)
            self._pbt_attempt_dao.update_mutation_score_by_round(
                self._conn, task.id, a_round, ig_round, score
            )

    def _backfill_mt_detail(
        self,
        task: Task,
        path: str,
        a_round: int,
        *,
        killed: int,
        survived: int,
        total: int,
        feedback_text: str,
    ) -> None:
        if path == "oj":
            self._oj_attempt_dao.update_mt_detail(
                self._conn, task.id, a_round,
                killed=killed, survived=survived, total=total,
                feedback_text=feedback_text,
            )
        else:
            ig_round = self._get_latest_ig_round(task.id, a_round)
            self._pbt_attempt_dao.update_mt_detail(
                self._conn, task.id, a_round, ig_round,
                killed=killed, survived=survived, total=total,
                feedback_text=feedback_text,
            )

    def _get_latest_ig_round(self, task_id: int, a_round: int) -> int:
        """获取指定 a_round 下最新的 is_pass=True 的 ig_round。"""
        rows = self._pbt_attempt_dao.list_passing(self._conn, task_id)
        candidates = [r for r in rows if r.get("a_round") == a_round]
        if not candidates:
            return 0
        return max(int(r.get("ig_round", 0)) for r in candidates)

    def _increment_feedback_round(self, task: Task, path: str) -> None:
        if path == "oj":
            task.mt_oj_feedback_round += 1
            self._task_dao.increment_counter(
                self._conn, task.id, "mt_oj_feedback_round"
            )
        else:
            task.mt_rp_feedback_round += 1
            self._task_dao.increment_counter(
                self._conn, task.id, "mt_rp_feedback_round"
            )

    def _rounds_exhausted(self, task: Task, path: str) -> bool:
        cfg_mt = self._config.mt
        if path == "oj":
            return task.mt_oj_feedback_round >= cfg_mt.oj_feedback_max_rounds
        return task.mt_rp_feedback_round >= cfg_mt.rp_feedback_max_rounds

    def _update_message_action(self, task: Task, action_taken: str) -> None:
        if task.current_message_id is None:
            return
        current = self._message_dao.get_by_id(self._conn, task.current_message_id)
        existing_is_oj_pass = (
            None if current is None else current.get("is_oj_pass")
        )
        existing_is_pbt_pass = (
            None if current is None else current.get("is_pbt_pass")
        )
        if isinstance(existing_is_oj_pass, int):
            existing_is_oj_pass = bool(existing_is_oj_pass)
        if isinstance(existing_is_pbt_pass, int):
            existing_is_pbt_pass = bool(existing_is_pbt_pass)
        self._message_dao.update_task_a_result(
            self._conn,
            task.current_message_id,
            is_oj_pass=existing_is_oj_pass,
            is_pbt_pass=existing_is_pbt_pass,
            action_taken=action_taken,
        )

    def _build_survived_summary(
        self,
        mutants: List[MajorMutant],
        verdicts: List[MutantVerdict],
        max_survived: int,
    ) -> str:
        """构建存活变异体摘要字符串，供 MT 反馈提示词使用。"""
        mutant_map = {m.major_id: m for m in mutants}
        survived_entries = [v for v in verdicts if v.verdict == SURVIVED][:max_survived]

        lines: List[str] = []
        for v in survived_entries:
            m = mutant_map.get(v.major_id)
            if m is None:
                continue
            obj = {
                "id": m.major_id,
                "operator": f"{m.operator} ({m.from_code}\u2192{m.to_code})",
                "line": m.line,
                "original": m.from_code,
                "mutant": m.to_code,
            }
            lines.append(json.dumps(obj, ensure_ascii=False))
        return "\n".join(lines)

