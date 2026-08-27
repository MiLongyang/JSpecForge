"""PBTProcessor —— 基于属性的动态测试处理器。

设计依据：

- ``docs/design/核心工作流程设计.md`` §5.4 / §5.5（PBT 子步骤 + 分支判定）
- ``docs/design/外部工具调用技术规格.md`` §5（动态测试驱动规格 + JVM 参数）
- ``docs/design/系统架构与调度设计.md`` §三 PBTProcessor 行
- ``docs/plan/P6-处理器.md`` plan §二 P6e

**职责**（plan §二 P6e 1~7 步）：

1. 用 :func:`build_test_driver` 把 ``task.current_input_gen_code`` 注入
   ``TestDriverTemplate.java`` 模板，生成 ``TestDriver_{task_id}_{a_round}.java``
2. 调 :meth:`PBTRunner.compile_test_driver` 编译
3. 调 :meth:`PBTRunner.run_test_driver` 跑动态测试，传 ``test_runs`` /
   ``early_stop_*`` / ``failure_sample_max`` / ``seed`` 共 5 个参数
4. 解析 stdout JSON（``total_runs`` / ``valid_inputs`` / ``post_fail_count``
   / ``precondition_reject_count`` / ``gen_error_count`` / ``failure_samples``
   / ``failure_counts`` / ``seed``）
5. 写 ``pbt_attempts``（含 ``has_blacklist_syntax`` / ``blacklist_hits``
   重新检测）
6. 用 :meth:`MessageDAO.update_task_a_result` 回填触发本轮的 task_a
   assistant 行的 ``is_pbt_pass`` / ``action_taken``
7. 按 plan §1.4 路由表分流：
   * **100% 通过 + 未触上限** → ``task_a``（继续生成 SMT 友好规约）
   * **100% 通过 + 触上限** → ``final``（已存档候选）
   * **postcondition 失败 + 未触上限** → logic_retry +1 → ``task_a``
   * **postcondition 失败 + 超限** → ``final``
   * **有效输入不足 + 未触上限** → input_gen_retry_total +1 → ``input_gen``
   * **有效输入不足 + 超限** → ``final``
   * **编译失败（罕见）** → syntax 计数 +1 → ``task_a`` 或 ``final``

**关于 ``ig_round``**（``pbt_attempts.ig_round`` 字段）：

由本 processor 在写入前查询同 ``(task_id, a_round)`` 下已有 ``pbt_attempts``
行数得到：第 0 次写入 ``ig_round=0``，第 1 次（同 a_round 的 IG 重试后）
``ig_round=1``，依此类推。

**通过判定**（design §5.5）：

- "100% 通过" = ``post_fail_count == 0`` AND ``valid_ratio >= valid_input_min_ratio``
- "postcondition 失败" = ``post_fail_count > 0``
- "有效输入不足" = ``post_fail_count == 0`` AND ``valid_ratio < valid_input_min_ratio``

``valid_ratio = (total_runs - gen_error_count - precondition_reject_count) / total_runs``

**failure_samples 序列化**（pbt_attempts.failure_samples JSON）：

数据库表字段 §7.1 期望的结构是数组，每条 ``{clause, count, sample_input,
sample_output, error_message}``。本 processor 把 TestDriver 输出的
``failure_samples`` (clause→list of "input=...; error=...") 与
``failure_counts`` (clause→count) 重组成 design 要求的结构。
"""

from __future__ import annotations

import random as _random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.message_dao import MessageDAO
from jspecforge.db.pbt_attempt_dao import PBTAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors._format_helpers import (
    extract_key_pbt_error_info,
    format_failure_samples,
    format_precondition_samples,
    format_valid_input_stats,
    truncate_error_log,
)
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_MT,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.tools.blacklist_runtime import BlacklistRuntime, get_instance
from jspecforge.tools.pbt_runner import PBTRunner
from jspecforge.tools.test_driver_builder import build_test_driver
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.pbt")


# Phase 5+ (2026-05-17): PBT TestDriver 运行超时反馈给 LLM 的预写文本。
# 复用 A_U_Fail_Syntax 模板渠道(挂在 task.last_rac_error 上), 模板开头有
# "SYNTAX ERRORS" 字样, 文本第一段先纠正这一措辞避免 LLM 走偏方向。
# 不引用具体子句(decreases/loop_invariant/quantifier 等)以兼容未来未知超时根因。
_TIMEOUT_RETRY_FEEDBACK = (
    "NOTE: Despite the message above mentioning \"SYNTAX ERRORS\", the JML "
    "you wrote actually compiled correctly under both OpenJML ESC and RAC. "
    "The real problem is that the runtime check TIMED OUT before any test "
    "result was produced. This is typically an OpenJML/RAC instrumentation "
    "overhead issue, not a logic bug in your specification.\n\n"
    "The system prompt's \"Runtime arithmetic safety\" rules already explain "
    "why this happens: at runtime, every JML expression (requires, ensures, "
    "loop_invariant, decreases, assert, and any sub-expression in them) is "
    "re-evaluated using Java native arithmetic on every check point. Clauses "
    "attached to hot constructs (loops, frequently-called helpers) or "
    "sub-expressions that iterate / quantify can dominate runtime.\n\n"
    "Please rewrite the JML so the semantics are preserved but the runtime "
    "cost drops. Useful moves:\n"
    "- replace expensive sub-expressions with cheaper but logically "
    "equivalent ones;\n"
    "- tighten `requires` to exclude boundary inputs that blow up the cost;\n"
    "- if a clause is not actually needed for correctness (for example an "
    "extra loop_invariant that mainly helps ESC, not behavior), drop it.\n\n"
    "Do NOT weaken the postcondition or output a vacuous / trivially-true "
    "specification just to dodge the timeout. The goal is a meaningful "
    "specification that is also cheap to check at runtime."
)


class PBTInfraError(Exception):
    """PBT 基础设施级错误（缺前置 / Runner 异常 / 模板问题等）。"""


class PBTProcessor(BaseProcessor):
    """PBT 动态测试处理器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        pbt_runner: Optional[PBTRunner] = None,
        task_dao: type = TaskDAO,
        pbt_attempt_dao: type = PBTAttemptDAO,
        message_dao: type = MessageDAO,
        blacklist_runtime: Optional[BlacklistRuntime] = None,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._runner = pbt_runner if pbt_runner is not None else PBTRunner(config)
        self._task_dao = task_dao
        self._pbt_attempt_dao = pbt_attempt_dao
        self._message_dao = message_dao
        self._blacklist_runtime = blacklist_runtime

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        if not task.current_jml_java:
            raise PBTInfraError(
                f"Task[{task.id}] PBTProcessor 进入时 current_jml_java 为空"
            )
        if not task.current_input_gen_code:
            raise PBTInfraError(
                f"Task[{task.id}] PBTProcessor 进入时 current_input_gen_code 为空"
            )
        if not task.rac_class_name:
            raise PBTInfraError(
                f"Task[{task.id}] PBTProcessor 进入时 rac_class_name 为空"
            )
        if not task.rac_output_dir:
            raise PBTInfraError(
                f"Task[{task.id}] PBTProcessor 进入时 rac_output_dir 为空"
            )

        a_round_for_attempt = max(task.a_round - 1, 0)
        ig_round = self._next_ig_round(task.id, a_round_for_attempt)

        # D.1 黑名单字段写入(2026-05-02): _insert_pbt_attempt 内部会自动从
        # task.current_jml_java 检测黑名单并填字段,所有 PBT attempt 写入
        # 路径都正确反映黑名单状态。task_a_processor 的黑名单分支只写
        # oj_attempts,这里 PBT 自己再 check 一次保证 pbt_attempts 字段一致。

        # 1. 注入 TestDriver
        try:
            build_result = self._build_driver(task, a_round_for_attempt)
        except Exception as exc:  # noqa: BLE001 - 模板/签名解析失败由本类处理
            raise PBTInfraError(
                f"Task[{task.id}] TestDriver 注入失败: {exc}"
            ) from exc

        # 2. 编译
        compile_result = self._runner.compile_test_driver(
            str(build_result.java_path),
            task.rac_output_dir,
            self._pbt_bin_dir(task.id, a_round_for_attempt),
        )
        if not compile_result.success:
            return self._on_compile_fail(
                task, a_round_for_attempt, ig_round, compile_result, build_result
            )

        # 3. 运行
        seed = self._gen_seed()
        program_args = [
            str(self._config.pbt.test_runs),
            str(self._config.pbt.early_stop_on_fail).lower(),
            str(self._config.pbt.early_stop_max_fails),
            str(self._config.pbt.failure_sample_max),
            str(seed),
            # Phase 4 (2026-05-14): 把并行线程数透传给 TestDriver。
            # 模板 args[5] 缺省回退到 1, 所以即便老版 TestDriver 也能容忍此参数。
            str(max(1, int(self._config.pbt.test_driver_threads))),
        ]
        run_result = self._runner.run_test_driver(
            test_driver_class=build_result.class_name,
            pbt_bin_dir=self._pbt_bin_dir(task.id, a_round_for_attempt),
            rac_output_dir=task.rac_output_dir,
            program_args=program_args,
        )
        if not run_result.success or run_result.parsed_json is None:
            return self._on_run_fail(
                task,
                a_round_for_attempt,
                ig_round,
                run_result,
                build_result,
                seed,
            )

        # 4. 解析 + 分流
        return self._on_run_success(
            task,
            a_round_for_attempt,
            ig_round,
            run_result.parsed_json,
            build_result,
            run_result.tool_result.elapsed,
            seed,
        )

    # ------------------------------------------------------------------
    # 路径分支
    # ------------------------------------------------------------------

    def _on_compile_fail(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        compile_result,
        build_result,
    ) -> ProcessResult:
        """TestDriver javac 编译失败（罕见）。视为 syntax 失败。"""
        err_text = (
            compile_result.tool_result.stderr or compile_result.tool_result.stdout or ""
        )
        # P10: PBT 编译错误使用关键信息提取，过滤 JVM warning
        truncated, full_length = extract_key_pbt_error_info(
            err_text,
            self._config.error_log.pbt_compile_error_max_lines,
        )

        # D.1 修复:action_taken 改成 pbt_compile_failed (而不是 terminated/retry_post_fail
        # 这两个语义不准确)。两表写同一个值,语义对齐。
        # has_blacklist_syntax / blacklist_hits 由 _insert_pbt_attempt 自动检测。
        # D.6: detail_truncated / detail_full_length 写入,数据集导出对齐。
        self._insert_pbt_attempt(
            task=task,
            a_round=a_round_for_attempt,
            ig_round=ig_round,
            is_pass=False,
            total_runs=0,
            gen_error_count=0,
            precondition_reject_count=0,
            post_fail_count=0,
            seed=0,
            elapsed_seconds=round(float(compile_result.tool_result.elapsed), 2),
            failure_samples_json=None,
            action_taken="pbt_compile_failed",
            detail_truncated=truncated,
            detail_full_length=full_length,
        )
        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken="pbt_compile_failed"
        )

        # D.8 (2026-05-03): PBT TestDriver 编译失败用独立计数器,
        # 不再共用 task_a_syntax 计数器。失败回 input_gen 阶段(让 LLM
        # 重新生成 generateValidInput 方法), 不回 task_a。
        task.pbt_compile_retry_consecutive += 1
        task.pbt_compile_retry_total += 1
        # D.5 (2026-05-03): PBT TestDriver 编译错误存到独立字段
        # last_pbt_compile_error, 不再混用 last_rac_error。
        # final_processor 据此写"最后一次 PBT TestDriver 编译错误"标签。
        task.last_pbt_compile_error = truncated
        task.last_rac_error = None  # 显式清掉,避免 final 误用
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_pbt_failure_text = None
        self._task_dao.increment_counter(
            self._conn, task.id, "pbt_compile_retry_consecutive"
        )
        self._task_dao.increment_counter(
            self._conn, task.id, "pbt_compile_retry_total"
        )

        retry = self._config.retry
        if (
            task.pbt_compile_retry_consecutive >= retry.pbt_compile_max_consecutive
            or task.pbt_compile_retry_total >= retry.pbt_compile_max_total
        ):
            return self._route_to_final(
                task, "exhausted_pbt_compile", "PBT TestDriver 编译失败超限"
            )
        # D.8: 回 input_gen 阶段让 LLM 重新生成 generate 方法 (不回 task_a)
        return self._route(
            task, NEXT_INPUT_GEN, "input_gen", "PBT 编译失败 → retry input_gen"
        )

    def _on_run_fail(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        run_result,
        build_result,
        seed: int,
    ) -> ProcessResult:
        """TestDriver 运行失败（exit_code != 0 或 JSON 解析失败）。

        Phase 4 (2026-05-14): 不再回 input_gen 重试 — 直接终止任务,
            failure_stage='failed_pbt_run', 根因 (timeout/jvm_crash/
            stdout_parse_failed) 写到 failure_reason 便于后处理区分。
            原因: PBT 失败重试 6 次几乎都是同一个错误, 浪费时间, 后处理
            阶段(导出/统计)更愿意把这种 task 单独标出来。
        D.9: 若检测到 RAC model method 缺失（NoSuchMethodError: String.numsplit 等），
             根因在 JML 规格调用了 JDK 不存在的 model method，回 task_a 重写规格
             (这条分支保留, 与 failed_pbt_run 直接终止互斥)。
        """
        tr = run_result.tool_result
        err_text = self._format_error_text(tr.stdout, tr.stderr)
        # P10: PBT 运行错误使用关键信息提取，过滤 JVM warning
        truncated, full_length = extract_key_pbt_error_info(
            err_text,
            self._config.error_log.pbt_compile_error_max_lines,
        )

        # D.9: 检测 RAC model method 缺失（如 String.numsplit）
        is_model_method_error = self._is_rac_model_method_error(err_text)

        sub_reason = self._classify_pbt_run_failure(
            tr.exit_code, tr.timed_out, run_result.parsed_json
        )
        is_timeout = sub_reason == "timeout"

        # Phase 5+ (2026-05-17): 超时单独走 logic_retry +1 → task_a 路径,
        # 用以 "pbt" 起头的新 action 标签区分(后处理脚本据此筛选)。
        # 非超时 (jvm_crash / stdout_parse_failed / unknown) 沿用旧的
        # pbt_run_failed → failed_pbt_run 直接终止。
        if is_model_method_error:
            attempt_action = "pbt_run_failed"
        elif is_timeout:
            attempt_action = "pbt_run_timeout_retry"
        else:
            attempt_action = "pbt_run_failed"

        self._insert_pbt_attempt(
            task=task,
            a_round=a_round_for_attempt,
            ig_round=ig_round,
            is_pass=False,
            total_runs=0,
            gen_error_count=0,
            precondition_reject_count=0,
            post_fail_count=0,
            seed=seed,
            elapsed_seconds=round(float(tr.elapsed), 2),
            failure_samples_json=None,
            action_taken=attempt_action,
            detail_truncated=truncated,
            detail_full_length=full_length,
        )
        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken=attempt_action
        )

        # D.9: RAC model method 错误回 task_a（换 input_gen 无效，需重写规格）
        if is_model_method_error:
            return self._route_model_method_error(task, truncated)

        # Phase 5+ (2026-05-17): 超时 → logic_retry +1 → task_a 重写
        if is_timeout:
            return self._route_run_timeout_retry(
                task, tr.exit_code, float(tr.elapsed)
            )

        # Phase 4 (2026-05-14): 直接终止 → failed_pbt_run
        # last_pbt_compile_error 沿用独立字段, final_processor 据此写
        # "最后一次 PBT TestDriver 编译错误" 标签 (PBT 运行 != 编译, 但
        # 同字段语义兼容, 待未来重命名)。
        task.last_pbt_compile_error = truncated
        task.last_rac_error = None
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_pbt_failure_text = None

        task.last_pbt_run_failure_reason = sub_reason
        return self._route_to_final(
            task,
            "failed_pbt_run",
            f"PBT TestDriver 运行失败 ({sub_reason}, exit={tr.exit_code}, "
            f"elapsed={tr.elapsed:.1f}s)",
        )

    @staticmethod
    def _classify_pbt_run_failure(
        exit_code: int,
        timed_out: bool,
        parsed_json: Optional[Dict[str, Any]],
    ) -> str:
        """归类 PBT 运行失败的子原因, 写入 failure_reason 便于后处理。

        - ``timeout``: Python 层超时打断 (exit_code=-1)
        - ``jvm_crash``: 子进程退出码非 0 且非超时
        - ``stdout_parse_failed``: 退出码 0 但 stdout JSON 解析失败
        - ``unknown``: 其它意外组合
        """
        if timed_out or exit_code == -1:
            return "timeout"
        if exit_code != 0:
            return "jvm_crash"
        if parsed_json is None:
            return "stdout_parse_failed"
        return "unknown"

    @staticmethod
    def _is_rac_model_method_error(err_text: str) -> bool:
        """检测是否为 RAC model method 缺失错误。

        OpenJML 的 specs/ 中声明的 model method（如 String.numsplit）
        在标准 JDK 中不存在，RAC 编译通过但运行时报 NoSuchMethodError。

        Args:
            err_text: TestDriver stderr + stdout 合并文本。

        Returns:
            True 表示检测到 model method 错误。
        """
        lowered = err_text.lower()
        # 关键模式：NoSuchMethodError + "Skipping a specification clause because it contains an uncompiled model method"
        # 或 NoSuchMethodError + String 的已知 model method
        if "nosuchmethoderror" not in lowered:
            return False
        if "uncompiled model method" in lowered:
            return True
        # 常见的 OpenJML model method
        model_methods = (
            "numsplit",
            "string.numsplit",
            "java.lang.string.numsplit",
        )
        return any(mm in lowered for mm in model_methods)

    def _route_model_method_error(
        self,
        task: Task,
        truncated_error: str,
    ) -> ProcessResult:
        """RAC model method 错误 → logic_retry +1 → task_a。

        这类错误根因是 JML 规格调用了 JDK 不存在的 model method，
        换 input_gen 无效，必须回 task_a 让 LLM 重写规格。
        """
        retry = self._config.retry
        will_terminate = (task.task_a_logic_retry + 1) >= retry.task_a_logic_max
        action = "terminated_logic_max" if will_terminate else "retry_rac_model_method"

        # 更新 message 的 action_taken
        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken=action
        )

        task.task_a_logic_retry += 1
        task.last_pbt_compile_error = truncated_error
        task.last_rac_error = None
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_pbt_failure_text = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_logic_retry"
        )

        if will_terminate:
            return self._route_to_final(
                task, "exhausted_rounds", "RAC model method 错误超限"
            )
        return self._route(
            task, NEXT_TASK_A, "task_a",
            f"RAC model method 错误 → retry task_a (error preview: {truncated_error[:100]})"
        )

    def _route_run_timeout_retry(
        self,
        task: Task,
        exit_code: int,
        elapsed: float,
    ) -> ProcessResult:
        """PBT TestDriver 运行超时 → logic_retry +1 → task_a。

        Phase 5+ (2026-05-17): 超时本质上是 RAC 子句运行时开销过大,
        而非 LLM 规格逻辑错。stdout/stderr 通常被 kill 时没来得及刷出,
        机械提取无东西可喂, 因此走预写文本 (_TIMEOUT_RETRY_FEEDBACK)
        塞进 task.last_rac_error, 复用 A_U_Fail_Syntax 模板渠道,
        模板第一段提示词会先纠正 \"SYNTAX ERRORS\" 措辞。
        """
        retry = self._config.retry
        will_terminate = (task.task_a_logic_retry + 1) >= retry.task_a_logic_max
        action = (
            "terminated_logic_max" if will_terminate else "pbt_run_timeout_retry"
        )

        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken=action
        )

        task.task_a_logic_retry += 1
        task.last_rac_error = _TIMEOUT_RETRY_FEEDBACK
        task.last_pbt_compile_error = None
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_pbt_failure_text = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_logic_retry"
        )

        if will_terminate:
            return self._route_to_final(
                task,
                "exhausted_rounds",
                f"PBT TestDriver 运行超时累计达 logic_retry 上限 "
                f"(exit={exit_code}, elapsed={elapsed:.1f}s)",
            )
        return self._route(
            task, NEXT_TASK_A, "task_a",
            f"PBT TestDriver 运行超时 → retry task_a "
            f"(exit={exit_code}, elapsed={elapsed:.1f}s)",
        )

    def _on_run_success(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        parsed: Dict[str, Any],
        build_result,
        elapsed: float,
        seed: int,
    ) -> ProcessResult:
        """TestDriver 正常返回 JSON。按 design §5.5 分流。"""
        total_runs = int(parsed.get("total_runs", 0))
        valid_inputs = int(parsed.get("valid_inputs", 0))
        post_fail = int(parsed.get("post_fail_count", 0))
        pre_skip = int(parsed.get("precondition_reject_count", 0))
        gen_err = int(parsed.get("gen_error_count", 0))
        # seed 优先用 TestDriver 输出（确保与实际跑的种子一致）
        actual_seed = int(parsed.get("seed", seed))

        valid_ratio = (
            valid_inputs / total_runs if total_runs > 0 else 0.0
        )
        valid_min = self._config.pbt.valid_input_min_ratio

        # 重组 failure_samples → design §7.1 期望结构
        fs_struct = self._build_failure_samples_json(parsed)

        # 通过判定优先级（design §5.5）：postcondition 失败 > 有效输入不足 > 100% 通过
        if post_fail > 0:
            return self._on_postcondition_fail(
                task,
                a_round_for_attempt,
                ig_round,
                parsed,
                fs_struct,
                build_result,
                actual_seed,
                total_runs,
                gen_err,
                pre_skip,
                post_fail,
                elapsed,
            )

        if valid_ratio < valid_min:
            return self._on_insufficient_valid_input(
                task,
                a_round_for_attempt,
                ig_round,
                parsed,
                build_result,
                actual_seed,
                total_runs,
                gen_err,
                pre_skip,
                elapsed,
                valid_ratio,
            )

        return self._on_pbt_pass(
            task,
            a_round_for_attempt,
            ig_round,
            build_result,
            actual_seed,
            total_runs,
            gen_err,
            pre_skip,
            elapsed,
            valid_inputs=valid_inputs,
        )

    def _on_pbt_pass(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        build_result,
        seed: int,
        total_runs: int,
        gen_err: int,
        pre_skip: int,
        elapsed: float,
        valid_inputs: int = 0,
    ) -> ProcessResult:
        """100% 通过：存档候选 → 路由到 MT 评分。"""
        runtime = self._get_blacklist_runtime()
        bl_hit, bl_hits = runtime.check(task.current_jml_java or "")

        action = (
            "pbt_pass_enter_mt"
            if self._config.pipeline_mode == "full"
            else "pbt_pass_final_no_mt"
        )
        new_pbt_pass_count = task.pbt_pass_count + 1

        self._insert_pbt_attempt(
            task=task,
            a_round=a_round_for_attempt,
            ig_round=ig_round,
            is_pass=True,
            total_runs=total_runs,
            gen_error_count=gen_err,
            precondition_reject_count=pre_skip,
            post_fail_count=0,
            seed=seed,
            elapsed_seconds=round(float(elapsed), 2),
            failure_samples_json=[],
            action_taken=action,
            has_blacklist_syntax=bl_hit,
            blacklist_hits=bl_hits,
            full_java_with_jml=task.current_jml_java,
            input_generator_code=task.current_input_gen_code,
            valid_inputs=valid_inputs,
        )
        self._update_message_pbt_result(
            task,
            is_pbt_pass=True,
            action_taken=action,
        )

        task.pbt_pass_count = new_pbt_pass_count
        task.last_pbt_pass_a_round = a_round_for_attempt
        task.last_pbt_failure_text = None
        task.last_pbt_valid_input_stats = None
        task.last_pbt_precondition_text = None
        self._task_dao.increment_counter(
            self._conn, task.id, "pbt_pass_count"
        )
        if task.task_a_insufficient_input_consecutive > 0:
            task.task_a_insufficient_input_consecutive = 0
            self._task_dao.reset_insufficient_input_consecutive(
                self._conn, task.id
            )

        # P13: RP 候选满 → 关闭本任务 RP 通道(不终止), 继续流程逼 OJ。
        # 该候选仍正常进 MT 评分; rp_disabled 对后续轮次的 OJ 失败/黑名单生效
        # (后续 OJ 失败回 task_a 而非进 RP)。
        if task.pbt_pass_count >= self._config.retry.pbt_pass_count_max:
            task.rp_disabled = True
            self._task_dao.update_rp_disabled(self._conn, task.id, True)
            _logger.info(
                "PBT[%s] RP 候选满(%d>=%d) → 关闭 RP 通道, 继续逼 OJ",
                task.id,
                task.pbt_pass_count,
                self._config.retry.pbt_pass_count_max,
            )

        task.last_mt_path = "rp"
        if self._config.pipeline_mode == "full":
            self._task_dao.update_step(self._conn, task.id, "mt")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_MT,
                task=task,
                diagnostic="PBT 100% pass → MT",
            )
        else:
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL,
                task=task,
                diagnostic="PBT 100% pass → Final (no MT)",
            )

    def _on_postcondition_fail(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        parsed: Dict[str, Any],
        fs_struct: List[Dict[str, Any]],
        build_result,
        seed: int,
        total_runs: int,
        gen_err: int,
        pre_skip: int,
        post_fail: int,
        elapsed: float,
    ) -> ProcessResult:
        """postcondition 失败 → logic_retry +1 → task_a 或 final。"""
        runtime = self._get_blacklist_runtime()
        bl_hit, bl_hits = runtime.check(task.current_jml_java or "")

        retry = self._config.retry
        will_terminate = (task.task_a_logic_retry + 1) >= retry.task_a_logic_max
        # D.2 (2026-05-02): terminated_logic_max 而不是 terminated, 让用户能看出
        # 是 logic 重试超限触发的终止
        action = "terminated_logic_max" if will_terminate else "retry_post_fail"

        self._insert_pbt_attempt(
            task=task,
            a_round=a_round_for_attempt,
            ig_round=ig_round,
            is_pass=False,
            total_runs=total_runs,
            gen_error_count=gen_err,
            precondition_reject_count=pre_skip,
            post_fail_count=post_fail,
            seed=seed,
            elapsed_seconds=round(float(elapsed), 2),
            failure_samples_json=fs_struct,
            action_taken=action,
            has_blacklist_syntax=bl_hit,
            blacklist_hits=bl_hits,
            full_java_with_jml=task.current_jml_java,
            input_generator_code=task.current_input_gen_code,
            valid_inputs=int(parsed.get("valid_inputs", 0)),
        )
        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken=action
        )

        task.task_a_logic_retry += 1
        # 给下一轮 TaskA 提供失败样本格式化文本
        # P10 fix: 将 fs_struct（列表格式）转换为 format_failure_samples 期望的字典格式
        # fs_struct: [{"clause": "...", "count": N, "sample_input": "...", ...}, ...]
        # 期望格式: {"clause": [{"sample_input": "...", ...}], ...}
        samples_by_clause: Dict[str, List[Dict[str, str]]] = {}
        for item in fs_struct:
            clause = item.get("clause", "")
            if clause:
                samples_by_clause[clause] = [{
                    "sample_input": item.get("sample_input", ""),
                    "sample_output": item.get("sample_output", ""),
                    "error_message": item.get("error_message", ""),
                }]
        task.last_pbt_failure_text = format_failure_samples(
            samples_by_clause,
            max_chars=self._config.error_log.failure_sample_max_chars,
            max_samples_per_clause=self._config.pbt.failure_sample_max,
        )
        task.last_pbt_valid_input_stats = None
        task.last_pbt_precondition_text = None
        task.last_pbt_pass_a_round = None
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_rac_error = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_logic_retry"
        )
        # Phase 4 (2026-05-13): 后条件失败路径 valid_ratio 也已过阈值,
        # 同样重置 insufficient_input_consecutive(只有真的输入不足才不重置)
        if task.task_a_insufficient_input_consecutive > 0:
            task.task_a_insufficient_input_consecutive = 0
            self._task_dao.reset_insufficient_input_consecutive(
                self._conn, task.id
            )

        if will_terminate:
            return self._route_to_final(task, "exhausted_rounds", "PBT postcondition 失败超限")
        return self._route(task, NEXT_TASK_A, "task_a", "PBT postcondition 失败 → retry task_a")

    def _on_insufficient_valid_input(
        self,
        task: Task,
        a_round_for_attempt: int,
        ig_round: int,
        parsed: Dict[str, Any],
        build_result,
        seed: int,
        total_runs: int,
        gen_err: int,
        pre_skip: int,
        elapsed: float,
        valid_ratio: float,
    ) -> ProcessResult:
        """有效输入不足 → 回 task_a 让 LLM 检查前置条件 或 终止。

        Phase 4 (2026-05-13) 重写:
            原语义: 回 input_gen 重写 generateValidInput, 消耗 input_gen_retry_total。
            新语义: 数据集生成器已离线预生成(高覆盖), 运行期"输入不足"几乎必然
                   意味着 JML 前置条件过严或值域自然稀疏。回 task_a 把统计数据
                   + precondition 样本喂给 LLM, 让它要么放宽 requires, 要么保持
                   原状(后者会在达到 consecutive/total 上限后终止)。
                   使用独立的 task_a_insufficient_input_{consecutive,total} 计数器,
                   与 task_a_logic_retry(PBT 后条件失败)语义解耦。
        """
        runtime = self._get_blacklist_runtime()
        bl_hit, bl_hits = runtime.check(task.current_jml_java or "")

        retry = self._config.retry
        will_terminate_consec = (
            task.task_a_insufficient_input_consecutive + 1
            >= retry.task_a_insufficient_input_consecutive_max
        )
        will_terminate_total = (
            task.task_a_insufficient_input_total + 1
            >= retry.task_a_insufficient_input_total_max
        )
        will_terminate = will_terminate_consec or will_terminate_total
        action = (
            "terminated_insufficient_input_max"
            if will_terminate
            else "feedback_insufficient_input"
        )

        self._insert_pbt_attempt(
            task=task,
            a_round=a_round_for_attempt,
            ig_round=ig_round,
            is_pass=False,
            total_runs=total_runs,
            gen_error_count=gen_err,
            precondition_reject_count=pre_skip,
            post_fail_count=0,
            seed=seed,
            elapsed_seconds=round(float(elapsed), 2),
            failure_samples_json=[],
            action_taken=action,
            has_blacklist_syntax=bl_hit,
            blacklist_hits=bl_hits,
            full_java_with_jml=task.current_jml_java,
            input_generator_code=task.current_input_gen_code,
            valid_inputs=int(parsed.get("valid_inputs", 0)),
        )
        self._update_message_pbt_result(
            task, is_pbt_pass=False, action_taken=action
        )

        task.task_a_insufficient_input_consecutive += 1
        task.task_a_insufficient_input_total += 1
        # 回 task_a 的反馈字段: A_U_Fail_InsufficientInput 模板需要 valid_input_stats + precondition_samples
        task.last_pbt_valid_input_stats = format_valid_input_stats(
            {
                "total_runs": total_runs,
                "valid_inputs": int(parsed.get("valid_inputs", 0)),
                "precondition_reject_count": pre_skip,
                "gen_error_count": gen_err,
                "valid_ratio": valid_ratio,
            }
        )
        precondition_samples = self._collect_precondition_samples(parsed)
        task.last_pbt_precondition_text = format_precondition_samples(
            precondition_samples,
            max_samples=self._config.pbt.failure_sample_max,
            max_chars=self._config.error_log.failure_sample_max_chars,
        )
        task.last_pbt_failure_text = None
        task.last_pbt_pass_a_round = None
        task.last_compile_error = None
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_rac_error = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_insufficient_input_consecutive"
        )
        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_insufficient_input_total"
        )

        if will_terminate:
            return self._route_to_final(
                task,
                "failed_pbt_insufficient_input",
                f"PBT 有效输入不足上限触发(consec={will_terminate_consec}, "
                f"total={will_terminate_total}, valid_ratio={valid_ratio:.3f})",
            )
        return self._route(
            task,
            NEXT_TASK_A,
            "task_a",
            f"PBT 有效输入不足 (ratio={valid_ratio:.3f}) → 回 task_a 检查前置条件",
        )

    # ------------------------------------------------------------------
    # 内部：写入 + 路由 helper
    # ------------------------------------------------------------------

    def _insert_pbt_attempt(
        self,
        *,
        task: Task,
        a_round: int,
        ig_round: int,
        is_pass: bool,
        total_runs: int,
        gen_error_count: int,
        precondition_reject_count: int,
        post_fail_count: int,
        seed: int,
        elapsed_seconds: float,
        failure_samples_json: Optional[List[Dict[str, Any]]],
        action_taken: str,
        has_blacklist_syntax: Optional[bool] = None,
        blacklist_hits: Optional[List[str]] = None,
        full_java_with_jml: Optional[str] = None,
        input_generator_code: Optional[str] = None,
        detail_truncated: Optional[str] = None,
        detail_full_length: int = 0,
        valid_inputs: Optional[int] = None,
    ) -> None:
        # D.1 修复:has_blacklist_syntax / blacklist_hits 默认从 task.current_jml_java
        # 自动检测,确保所有写入 pbt_attempts 的路径都正确反映黑名单状态。
        # 调用方仍可显式覆盖(传 has_blacklist_syntax 参数)。
        if has_blacklist_syntax is None or blacklist_hits is None:
            runtime = self._get_blacklist_runtime()
            auto_hit, auto_hits = runtime.check(task.current_jml_java or "")
            if has_blacklist_syntax is None:
                has_blacklist_syntax = auto_hit
            if blacklist_hits is None:
                blacklist_hits = auto_hits
        # P10 (Phase 2 §3.5): 计算 valid_input_ratio。
        # 公式与 _handle_testdriver_success 的判定公式保持一致:
        #   valid_inputs / total_runs
        # (旧公式 (total_runs - precondition_reject_count)/total_runs 未扣
        #  gen_error_count, 会高估有效输入比例)。
        # total_runs == 0(编译/运行失败) 或 valid_inputs 缺失 → 写 None,无意义。
        valid_input_ratio: Optional[float] = None
        if total_runs > 0 and valid_inputs is not None:
            valid_input_ratio = round(valid_inputs / total_runs, 4)
        self._pbt_attempt_dao.insert(
            self._conn,
            task_id=task.id,
            a_round=a_round,
            ig_round=ig_round,
            full_java_with_jml=full_java_with_jml or (task.current_jml_java or ""),
            input_generator_code=input_generator_code
            or (task.current_input_gen_code or ""),
            is_pass=is_pass,
            total_runs=total_runs,
            seed=seed,
            elapsed_seconds=elapsed_seconds,
            action_taken=action_taken,
            has_blacklist_syntax=has_blacklist_syntax,
            blacklist_hits=blacklist_hits,
            gen_error_count=gen_error_count,
            precondition_reject_count=precondition_reject_count,
            post_fail_count=post_fail_count,
            failure_samples=failure_samples_json,
            detail_truncated=detail_truncated,
            detail_full_length=detail_full_length,
            valid_input_ratio=valid_input_ratio,
        )

    def _update_message_pbt_result(
        self,
        task: Task,
        *,
        is_pbt_pass: bool,
        action_taken: str,
    ) -> None:
        if task.current_message_id is None:
            return
        # 注意：is_oj_pass 在 OJProcessor 已写过；此处仅更新 is_pbt_pass + action_taken
        # 但 update_task_a_result 不支持 partial update（必须传所有字段）
        # 为保持简单，这里重新查一下 is_oj_pass 后再写回——但 message_dao
        # 当前不暴露 get_by_id，我们用 None 表示"保持不变"。
        # 实际：DAO 写法是 SET is_oj_pass=%s, is_pbt_pass=%s, action_taken=%s
        # 传 None 会把 is_oj_pass 置 NULL。为避免这个问题，重写方式：
        # 通过 message_dao.get_by_id 取当前值再写回。
        current = self._message_dao.get_by_id(self._conn, task.current_message_id)
        existing_is_oj_pass = (
            None if current is None else current.get("is_oj_pass")
        )
        # 把 0/1 转回 bool（DAO 存的是 TINYINT(1)）
        if isinstance(existing_is_oj_pass, int):
            existing_is_oj_pass = bool(existing_is_oj_pass)
        self._message_dao.update_task_a_result(
            self._conn,
            task.current_message_id,
            is_oj_pass=existing_is_oj_pass,
            is_pbt_pass=is_pbt_pass,
            action_taken=action_taken,
        )

    def _route(
        self,
        task: Task,
        next_stage: str,
        step_name: str,
        diagnostic: str,
    ) -> ProcessResult:
        self._task_dao.update_step(self._conn, task.id, step_name)
        self._conn.commit()
        return ProcessResult(next_stage=next_stage, task=task, diagnostic=diagnostic)

    def _route_to_final(
        self,
        task: Task,
        failure_stage: Optional[str],
        reason: str,
    ) -> ProcessResult:
        if failure_stage is not None:
            task.pending_termination = failure_stage
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        _logger.info("PBT[%s] 终止: %s（%s）", task.id, failure_stage, reason)
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic=f"terminated: {reason}",
        )

    # ------------------------------------------------------------------
    # 内部：子工具
    # ------------------------------------------------------------------

    def _build_driver(self, task: Task, a_round_for_attempt: int):
        template_path = (
            Path(self._config.paths.template_dir or "") / "TestDriverTemplate.java"
        )
        out_dir = Path(self._config.paths.java_source_dir or "")
        return build_test_driver(
            task_id=task.id,
            a_round=a_round_for_attempt,
            target_class_name=task.rac_class_name or "Solution",
            method_signature_text=task.method_code or "",
            input_generator_code=task.current_input_gen_code or "",
            template_path=template_path,
            output_dir=out_dir,
        )

    def _pbt_bin_dir(self, task_id: int, a_round: int) -> str:
        return str(
            Path(self._config.paths.pbt_bin_dir or "")
            / f"{task_id}_{a_round}"
        )

    def _next_ig_round(self, task_id: int, a_round: int) -> int:
        rows = self._pbt_attempt_dao.list_by_task(self._conn, task_id)
        return sum(1 for r in rows if r.get("a_round") == a_round)

    @staticmethod
    def _gen_seed() -> int:
        return _random.randint(0, 2**31 - 1)

    @staticmethod
    def _format_error_text(stdout: str, stderr: str) -> str:
        parts = []
        if stderr:
            parts.append(stderr.rstrip())
        if stdout:
            parts.append("=== stdout ===")
            parts.append(stdout.rstrip())
        return "\n".join(parts)

    @staticmethod
    def _build_failure_samples_json(
        parsed: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """把 TestDriver 输出的 failure_samples + failure_counts 重组为
        pbt_attempts.failure_samples JSON 期望的结构。

        参考 docs/design/数据库表字段设计.md §七 ``failure_samples`` 结构示例。

        TestDriver 当前输出 (P10 GroupedSample 改造后)::

            {
              "<clause>": [
                {"error": "JmlAssertionError: ...", "inputs": ["[1,2]","[3,4]"], "count": N},
                ...  # 同 clause 下不同错误信息分组
              ],
              ...
            }

        历史(P10 改造前)输出每个 clause 是一组 ``"input=...; error=..."`` 字符串,
        本函数同时兼容两种形态(2026-05-16 hot-fix #33: 早先只解析旧字符串
        形态导致 sample_input/error_message 全空, 喂给 LLM 的反例为空,
        Phase 4 多次 retry_post_fail 收敛失败的根因)。

        每个 dict 输出形态: ``{clause, count, sample_input, sample_output,
        error_message}``。当一组 inputs 含多条时, sample_input 取第一条
        (LLM 看一条具体反例就够 fix 规约), 但保留首条完整的 error_message。
        """
        counts = parsed.get("failure_counts") or {}
        samples = parsed.get("failure_samples") or {}
        result: List[Dict[str, Any]] = []
        for clause, sample_list in samples.items():
            if not sample_list:
                count = int(counts.get(clause, 0))
                result.append(
                    {
                        "clause": clause,
                        "count": count,
                        "sample_input": "",
                        "sample_output": "",
                        "error_message": "",
                    }
                )
                continue

            first = sample_list[0]
            sample_input = ""
            error_message = ""
            group_count = 0

            if isinstance(first, dict):
                # 新格式: {"error": "...", "inputs": [...], "count": N}
                error_message = str(first.get("error", "") or "")
                inputs = first.get("inputs") or []
                if isinstance(inputs, list) and inputs:
                    sample_input = str(inputs[0])
                group_count = int(first.get("count", 0) or 0)
            elif isinstance(first, str):
                # 历史格式: "input=[1, 2]; error=org.jmlspecs.runtime..."
                parts = first.split("; error=", 1)
                sample_input = (
                    parts[0][len("input=") :]
                    if parts[0].startswith("input=")
                    else parts[0]
                )
                if len(parts) >= 2:
                    error_message = parts[1]

            # count 优先用 failure_counts (聚合 clause 级别),
            # 退化到组内 count, 再退化到 list 长度
            count = int(counts.get(clause, group_count or len(sample_list)))
            result.append(
                {
                    "clause": clause,
                    "count": count,
                    "sample_input": sample_input,
                    "sample_output": "",
                    "error_message": error_message,
                }
            )
        return result

    @staticmethod
    def _collect_precondition_samples(
        parsed: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """从 TestDriver JSON 中提取 precondition 样本（设计上 TestDriver
        没单独输出 precondition 样本——只统计 ``precondition_reject_count``）。

        当前实现返回空列表；future_improvements 可让 TestDriver 输出
        ``precondition_rejected_samples``，本方法据此返回真实样本。
        """
        rejected = parsed.get("precondition_rejected_samples") or []
        if not isinstance(rejected, list):
            return []
        out: List[Dict[str, Any]] = []
        for sample in rejected:
            if isinstance(sample, str):
                out.append({"sample_input": sample, "error_message": ""})
            elif isinstance(sample, dict):
                out.append(sample)
        return out

    def _get_blacklist_runtime(self) -> BlacklistRuntime:
        if self._blacklist_runtime is not None:
            return self._blacklist_runtime
        return get_instance()
