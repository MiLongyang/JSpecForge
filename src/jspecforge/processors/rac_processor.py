"""RACProcessor —— OpenJML RAC 编译插桩处理器。

设计依据：

- ``docs/design/核心工作流程设计.md`` §5.2（RAC 编译子步骤）
- ``docs/design/外部工具调用技术规格.md`` §3.2 / §3.3（RAC 调用 + 成功判定）
- ``docs/design/系统架构与调度设计.md`` §三 Queue 表中 RACProcessor 行
- ``docs/plan/P6-处理器.md`` plan §二 P6c

**职责**（plan §二 P6c 1~4 步）：

1. 把 ``task.current_jml_java`` 写到 ``.java`` 文件（黑名单命中跳过 OJ 时
   OJ 没写过；OJ→TIMEOUT/UNSUPPORTED 进 RP 时 OJ 已写过——两种情况统一
   覆盖写一次，行为一致）
2. 准备 RAC 输出目录 ``{rac_output_dir}/{task_id}_{a_round}/``
3. 调 :meth:`RACRunner.run` 拿 :class:`RACResult`
4. 写 ``rac_attempts``：status / exit_code / elapsed / detail_truncated /
   action_taken
5. 分流：
   * 编译通过 → 设 ``task.last_rac_class_name`` / ``task.last_rac_output_dir``
     → reset syntax_consecutive → ``next_stage="input_gen"``
   * 编译失败 → syntax 计数器 +1 → ``next_stage="task_a"`` 或
     ``next_stage="final"``（若超限）

**RAC 进入条件**（参见 ``OJProcessor``）：

- 黑名单命中（TaskAProcessor 直接路由 → ``rac``）
- OJ TIMEOUT / UNSUPPORTED_JML（OJProcessor 路由 → ``rac``）

**a_round 语义**：与 OJProcessor 一致，``rac_attempts.a_round = task.a_round - 1``。

**关于 .class 校验**：``RACRunner.run`` 内部已校验 ``.class`` 文件是否生成，
``RACResult.success = (exit_code == 0 and class_exists)``。本 processor 不
需要再单独检查。

**RAC 失败时 task_a assistant 行的回填**：design §九 写入时序未要求 RAC
失败时回填 ``task_a`` 消息（不像 OJProcessor 必须回填 ``is_oj_pass``）。
但因为 RAC 失败导致 syntax 计数 +1，需要让下一轮 TaskA 选 ``A_U_Fail_Syntax``
模板，所以本 processor 设置 ``task.last_rac_error`` 字段（TaskAProcessor
的 ``_select_retry_template`` 会优先选用 syntax 模板）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.message_dao import MessageDAO
from jspecforge.db.oj_bug_report_dao import OJBugReportDAO
from jspecforge.db.rac_attempt_dao import RACAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.processors._format_helpers import (
    extract_key_syntax_error_info,
    truncate_error_log,
)
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_INPUT_GEN,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.tools.rac_runner import RACRunner
from jspecforge.utils.jml_utils import extract_java_class_name
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.rac")


class RACInfraError(Exception):
    """RAC 基础设施级错误（无 JML / Runner 抛异常等）。"""


class RACProcessor(BaseProcessor):
    """OpenJML RAC 编译插桩处理器。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        rac_runner: Optional[RACRunner] = None,
        task_dao: type = TaskDAO,
        rac_attempt_dao: type = RACAttemptDAO,
        oj_bug_report_dao: type = OJBugReportDAO,
        message_dao: type = MessageDAO,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._runner = rac_runner if rac_runner is not None else RACRunner(config)
        self._task_dao = task_dao
        self._rac_attempt_dao = rac_attempt_dao
        self._oj_bug_report_dao = oj_bug_report_dao
        self._message_dao = message_dao

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        if not task.current_jml_java:
            raise RACInfraError(
                f"Task[{task.id}] RACProcessor 进入时 current_jml_java 为空"
            )

        a_round_for_attempt = max(task.a_round - 1, 0)

        # 1. 类名 + 写 .java 文件
        class_name = (
            task.rac_class_name
            or extract_java_class_name(task.current_jml_java)
            or "Solution"
        )
        task.rac_class_name = class_name

        java_path = self._write_java_file(task, class_name, a_round_for_attempt)

        # 2. RAC 输出目录（{rac_output_dir}/{task_id}_{a_round}/）
        output_dir = Path(self._config.paths.rac_output_dir or "") / (
            f"{task.id}_{a_round_for_attempt}"
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        # 3. 调 RACRunner.run（成功 = exit_code==0 AND class_exists）
        try:
            rac_result = self._runner.run(
                str(java_path),
                str(output_dir),
                class_name,
            )
        except Exception as exc:  # noqa: BLE001
            raise RACInfraError(
                f"Task[{task.id}] RACRunner.run 抛异常: {exc}"
            ) from exc

        tool_result = rac_result.tool_result
        _logger.info(
            "RAC[%s, a_round=%d] exit=%d class_exists=%s elapsed=%.2fs",
            task.id,
            a_round_for_attempt,
            tool_result.exit_code,
            rac_result.class_exists,
            tool_result.elapsed,
        )

        # 4. 截断报错（P10: RAC 失败使用语法错误提取，过滤 JVM warning）
        combined_err = self._format_error_text(
            tool_result.stdout, tool_result.stderr
        )
        truncated_text, full_length = extract_key_syntax_error_info(
            combined_err,
            self._config.error_log.rac_max_lines,
        )

        # 4.5 超时检测: 子进程被 kill (exit_code==-1) 且耗时接近超时阈值
        # → 合成有意义的错误信息, 避免 LLM 收到 "unknown error"
        rac_timeout = self._config.openjml.rac_timeout or 180
        if (
            tool_result.exit_code == -1
            and tool_result.elapsed >= rac_timeout - 5
            and not truncated_text
        ):
            truncated_text = (
                f"error: RAC compilation timed out after {int(tool_result.elapsed)}s "
                f"(limit: {rac_timeout}s). "
                "The JML specification is too complex to compile within the time limit. "
                "Simplify the specification: reduce the number of ensures/requires clauses, "
                "avoid deeply nested quantifiers, and remove redundant conditions."
            )
            full_length = len(truncated_text)

        # 5. 分流
        if rac_result.success:
            return self._on_pass(
                task, a_round_for_attempt, tool_result, output_dir
            )

        return self._on_fail(
            task,
            a_round_for_attempt,
            tool_result,
            truncated_text,
            full_length,
        )

    # ------------------------------------------------------------------
    # 分支
    # ------------------------------------------------------------------

    def _on_pass(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        output_dir: Path,
    ) -> ProcessResult:
        """RAC 编译通过 → 进 input_gen。"""
        self._rac_attempt_dao.insert(
            self._conn,
            task_id=task.id,
            a_round=a_round_for_attempt,
            status="pass",
            exit_code=tool_result.exit_code,
            elapsed_seconds=round(float(tool_result.elapsed), 2),
            action_taken="continue_to_pbt",
            detail_truncated=None,
            detail_full_length=0,
        )

        task.rac_output_dir = str(output_dir)
        task.last_rac_error = None

        # RAC 编译成功 = 语法正确，重置 syntax_consecutive
        if task.task_a_syntax_retry_consecutive > 0:
            task.task_a_syntax_retry_consecutive = 0
            self._task_dao.reset_syntax_consecutive(self._conn, task.id)

        self._task_dao.update_step(self._conn, task.id, "input_gen")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_INPUT_GEN,
            task=task,
            diagnostic="RAC pass → input_gen",
        )

    def _on_fail(
        self,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
    ) -> ProcessResult:
        """RAC 编译失败 → syntax 计数 +1，task_a 或 final。"""
        # 2026-05-16: OJ ESC 已通过语法校验后 RAC 仍编译失败 → 视为 OJ 工具
        # 自身 bug, 不再枚举具体错误类 (NoSuchMethodError / code too large /
        # ...). 黑名单跳过 OJ 的路径 last_oj_label is None, 不进本分支,
        # 保持正常 syntax 重试。
        combined_err = self._format_error_text(tool_result.stdout, tool_result.stderr)
        if task.last_oj_label in self._OJ_VALIDATED_LABELS:
            bug_pattern = self._extract_oj_bug_pattern(combined_err)
            terminated = self._handle_oj_bug_detected(
                task=task,
                a_round_for_attempt=a_round_for_attempt,
                tool_result=tool_result,
                truncated_text=truncated_text,
                full_length=full_length,
                error_pattern=bug_pattern,
                full_error_text=combined_err,
            )
            if terminated is not None:
                return terminated

        self._rac_attempt_dao.insert(
            self._conn,
            task_id=task.id,
            a_round=a_round_for_attempt,
            status="failed",
            exit_code=tool_result.exit_code,
            elapsed_seconds=round(float(tool_result.elapsed), 2),
            action_taken="retry_syntax",
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or 0,
        )

        # Phase 4 (2026-05-16): 回填触发本轮的 task_a assistant 行,
        # 把 OJProcessor/TaskAProcessor 留下的 action_taken
        # (skipped_to_rp / enter_rp / 等) 覆盖为 retry_syntax。
        # 否则黑名单跳过 OJ 后 RAC 失败的 message 会停留在 skipped_to_rp,
        # 与"进 RP 后回填最终结果"的设计语义不一致。
        self._update_message_rac_result(task, action_taken="retry_syntax")

        task.task_a_syntax_retry_consecutive += 1
        task.task_a_syntax_retry_total += 1
        task.last_rac_error = truncated_text
        # RAC 失败时 last_oj_label 不再有效，让下一轮 TaskA 走 syntax 模板
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_pbt_failure_text = None

        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_syntax_retry_consecutive"
        )
        self._task_dao.increment_counter(
            self._conn, task.id, "task_a_syntax_retry_total"
        )

        # 终止条件检查
        retry = self._config.retry
        terminated: Optional[str] = None
        if task.task_a_syntax_retry_consecutive >= retry.task_a_syntax_consecutive_max:
            terminated = "exhausted_syntax"
        elif task.task_a_syntax_retry_total >= retry.task_a_syntax_total_max:
            terminated = "exhausted_syntax"

        if terminated is not None:
            task.pending_termination = terminated
            self._task_dao.update_step(self._conn, task.id, "final")
            self._conn.commit()
            return ProcessResult(
                next_stage=NEXT_FINAL,
                task=task,
                diagnostic=f"RAC fail → terminate({terminated})",
            )

        self._task_dao.update_step(self._conn, task.id, "task_a")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_TASK_A,
            task=task,
            diagnostic="RAC fail → retry task_a",
        )

    # ------------------------------------------------------------------
    # 回填 task_a assistant 行 (Phase 4, 2026-05-16)
    # ------------------------------------------------------------------

    def _update_message_rac_result(
        self,
        task: Task,
        *,
        action_taken: str,
    ) -> None:
        """RAC 失败后把当前 task_a assistant 的 action_taken 覆盖为
        ``retry_syntax`` (与 ``rac_attempts.action_taken`` 一致)。

        is_oj_pass 不该被 RAC 改: 黑名单跳过 OJ 时上游已写 False;
        OJ 真跑过的 enter_rp/SUCCESS 也都是 OJProcessor 写过。
        update_task_a_result 不支持 partial update, 这里和 PBT 一样
        先用 ``MessageDAO.get_by_id`` 读现值再写回, 避免 NULL 化。
        """
        if task.current_message_id is None:
            return
        current = self._message_dao.get_by_id(self._conn, task.current_message_id)
        existing_is_oj_pass = (
            None if current is None else current.get("is_oj_pass")
        )
        if isinstance(existing_is_oj_pass, int):
            existing_is_oj_pass = bool(existing_is_oj_pass)
        existing_is_pbt_pass = (
            None if current is None else current.get("is_pbt_pass")
        )
        if isinstance(existing_is_pbt_pass, int):
            existing_is_pbt_pass = bool(existing_is_pbt_pass)
        self._message_dao.update_task_a_result(
            self._conn,
            task.current_message_id,
            is_oj_pass=existing_is_oj_pass,
            is_pbt_pass=existing_is_pbt_pass,
            action_taken=action_taken,
        )

    # ------------------------------------------------------------------
    # 写文件 + 报错文本
    # ------------------------------------------------------------------

    def _write_java_file(
        self,
        task: Task,
        class_name: str,
        a_round_for_attempt: int,
    ) -> Path:
        """覆盖写 ``{java_source_dir}/{ClassName}_{task_id}_{a_round}.java``。"""
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
    def _format_error_text(stdout: str, stderr: str) -> str:
        parts = []
        if stdout:
            parts.append(stdout.rstrip())
        if stderr:
            parts.append("=== stderr ===")
            parts.append(stderr.rstrip())
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # OJ bug 打标（Phase 4, 2026-05-13；2026-05-16 泛化）
    # ------------------------------------------------------------------

    #: OJ ESC 已经过完整语法校验的标签。命中其一表示规格能被 OJ 解析,
    #: 此时 RAC 仍编译失败 → 视为 OJ 工具自身 bug。黑名单跳过 OJ 时
    #: ``last_oj_label is None``, 不在此集合, 走正常 syntax 重试。
    _OJ_VALIDATED_LABELS = frozenset(
        {"SUCCESS", "LOGIC_ERROR", "OPENJML_TIMEOUT", "HARNESS_TIMEOUT", "UNSUPPORTED_JML"}
    )

    #: javac 报错前缀 ``<path>/Foo.java:<line>:`` 的归一化匹配。
    #: 同一类报错会因 a_round 变化导致文件名(``Solution_<task>_<round>.java``)
    #: 和行号(JML 长度变化)漂动, 不剥前缀则每次 pattern 都不同, A 阈值永远
    #: 命中 1 次连续。剥掉后 ``error: code too large for try statement``
    #: 就能稳定计数。
    _JAVAC_PREFIX_RE = re.compile(r"^.*?\.java:\d+:\s*(.+)$")

    _JVM_BANNER_RE = re.compile(
        r"^(Picked up |WARNING: A terminally deprecated method|"
        r"WARNING: sun\.|OpenJDK 64-Bit|NOTE:)"
    )

    @classmethod
    def _extract_oj_bug_pattern(cls, combined_err: str) -> str:
        """从 RAC 错误文本里抽取一行作为 pattern key, 用于同模式连续计数。

        策略: 取第一行非空、非分隔符、非 JVM 启动横幅的文本, 剥掉 javac
        ``<path>.java:<line>:`` 前缀后截断到 200 字符。
        """
        if not combined_err:
            return "RAC_FAIL_UNKNOWN"
        for raw in combined_err.splitlines():
            line = raw.strip()
            if not line or line.startswith("==="):
                continue
            if cls._JVM_BANNER_RE.match(line):
                continue
            m = cls._JAVAC_PREFIX_RE.match(line)
            if m:
                line = m.group(1).strip()
            return line[:200]
        return "RAC_FAIL_UNKNOWN"

    def _count_consecutive_same_pattern(
        self, task_id: int, error_pattern: str
    ) -> int:
        """从 oj_bug_reports 时间序末尾起算最新一条 pattern 的连续出现次数。

        list_by_task 已按 id ASC 返回, 反向遍历直到遇到不同 pattern 即止。
        本次刚 insert 的行也包含在内, 因此返回值 >= 1。
        """
        rows = self._oj_bug_report_dao.list_by_task(self._conn, task_id)
        consec = 0
        for row in reversed(rows):
            if row.get("error_pattern") == error_pattern:
                consec += 1
            else:
                break
        return consec

    def _handle_oj_bug_detected(
        self,
        *,
        task: Task,
        a_round_for_attempt: int,
        tool_result,
        truncated_text: str,
        full_length: int,
        error_pattern: str,
        full_error_text: str,
    ) -> Optional[ProcessResult]:
        """记录 oj_bug_reports + 检查双阈值 → 命中则终止任务。

        触发终止的两个独立条件 (任一即终止):
          A. 同一 pattern *连续* 出现达 ``rac_error_pattern_max`` (默认 3)。
             不同 pattern 出现会把连续计数清零, 再次出现重新从 1 起算。
          B. 不区分 pattern 累计达 ``rac_oj_bug_total_max`` (默认 8)。
             用于拦截不同 pattern 交替出现导致 A 永远清零的死循环。

        返回非 None 表示已经触发终止 (调用方应直接 return); 返回 None
        表示仅落了一行 bug_report, 走原有 RAC syntax 失败重试路径。
        """
        try:
            self._oj_bug_report_dao.insert(
                self._conn,
                error_pattern=error_pattern,
                task_id=task.id,
                a_round=a_round_for_attempt,
                jml_source=task.current_jml_java,
                method_code=task.method_code,
                rac_detail=truncated_text or full_error_text[:4000] or None,
            )
            consec = self._count_consecutive_same_pattern(task.id, error_pattern)
            total = self._oj_bug_report_dao.count_by_task(self._conn, task.id)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "RAC[%s] oj_bug_reports 写入失败: %s; 跳过打标", task.id, exc
            )
            return None

        max_consec = self._config.retry.rac_error_pattern_max
        max_total = self._config.retry.rac_oj_bug_total_max
        _logger.warning(
            "RAC[%s] OJ 疑似 bug pattern=%r consec=%d/%d total=%d/%d",
            task.id, error_pattern, consec, max_consec, total, max_total,
        )

        trigger: Optional[str] = None
        if consec >= max_consec:
            trigger = f"consec={consec}>={max_consec}"
        elif total >= max_total:
            trigger = f"total={total}>={max_total}"
        if trigger is None:
            return None

        # 命中阈值: 也写一条 rac_attempts (action_taken=retry_syntax) 保留诊断,
        # 然后立即 final(failure_stage=oj_bug)。
        self._rac_attempt_dao.insert(
            self._conn,
            task_id=task.id,
            a_round=a_round_for_attempt,
            status="failed",
            exit_code=tool_result.exit_code,
            elapsed_seconds=round(float(tool_result.elapsed), 2),
            action_taken="retry_syntax",
            detail_truncated=truncated_text or None,
            detail_full_length=full_length or 0,
        )

        # 同 _on_fail 路径: 回填 task_a assistant 的 action_taken=retry_syntax
        self._update_message_rac_result(task, action_taken="retry_syntax")

        task.last_rac_error = truncated_text
        task.pending_termination = "failed_oj_bug"
        task.extra["oj_bug_pattern"] = error_pattern
        task.extra["oj_bug_trigger"] = trigger
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic=f"RAC oj_bug({error_pattern}, {trigger}) → terminate",
        )
