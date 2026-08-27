"""OJProcessor —— OpenJML ESC 静态验证处理器。

设计依据：

- ``docs/design/核心工作流程设计.md`` §四（OJ 分支判定 + 4.4 分支表）
- ``docs/design/外部工具调用技术规格.md`` §3.1 / §3.4（ESC 调用 + 6 类标签分流）
- ``docs/design/系统架构与调度设计.md`` §5.3（OJProcessor.process 伪代码 +
  动态黑名单扩充时机）
- ``docs/plan/P6-处理器.md`` plan §二 P6b

**职责**（plan §二 P6b 1~7 步）：

1. 把 ``task.current_jml_java`` 写到 ``{java_source_dir}/{ClassName}_{task_id}_{a_round}.java``
2. 调 :meth:`OpenJMLRunner.run_esc` 拿原始 :class:`ToolResult`
3. 调 :func:`classify_oj_result` 拿 ``(label, new_blacklist_keywords)``
4. 写 ``oj_attempts``（status / exit_code / elapsed / detail_truncated /
   action_taken）+ 回填触发本轮的 task_a assistant 消息的
   ``is_oj_pass`` / ``action_taken``
5. 按 label 分流：
   * **SUCCESS**：直接 :meth:`TaskDAO.mark_success`，``next_stage="done_oj_pass"``
     （**不**进 final_queue —— design §5.4 说明）
   * **SYNTAX_ERROR**：syntax 计数器 +1，``last_oj_label="SYNTAX_ERROR"`` →
     ``task_a`` 或终止
   * **LOGIC_ERROR**：reset syntax_consecutive → ``rac``（Phase 4 改: 不再
     消耗 task_a_logic_retry 计数, 与 timeout/unsupported 同路进 RP；
     ``last_oj_label="LOGIC_ERROR"`` 仍保留以供 RP 阶段诊断）
   * **OPENJML_TIMEOUT** / **HARNESS_TIMEOUT**：reset syntax_consecutive →
     ``rac``
   * **UNSUPPORTED_JML**：调 :meth:`BlacklistRuntime.update`（动态扩充）→
     ``rac``
   * **INFRA_ERROR**：上抛 :class:`OJInfraError`（不计入计数器）
   * **UNKNOWN**：保守上抛同样的异常（design 未规定，我们按"不可识别状态"
     处理给上层）

**a_round 语义**（与 P6a 对齐）：

进入 OJProcessor 时 ``task.a_round`` 是"已生成的 assistant 数"（即 N）；
对应到 ``oj_attempts.a_round`` 是 ``task.a_round - 1``（即"刚生成的那一轮"）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.message_dao import MessageDAO
from jspecforge.db.oj_attempt_dao import OJAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors._format_helpers import (
    extract_key_syntax_error_info,
    truncate_error_log,
)
from jspecforge.processors.base import (
    NEXT_DONE_OJ_PASS,
    NEXT_FINAL,
    NEXT_MT,
    NEXT_RAC,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.tools.blacklist_runtime import BlacklistRuntime, get_instance
from jspecforge.tools.oj_classifier import LABEL_TO_OJ_STATUS, classify_oj_result
from jspecforge.tools.openjml_tool import OpenJMLRunner
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.openjml")


class OJInfraError(Exception):
    """OpenJML 基础设施级错误（INFRA_ERROR / UNKNOWN exit code）。

    不计入任何计数器；交由 P7 编排器处理（通常重排或失败上报）。
    """


class OJProcessor(BaseProcessor):
    """OpenJML ESC 静态验证处理器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        openjml_runner: Optional[OpenJMLRunner] = None,
        task_dao: type = TaskDAO,
        oj_attempt_dao: type = OJAttemptDAO,
        message_dao: type = MessageDAO,
        blacklist_runtime: Optional[BlacklistRuntime] = None,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._runner = (
            openjml_runner if openjml_runner is not None else OpenJMLRunner(config)
        )
        self._task_dao = task_dao
        self._oj_attempt_dao = oj_attempt_dao
        self._message_dao = message_dao
        self._blacklist_runtime = blacklist_runtime

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        if not task.current_jml_java:
            raise OJInfraError(
                f"Task[{task.id}] OJProcessor 进入时 current_jml_java 为空，"
                "TaskA 未正确传递 JML 结果"
            )

        a_round_for_attempt = max(task.a_round - 1, 0)

        # 1. 写 .java 文件
        java_path = self._write_java_file(task, a_round_for_attempt)

        # 2. 调 OpenJML ESC
        try:
            tool_result = self._runner.run_esc(str(java_path))
        except Exception as exc:  # noqa: BLE001
            raise OJInfraError(
                f"Task[{task.id}] OpenJMLRunner.run_esc 抛异常: {exc}"
            ) from exc

        # 3. 分类
        label, new_blacklist_kws = classify_oj_result(
            tool_result.exit_code,
            tool_result.stdout or "",
            tool_result.stderr or "",
            tool_result.elapsed,
            float(self._config.openjml.timeout_threshold_seconds),
        )

        _logger.info(
            "OJ[%s, a_round=%d] exit=%d elapsed=%.2fs label=%s",
            task.id,
            a_round_for_attempt,
            tool_result.exit_code,
            tool_result.elapsed,
            label,
        )

        # 4. INFRA_ERROR：上抛 InfraError,不计入任何计数器(工具/JVM 真实故障)
        if label == "INFRA_ERROR":
            raise OJInfraError(
                f"Task[{task.id}] OJ {label}: exit={tool_result.exit_code} "
                f"stderr={(tool_result.stderr or '')[:200]}"
            )

        # 5. 截断报错信息
        combined_err = self._format_error_text(
            tool_result.stdout, tool_result.stderr
        )
        truncated_text, full_length = truncate_error_log(
            combined_err,
            self._config.error_log.openjml_max_lines,
        )

        # P10: SYNTAX_ERROR 使用关键信息提取，过滤 JVM warning
        if label == "SYNTAX_ERROR":
            syntax_text, syntax_full_len = extract_key_syntax_error_info(
                combined_err,
                self._config.error_log.openjml_max_lines,
            )
            return self._on_syntax_error(
                task, a_round_for_attempt, tool_result, syntax_text, syntax_full_len
            )

        # UNKNOWN: OpenJML 工具内部 crash (常见: AssertionError / NullPointerException
        # 在 JmlAttr 等阶段抛出),通常是 LLM 生成的某种 JML 触发了 OpenJML bug。
        # 不是基础设施故障,**不能**走 InfraError 死循环路径,而是当 SYNTAX_ERROR
        # 处理: syntax 计数器 +1, 让 task_a 重新生成不同的 JML
        if label == "UNKNOWN":
            _logger.warning(
                "OJ[%s, a_round=%d] UNKNOWN exit=%d (OpenJML 工具崩溃,按 syntax 失败处理)",
                task.id, a_round_for_attempt, tool_result.exit_code,
            )
            unknown_text, unknown_full_len = extract_key_syntax_error_info(
                combined_err,
                self._config.error_log.openjml_max_lines,
            )
            return self._on_syntax_error(
                task, a_round_for_attempt, tool_result, unknown_text, unknown_full_len
            )

        # 6. 按 label 分流
        if label == "SUCCESS":
            return self._on_success(
                task, a_round_for_attempt, tool_result, truncated_text, full_length
            )
        # SYNTAX_ERROR 已在上面提前处理
        if label == "LOGIC_ERROR":
            # P10: 对逻辑错误使用关键信息提取，过滤无用噪音
            logic_text, logic_full_len = self._extract_key_logic_error_info(
                combined_err,
                self._config.error_log.openjml_max_lines,
            )
            if not logic_text:
                logic_text, logic_full_len = truncated_text, full_length
            return self._on_logic_error(
                task, a_round_for_attempt, tool_result, logic_text, logic_full_len
            )
        if label in ("OPENJML_TIMEOUT", "HARNESS_TIMEOUT"):
            return self._on_timeout(
                task,
                a_round_for_attempt,
                label,
                tool_result,
                truncated_text,
                full_length,
            )
        if label == "UNSUPPORTED_JML":
            return self._on_unsupported(
                task,
                a_round_for_attempt,
                tool_result,
                truncated_text,
                full_length,
                new_blacklist_kws,
            )

        # 兜底（不应到达）
        raise OJInfraError(f"Task[{task.id}] 未识别的 OJ label: {label}")

    # ------------------------------------------------------------------
    # 各 label 分支
    # ------------------------------------------------------------------

    def _on_success(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
    ) -> ProcessResult:
        action = "success_oj"
        self._insert_oj_attempt(
            task=task,
            a_round=a_round_for_attempt,
            label="SUCCESS",
            tool_result=tool_result,
            detail_truncated=None,
            detail_full_length=None,
            action_taken=action,
            full_java_with_jml=task.current_jml_java,
        )
        self._update_message_oj_result(task, is_oj_pass=True, action_taken=action)

        if task.task_a_syntax_retry_consecutive > 0:
            task.task_a_syntax_retry_consecutive = 0
            self._task_dao.reset_syntax_consecutive(self._conn, task.id)

        task.has_oj_passed = True
        task.final_jml_for_oj_pass = task.current_jml_java
        task.last_oj_label = "SUCCESS"
        task.last_oj_error = None
        task.last_mt_path = "oj"

        # P13: OJ ESC 通过候选计数 +1, 并检查上限。
        # OJ 满 → 任务终止成功(金答案足够, 不必再进 MT)。
        task.oj_pass_count += 1
        self._task_dao.increment_counter(
            self._conn, task.id, "oj_pass_count"
        )
        if task.oj_pass_count >= self._config.retry.oj_pass_count_max:
            task.pending_termination = None
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL,
                task=task,
                diagnostic=(
                    f"OJ ESC pass (oj_pass_count={task.oj_pass_count}"
                    f" >= {self._config.retry.oj_pass_count_max}) → 终止成功"
                ),
            )

        if self._config.pipeline_mode == "full":
            self._task_dao.update_step(self._conn, task.id, "mt")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_MT,
                task=task,
                diagnostic="OJ ESC pass → MT",
            )
        else:
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL,
                task=task,
                diagnostic="OJ ESC pass → Final (no MT)",
            )

    def _on_syntax_error(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
    ) -> ProcessResult:
        action = "retry_syntax"
        self._insert_oj_attempt(
            task=task,
            a_round=a_round_for_attempt,
            label="SYNTAX_ERROR",
            tool_result=tool_result,
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or None,
            action_taken=action,
        )
        self._update_message_oj_result(task, is_oj_pass=False, action_taken=action)

        task.task_a_syntax_retry_consecutive += 1
        task.task_a_syntax_retry_total += 1
        task.last_oj_label = "SYNTAX_ERROR"
        task.last_oj_error = truncated_text
        task.last_rac_error = None
        task.last_pbt_failure_text = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_syntax_retry_consecutive"
        )
        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_syntax_retry_total"
        )
        return self._route_after_failure(task, "syntax")

    def _on_logic_error(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
    ) -> ProcessResult:
        # Phase 4 (2026-05-13): exit=6 LOGIC_ERROR 不再回 task_a 消耗 logic_retry,
        # 直接走 RP (与 timeout / unsupported_jml 同路)。logic_retry 计数器
        # 现在仅由 PBT 后条件失败触发, 含义变更为"PBT 后条件连续失败次数上限"。
        action = "enter_rp"
        self._insert_oj_attempt(
            task=task,
            a_round=a_round_for_attempt,
            label="LOGIC_ERROR",
            tool_result=tool_result,
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or None,
            action_taken=action,
        )
        self._update_message_oj_result(task, is_oj_pass=False, action_taken=action)

        task.last_oj_label = "LOGIC_ERROR"
        task.last_oj_error = truncated_text
        task.last_rac_error = None
        task.last_pbt_failure_text = None

        if task.task_a_syntax_retry_consecutive > 0:
            task.task_a_syntax_retry_consecutive = 0
            self._task_dao.reset_syntax_consecutive(self._conn, task.id)

        if self._config.pipeline_mode == "oj_only" or self._rp_disabled(task):
            task.task_a_logic_retry += 1
            self._task_dao.increment_counter(
                self._conn, task.id, "task_a_logic_retry"
            )
            return self._route_after_failure(task, "logic")

        self._task_dao.update_step(self._conn, task.id, "rac")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_RAC,
            task=task,
            diagnostic="OJ LOGIC_ERROR → RP",
        )

    def _on_timeout(
        self,
        task: Task,
        a_round_for_attempt: int,
        label: str,
        tool_result,
        truncated_text: str,
        full_length: int,
    ) -> ProcessResult:
        action = "enter_rp"
        self._insert_oj_attempt(
            task=task,
            a_round=a_round_for_attempt,
            label=label,
            tool_result=tool_result,
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or None,
            action_taken=action,
        )
        self._update_message_oj_result(task, is_oj_pass=False, action_taken=action)

        task.last_oj_label = label
        task.last_oj_error = truncated_text

        if task.task_a_syntax_retry_consecutive > 0:
            task.task_a_syntax_retry_consecutive = 0
            self._task_dao.reset_syntax_consecutive(self._conn, task.id)

        if self._config.pipeline_mode == "oj_only" or self._rp_disabled(task):
            task.task_a_logic_retry += 1
            self._task_dao.increment_counter(
                self._conn, task.id, "task_a_logic_retry"
            )
            return self._route_after_failure(task, "timeout")

        self._task_dao.update_step(self._conn, task.id, "rac")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_RAC,
            task=task,
            diagnostic=f"OJ {label} → RP",
        )

    def _on_unsupported(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
        new_blacklist_kws,
    ) -> ProcessResult:
        action = "enter_rp"
        self._insert_oj_attempt(
            task=task,
            a_round=a_round_for_attempt,
            label="UNSUPPORTED_JML",
            tool_result=tool_result,
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or None,
            action_taken=action,
        )
        self._update_message_oj_result(task, is_oj_pass=False, action_taken=action)

        task.last_oj_label = "UNSUPPORTED_JML"
        task.last_oj_error = truncated_text

        if task.task_a_syntax_retry_consecutive > 0:
            task.task_a_syntax_retry_consecutive = 0
            self._task_dao.reset_syntax_consecutive(self._conn, task.id)

        if (
            new_blacklist_kws
            and self._config.pipeline_mode != "oj_only"
            and self._config.rp_fallback_enabled
        ):
            runtime = self._get_blacklist_runtime()
            added = runtime.update(new_blacklist_kws)
            if added:
                _logger.info(
                    "OJ[%s] UNSUPPORTED_JML 动态扩充黑名单: %s",
                    task.id,
                    added,
                )
        elif new_blacklist_kws:
            _logger.info(
                "OJ[%s] 忽略 UNSUPPORTED_JML 动态黑名单(oj_only 或 RP 全局关闭): %s",
                task.id,
                new_blacklist_kws,
            )

        if self._config.pipeline_mode == "oj_only" or self._rp_disabled(task):
            task.task_a_logic_retry += 1
            self._task_dao.increment_counter(
                self._conn, task.id, "task_a_logic_retry"
            )
            return self._route_after_failure(task, "unsupported_jml")

        self._task_dao.update_step(self._conn, task.id, "rac")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_RAC,
            task=task,
            diagnostic="OJ UNSUPPORTED_JML → RP（动态黑名单扩充）",
        )

    # ------------------------------------------------------------------
    # 内部：失败后的路由（终止条件检查）
    # ------------------------------------------------------------------

    def _rp_disabled(self, task: Task) -> bool:
        """本任务当前是否不走 RP 兜底。

        取或：全局配置 ``rp_fallback_enabled=False`` 或 per-task 运行时
        ``task.rp_disabled=True``（后者由 RP 候选满 / RP 路径 MT 达标动态置位，
        P13 任务 3 引入；此处用 getattr 兼容字段尚未落库的中间状态）。
        """
        if not self._config.rp_fallback_enabled:
            return True
        return bool(getattr(task, "rp_disabled", False))

    def _route_after_failure(self, task: Task, kind: str) -> ProcessResult:
        retry = self._config.retry
        terminated: Optional[str] = None
        if task.task_a_syntax_retry_consecutive >= retry.task_a_syntax_consecutive_max:
            terminated = "exhausted_syntax"
        elif task.task_a_syntax_retry_total >= retry.task_a_syntax_total_max:
            terminated = "exhausted_syntax"
        elif task.task_a_logic_retry >= retry.task_a_logic_max:
            terminated = "exhausted_rounds"

        if terminated is not None:
            task.pending_termination = terminated
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL,
                task=task,
                diagnostic=f"OJ {kind} → terminate({terminated})",
            )

        self._task_dao.update_step(self._conn, task.id, "task_a")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_TASK_A,
            task=task,
            diagnostic=f"OJ {kind} → retry task_a",
        )

    # ------------------------------------------------------------------
    # 内部：DAO 包装
    # ------------------------------------------------------------------

    def _insert_oj_attempt(
        self,
        *,
        task: Task,
        a_round: int,
        label: str,
        tool_result,
        detail_truncated: Optional[str],
        detail_full_length: Optional[int],
        action_taken: str,
        full_java_with_jml: Optional[str] = None,
    ) -> None:
        status = LABEL_TO_OJ_STATUS.get(label)
        if status is None:
            raise OJInfraError(
                f"label={label!r} 没有对应的 oj_attempts.status 映射"
            )
        self._oj_attempt_dao.insert(
            self._conn,
            task_id=task.id,
            a_round=a_round,
            status=status,
            action_taken=action_taken,
            exit_code=tool_result.exit_code,
            elapsed_seconds=round(float(tool_result.elapsed), 2),
            detail_truncated=detail_truncated,
            detail_full_length=detail_full_length,
            full_java_with_jml=full_java_with_jml,
        )

    def _update_message_oj_result(
        self,
        task: Task,
        *,
        is_oj_pass: bool,
        action_taken: str,
    ) -> None:
        if task.current_message_id is None:
            _logger.warning(
                "OJ[%s] task.current_message_id 为空，跳过 update_task_a_result",
                task.id,
            )
            return
        self._message_dao.update_task_a_result(
            self._conn,
            task.current_message_id,
            is_oj_pass=is_oj_pass,
            is_pbt_pass=None,
            action_taken=action_taken,
        )

    # ------------------------------------------------------------------
    # 内部：写文件 + 报错文本
    # ------------------------------------------------------------------

    def _write_java_file(self, task: Task, a_round_for_attempt: int) -> Path:
        class_name = (
            self._extract_class_name(task.current_jml_java or "") or "Solution"
        )
        task.rac_class_name = class_name

        out_dir = Path(self._config.paths.java_source_dir or "")
        if str(out_dir) and not out_dir.is_dir():
            out_dir.mkdir(parents=True, exist_ok=True)
        java_path = (
            out_dir / f"{class_name}_{task.id}_{a_round_for_attempt}.java"
        )
        # P10 fix: 去掉 public 修饰符，避免文件名与 public class 名不匹配
        # Java 要求 public class X 必须在 X.java 中，但文件名为 Solution_xxx_x.java
        content = task.current_jml_java or ""
        content = re.sub(r"\bpublic\s+class\b", "class", content)
        java_path.write_text(content, encoding="utf-8", newline="\n")
        return java_path

    @staticmethod
    def _extract_class_name(java_code: str) -> Optional[str]:
        m = re.search(
            r"public\s+(?:final\s+|abstract\s+)?class\s+(\w+)",
            java_code,
        )
        if m:
            return m.group(1)
        m2 = re.search(r"class\s+(\w+)", java_code)
        if m2:
            return m2.group(1)
        return None

    @staticmethod
    def _format_error_text(stdout: str, stderr: str) -> str:
        parts = []
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append("=== stderr ===")
            parts.append(stderr.rstrip())
        return "\n".join(parts)

    @staticmethod
    def _extract_key_logic_error_info(
        combined_err: str, max_lines: int
    ) -> tuple[str, int]:
        """P10: 从 OJ 逻辑错误输出中提取关键信息，过滤无用噪音。

        提取规则：
        1. 过滤掉 .jml 规格文件的错误（对 LLM 无用）
        2. 过滤掉 JVM warning
        3. 按错误单元提取：主错误行 + 代码行 + 标记行 + 关联声明
        4. 最后保留汇总行（N verification failures）

        Args:
            combined_err: stdout + stderr 拼接的完整错误输出
            max_lines: 最大保留行数（来自 config.error_log.openjml_max_lines）

        Returns:
            (truncated_text, full_length) 与 truncate_error_log 保持一致
        """
        if not combined_err:
            return "", 0

        lines = combined_err.split("\n")
        full_length = len(lines)

        result_lines: list[str] = []
        summary_line: str = ""
        skip_until_next_error = False  # 跳过当前错误单元的标记

        for i, line in enumerate(lines):
            # 检测汇总行
            if re.match(r"^\d+ verification failures?$", line.strip()):
                summary_line = line.strip()
                continue

            # 过滤 JVM warning
            if "OpenJDK" in line and "warning:" in line:
                continue

            # 过滤 stderr 分隔符
            if line.startswith("=== stderr ==="):
                continue

            # 检测错误行：.java:数字: verify: 或 .java:数字: error:
            is_java_error = re.search(r"\.java:\d+:\s*(verify:|error:)", line)
            is_jml_error = ".jml:" in line

            if is_java_error or is_jml_error:
                if is_jml_error:
                    # 规格文件错误：跳过这个错误单元
                    skip_until_next_error = True
                    continue
                else:
                    # 用户代码错误：开始新的错误单元
                    skip_until_next_error = False
                    result_lines.append(line)
                    continue

            # 如果正在跳过，忽略后续行直到下一个错误
            if skip_until_next_error:
                continue

            # 代码行（以空格/Tab开头）和标记行（含 ^）
            if line.startswith(" ") or line.startswith("\t"):
                # 再次检查是否引用了 .jml 文件
                if ".jml:" not in line:
                    result_lines.append(line)
                continue

            # 空行：如果结果不为空，可以保留一个空行作为分隔
            if not line.strip() and result_lines and result_lines[-1].strip():
                if len(result_lines) < max_lines - 1:
                    result_lines.append(line)
                continue

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

    # ------------------------------------------------------------------
    # 内部：黑名单单例
    # ------------------------------------------------------------------

    def _get_blacklist_runtime(self) -> BlacklistRuntime:
        if self._blacklist_runtime is not None:
            return self._blacklist_runtime
        return get_instance()
