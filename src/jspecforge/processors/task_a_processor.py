"""TaskAProcessor —— Task A 阶段处理器（生成 / 修复 JML 规约）。

设计依据：

- ``docs/design/核心工作流程设计.md`` §三（Task A 执行）+ §3.4（LLM 输出
  异常处理）+ §四（黑名单检测 + OJ 分支判定中的 TaskA 入口逻辑）
- ``docs/design/系统架构与调度设计.md`` §5.3 TaskAProcessor.process 伪代码
- ``docs/plan/P6-处理器.md`` plan §二 P6a

**职责**（plan §二 P6a 1~7 步）：

1. 检查全局终止条件（``a_round`` / 三个 syntax/logic 计数器），命中 →
   ``next_stage=NEXT_FINAL`` 并设 ``task.pending_termination``
2. 选择提示词模板：首次 ``A_S + A_U_First``；重试时按 ``task.last_*``
   选 ``A_U_Fail_OJ`` / ``A_U_Fail_Syntax`` / ``A_U_Fail_PostCondition``
   / ``A_U_PBT_Pass``
3. 用 :meth:`LLMSession.from_db` 恢复（首次时空消息列表）+
   :meth:`for_task_a` 语义；首次时 ``append_system(A_S)``，后续始终
   ``append_user(...)`` 后 ``complete_via(client)``
4. ``result.success=False``：按 ``error`` 字段处理：
   * ``"reasoning_only_no_content"`` / ``"all_models_exhausted"`` →
     不消耗 a_round，重试 LLM 调用（最多 ``max_reasoning_only_retries`` 次）
   * 其他 → 视为 syntax 失败，计数器 +1
5. ``result.success=True``：用 :func:`extract_jml_block` +
   :func:`merge_jml_into_code` 提取 + 拼接；失败视为 syntax 失败
6. 黑名单检测（:meth:`BlacklistRuntime.check`）：命中 → 写
   ``oj_attempts(skipped_blacklist)`` + ``next_stage=NEXT_RAC``；未命中 →
   ``next_stage=NEXT_OJ``
7. 更新 ``tasks.a_round`` / ``current_step``

**a_round 语义**（与 ``task_llm_messages.round`` 对齐）：

- ``task.a_round`` = "已成功生成的 assistant 数" = "下一轮 round 的序号"
- 首次进入 TaskA 时 ``task.a_round=0``；LLM 成功 + JML 提取成功后 ``+=1``
- OJ / RAC / PBT 处理时拿到的 ``task.a_round`` 是 ``+1`` 后的值；它们写
  attempt 表时使用 ``task.a_round - 1``（即"刚生成的那一轮"）

**模板选择优先级**（重试时）：

1. ``last_pbt_valid_input_stats`` 非空 → ``A_U_Fail_InsufficientInput`` (Phase 4, PBT 有效输入不足)
2. ``last_pbt_failure_text`` 非空 → ``A_U_Fail_PostCondition``
3. ``last_pbt_pass_a_round`` 非空 → ``A_U_PBT_Pass``
4. ``last_oj_label == "SYNTAX_ERROR"`` 或 ``last_rac_error`` 非空 →
   ``A_U_Fail_Syntax``
5. ``last_oj_label == "LOGIC_ERROR"`` 或 ``last_oj_error`` 非空 →
   ``A_U_Fail_OJ``
6. 兜底 → ``A_U_Fail_Syntax``（不应该走到，但兜底为安全）

**P4 包揽消息持久化的契约**：本类**不直接**调 ``MessageDAO.insert`` —— 全部
通过 :class:`LLMSession`。关于 LLM 失败时是否 +1 a_round 的语义：失败时
``LLMSession.complete_via`` **不**记录 assistant，所以 ``session.assistant_count``
不变；下一轮 ``from_db`` 仍认为 ``is_first``。这与 design §3.4
"reasoning_only_no_content 不消耗 a_round 直接重试" 自然吻合。
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from jspecforge.config.settings import AppConfig
from jspecforge.core.task_entity import Task
from jspecforge.db.message_dao import MessageDAO
from jspecforge.db.oj_attempt_dao import OJAttemptDAO
from jspecforge.db.task_dao import TaskDAO
from jspecforge.llm.client import BaseLLMClient
from jspecforge.llm.session import LLMSession
from jspecforge.processors.base import (
    NEXT_FINAL,
    NEXT_OJ,
    NEXT_RAC,
    NEXT_TASK_A,
    BaseProcessor,
    ProcessResult,
)
from jspecforge.prompts import render
from jspecforge.tools.blacklist_runtime import BlacklistRuntime, get_instance
from jspecforge.utils.jml_utils import (
    extract_jml_block,
    is_source_method_fidelity_preserved,
    merge_jml_into_code,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("processors.task_a")

#: 模板：首次系统消息
_T_A_S = "A_S"
#: 模板：首次用户消息
_T_A_U_FIRST = "A_U_First"
#: 模板：OJ 逻辑失败回退
_T_A_U_FAIL_OJ = "A_U_Fail_OJ"
#: 模板：OJ 语法失败 / RAC 编译失败回退
_T_A_U_FAIL_SYNTAX = "A_U_Fail_Syntax"
#: 模板：PBT postcondition 失败回退
_T_A_U_FAIL_POST = "A_U_Fail_PostCondition"
#: 模板：PBT 100% 通过回退（请求 SMT 友好重写）
_T_A_U_PBT_PASS = "A_U_PBT_Pass"
#: 模板：PBT 有效输入不足回喂（Phase 4, 2026-05-13）
_T_A_U_FAIL_INSUFFICIENT_INPUT = "A_U_Fail_InsufficientInput"
#: 模板：MT 反馈回喂（P11）
_T_A_U_MT_FEEDBACK = "A_U_MT_Feedback"
_T_A_U_FAIL_SOURCE_FIDELITY = "A_U_Fail_SourceFidelity"


class TaskAProcessor(BaseProcessor):
    """Task A 阶段处理器。

    依赖通过构造函数注入，便于测试时 mock。

    Args:
        config: ``AppConfig``。
        conn: PyMySQL 连接（DAO 写入用）。
        llm_client: 实现 :class:`BaseLLMClient` 的客户端。
        message_dao: 默认为 :class:`MessageDAO`，可注入 mock。
        task_dao: 默认为 :class:`TaskDAO`。
        oj_attempt_dao: 默认为 :class:`OJAttemptDAO`。
        blacklist_runtime: 默认为 ``None`` —— 延迟到 :meth:`process` 时调
            :func:`tools.blacklist_runtime.get_instance`。测试中可显式传入。
        max_reasoning_only_retries: ``reasoning_only_no_content`` /
            ``all_models_exhausted`` 错误时不消耗 a_round 的重试次数上限。
            达到上限后视为 syntax 失败。默认 3。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        conn: Any,
        llm_client: BaseLLMClient,
        message_dao: type = MessageDAO,
        task_dao: type = TaskDAO,
        oj_attempt_dao: type = OJAttemptDAO,
        blacklist_runtime: Optional[BlacklistRuntime] = None,
        max_reasoning_only_retries: int = 3,
    ) -> None:
        super().__init__(config)
        self._conn = conn
        self._llm = llm_client
        self._message_dao = message_dao
        self._task_dao = task_dao
        self._oj_attempt_dao = oj_attempt_dao
        self._blacklist_runtime = blacklist_runtime
        self._max_reasoning_only_retries = max_reasoning_only_retries

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def process(self, task: Task) -> ProcessResult:
        """处理一个 Task A 步骤。"""
        # 0. 入口：清掉本轮的临时字段（current_*），保留 last_* 供模板选择
        task.reset_round_temp()
        self._refresh_persisted_counters(task)

        # 1. 检查全局终止条件（在调 LLM 之前，避免浪费 token）
        terminated = self._check_terminations(task)
        if terminated is not None:
            return self._route_to_final(task, terminated)

        # 2. 从 DB 恢复或新建 LLM 会话
        session = LLMSession.from_db(
            task_id=task.id,
            session_type="task_a",
            a_round=0,
            dao=self._message_dao,
            conn=self._conn,
        )
        is_first = session.assistant_count == 0

        # ─────────────────────────────────────────────────────────────
        # 2.5 中断恢复检查(2026-05-02 阶段 C)
        #
        # 问题 6.2: 上次中断时 assistant 已落盘但 OJ 未跑(is_oj_pass IS NULL)
        #          → 跳过 LLM,直接拿 last_assistant 进 OJ 验证流程
        # 问题 6.1: 上次中断时 user 已落盘但 LLM 还没返回
        #          → 跳过 append_user,直接 complete_via 复用末尾 user
        # ─────────────────────────────────────────────────────────────
        if session.has_pending_assistant():
            _logger.info(
                "TaskA[%s] 中断恢复: 检测到 pending assistant,"
                "跳过 LLM 直接进入 OJ 验证(assistant_count=%d)",
                task.id,
                session.assistant_count,
            )
            return self._resume_from_pending_assistant(session, task)

        skip_append = False
        if session.has_trailing_user_without_assistant():
            # 末尾是 user 但无 assistant: 上次中断在 LLM 调用前/中
            # 不再 append 任何 system/user,直接复用末尾 user 发 LLM,
            # 否则会写出重复的 system+user(首次) 或重复 user(重试)。
            _logger.info(
                "TaskA[%s] 中断恢复: 检测到 trailing user 无 assistant,"
                "跳过 append 直接复用末尾 user 发 LLM (is_first=%s)",
                task.id,
                is_first,
            )
            skip_append = True
        elif not is_first:
            task.a_round += 1
            # 重试时再次检查 a_round 上限
            terminated = self._check_terminations(task)
            if terminated is not None:
                return self._route_to_final(task, terminated)

        # 4. 选模板 + append(skip_append 时跳过)
        if not skip_append:
            try:
                self._append_messages(session, task, is_first=is_first)
            except Exception as exc:  # noqa: BLE001 - 渲染异常按 syntax 失败处理
                _logger.warning(
                    "TaskA[%s, a_round=%d] 模板渲染失败: %s",
                    task.id,
                    task.a_round,
                    exc,
                )
                return self._handle_syntax_failure(
                    task, f"模板渲染失败: {exc}"
                )

        # 5. 调 LLM（含 reasoning_only_no_content 重试）
        result = None
        for attempt in range(self._max_reasoning_only_retries):
            result = session.complete_via(self._llm)
            if result.success:
                break
            if result.error in ("reasoning_only_no_content", "all_models_exhausted"):
                _logger.info(
                    "TaskA[%s] LLM 失败但不消耗 a_round（error=%s, attempt=%d）",
                    task.id,
                    result.error,
                    attempt + 1,
                )
                continue
            # 其他错误 → syntax 失败
            return self._handle_syntax_failure(
                task, f"LLM 调用失败: {result.error}"
            )

        if result is None or not result.success:
            return self._handle_syntax_failure(
                task,
                f"LLM 连续失败 {self._max_reasoning_only_retries} 次"
                f"（最后 error={result.error if result else 'None'}）",
            )

        # 6-10. 提取 / 黑名单 / 清 last_* / 路由 — 抽到 _post_llm_pipeline
        # (中断恢复 _resume_from_pending_assistant 也复用此流程)

        # 9. a_round 写回 DB（本轮已成功生成）
        # 首次时 task.a_round=0；重试时已经在 step 3 +1 过
        if is_first:
            task.a_round = 1  # 首次成功后 = 1（已生成 1 轮 assistant）
        elif skip_append:
            # 中断恢复(trailing user) 走到这里,a_round 应等于 assistant_count
            # (LLM 刚成功返回新一条 assistant)
            task.a_round = session.assistant_count
        # else: 正常重试,a_round 已在 step 3 被 +1

        self._task_dao.update_a_round(self._conn, task.id, task.a_round)

        return self._post_llm_pipeline(session, task, result.content)

    # ------------------------------------------------------------------
    # 中断恢复 + LLM 后处理(2026-05-02 阶段 C)
    # ------------------------------------------------------------------

    def _resume_from_pending_assistant(
        self,
        session: LLMSession,
        task: Task,
    ) -> ProcessResult:
        """中断恢复: 拿 DB 中已有的 last assistant 跑 OJ 验证流程。

        2026-05-02 阶段 C(用户实测问题 6.2):
        上次中断时 LLM 已返回 assistant 并落盘(is_oj_pass IS NULL),
        但 OJ 还没跑就中断了。重启后:

        1. 从 :meth:`LLMSession.last_assistant_content` 拿原始内容
        2. 修正 ``task.a_round`` = ``session.assistant_count``(权威值)
        3. 进入 :meth:`_post_llm_pipeline`(提取 / 黑名单 / 路由)

        **不**调 LLM,**不**追加 user/system 消息,**不**消耗任何重试计数器。
        """
        content = session.last_assistant_content()
        if not content:
            # 不应发生:has_pending_assistant True 但 last_assistant_content
            # 为空。保守按 syntax 失败兜底
            return self._handle_syntax_failure(
                task, "中断恢复: pending assistant 但 content 为空"
            )

        # 修正 a_round 为权威值(session.assistant_count = DB 中已有 assistant 数)
        task.a_round = session.assistant_count
        self._task_dao.update_a_round(self._conn, task.id, task.a_round)

        return self._post_llm_pipeline(session, task, content)

    def _post_llm_pipeline(
        self,
        session: LLMSession,
        task: Task,
        raw_content: str,
    ) -> ProcessResult:
        """LLM 后处理: 提取 JML → 黑名单 → 路由 OJ/RAC。

        调用方负责: ``task.a_round`` 已正确并已写 DB。

        本方法被两条路径共用:
        - 主流程 (LLM 调用刚成功)
        - 中断恢复 :meth:`_resume_from_pending_assistant`
        """
        # 6. 提取 JML
        # D.4 (2026-05-03): 把 session.last_assistant_id 传给 _handle_syntax_failure,
        # 让它回填 assistant 的 is_oj_pass=False, 避免下次 process 走
        # _resume_from_pending_assistant 死循环
        jml_block = extract_jml_block(raw_content)
        if not jml_block:
            return self._handle_syntax_failure(
                task,
                "LLM 回复中未提取到含 JML 注释的 Java 代码块",
                message_id=session.last_assistant_id,
            )

        full_java = merge_jml_into_code(task.full_code, jml_block)
        if not full_java:
            return self._handle_syntax_failure(
                task,
                "LLM 回复未包含原始方法签名，视为提取失败",
                message_id=session.last_assistant_id,
            )

        # 7. 黑名单检测
        if not is_source_method_fidelity_preserved(task.method_code, full_java):
            return self._handle_source_fidelity_failure(
                task,
                message_id=session.last_assistant_id,
            )

        task.current_jml_java = full_java
        task.current_message_id = session.last_assistant_id

        runtime = self._get_blacklist_runtime()
        hit, hits = runtime.check(full_java)

        # 8. 清掉 last_* 反馈字段(已经被本轮 LLM 消费了)
        task.last_oj_label = None
        task.last_oj_error = None
        task.last_rac_error = None
        task.last_compile_error = None
        task.last_pbt_failure_text = None
        task.last_pbt_valid_input_stats = None
        task.last_pbt_precondition_text = None
        task.last_pbt_pass_a_round = None
        task.last_mt_feedback_text = None
        task.last_mt_path = None
        task.last_mt_score = None
        task.last_mt_killed = None
        task.last_mt_survived = None
        task.last_mt_total = None
        task.last_source_fidelity_error = None

        # 10. 路由
        # P13: 黑名单命中是否跳 RP，取决于本任务是否走 RP 兜底。
        #   - oj_only 模式：A 组基线，向来忽略黑名单
        #   - rp_fallback_enabled=False（全局）或 task.rp_disabled=True（运行时）：
        #     不走 RP，黑名单当不存在，继续送 OJ（届时黑名单语法在 OJ 下真失败
        #     才得到黑名单失败结果）
        # 关键：绝不修改进程级 BlacklistRuntime 单例/文件，仅 per-task 翻转 hit，
        # 避免污染同进程内其它仍走 RP 的任务。
        if hit and (
            self._config.pipeline_mode == "oj_only" or self._rp_disabled(task)
        ):
            _logger.info(
                "TaskA[%s, a_round=%d] 忽略黑名单命中 %s（不走 RP），继续送 OJ",
                task.id,
                task.a_round - 1,
                hits,
            )
            hit = False

        if hit:
            self._oj_attempt_dao.insert(
                self._conn,
                task_id=task.id,
                a_round=task.a_round - 1,
                status="skipped_blacklist",
                action_taken="skipped_to_rp",
            )
            # 回填 task_a assistant 行的 is_oj_pass=0 + action_taken
            # (design §四 注释:"黑名单跳过时为 0")
            if task.current_message_id is not None:
                self._message_dao.update_task_a_result(
                    self._conn,
                    task.current_message_id,
                    is_oj_pass=False,
                    is_pbt_pass=None,
                    action_taken="skipped_to_rp",
                )
            self._task_dao.update_step(self._conn, task.id, "rac")
            self._conn.commit()
            _logger.info(
                "TaskA[%s, a_round=%d] 黑名单命中 %s → 跳过 OJ 进 RP",
                task.id,
                task.a_round - 1,
                hits,
            )
            return ProcessResult(
                next_stage=NEXT_RAC,
                task=task,
                diagnostic=f"blacklist hit {hits} → skip OJ",
            )

        self._task_dao.update_step(self._conn, task.id, "openjml")
        self._conn.commit()
        _logger.info(
            "TaskA[%s, a_round=%d] 已生成 JML → OJ",
            task.id,
            task.a_round - 1,
        )
        return ProcessResult(
            next_stage=NEXT_OJ,
            task=task,
            diagnostic="JML generated → OJ",
        )

    # ------------------------------------------------------------------
    # 内部：模板选择 + 渲染
    # ------------------------------------------------------------------

    def _append_messages(
        self,
        session: LLMSession,
        task: Task,
        *,
        is_first: bool,
    ) -> None:
        """渲染 + append 系统/用户消息到 session。

        首次：append_system(A_S) + append_user(A_U_First)。
        重试：仅 append_user，按 task.last_* 选模板。
        """
        if is_first:
            session.append_system(render(_T_A_S))
            user_text = render(
                _T_A_U_FIRST,
                # E.2.1 (2026-05-03): 删除 method_code 参数(模板已不需要,
                # full_java_code 已包含目标方法, 避免重复浪费 token)
                full_java_code=task.full_code,
                nl_description=task.nl_description,
            )
        else:
            template_id, kwargs = self._select_retry_template(task)
            user_text = render(template_id, **kwargs)

        session.append_user(user_text)

    def _select_retry_template(self, task: Task) -> Tuple[str, dict]:
        """按优先级选择重试模板。

        Returns:
            ``(template_id, render_kwargs)``。
        """
        if task.last_source_fidelity_error:
            return (
                _T_A_U_FAIL_SOURCE_FIDELITY,
                {
                    "source_error": task.last_source_fidelity_error,
                    "full_java_code": task.full_code,
                },
            )
        if task.last_mt_feedback_text:
            mt_path_display = "OJ ESC" if task.last_mt_path == "oj" else "RP (RAC+PBT)"
            score = task.last_mt_score if task.last_mt_score is not None else 0.0
            killed = task.last_mt_killed if task.last_mt_killed is not None else 0
            survived = task.last_mt_survived if task.last_mt_survived is not None else 0
            total = task.last_mt_total if task.last_mt_total is not None else 0
            # P13 任务2.2: MT 反馈仅在 RP 路径加 OJ 优越性提醒 + OJ 失败摘要。
            # path==oj 时该候选本就是 OJ 金答案, 提"OJ 更可信"属冗余, 注入空串。
            if task.last_mt_path == "rp":
                oj_block = self._build_mt_rp_oj_block(task)
            else:
                oj_block = ""
            return (
                _T_A_U_MT_FEEDBACK,
                {
                    "mt_path": mt_path_display,
                    "mt_score": f"{score:.4f}",
                    "mt_killed": str(killed),
                    "mt_survived": str(survived),
                    "mt_total": str(total),
                    "survived_summary": task.last_mt_feedback_text,
                    "oj_error_block": oj_block,
                },
            )
        # Phase 4 (2026-05-13): PBT 有效输入不足优先。last_pbt_valid_input_stats
        # 仅由 PBTProcessor._on_insufficient_valid_input 写入, 与 post_fail 互斥。
        if task.last_pbt_valid_input_stats:
            return (
                _T_A_U_FAIL_INSUFFICIENT_INPUT,
                {
                    "valid_input_stats": task.last_pbt_valid_input_stats,
                    "precondition_samples": task.last_pbt_precondition_text or "(no samples)",
                    "oj_error_block": self._build_oj_block(task),
                },
            )
        if task.last_pbt_failure_text:
            return (
                _T_A_U_FAIL_POST,
                {
                    "failure_samples": task.last_pbt_failure_text,
                    "oj_error_block": self._build_oj_block(task),
                },
            )
        if task.last_pbt_pass_a_round is not None:
            return (
                _T_A_U_PBT_PASS,
                {"oj_error_block": self._build_pbt_pass_oj_block(task)},
            )
        if task.last_oj_label == "SYNTAX_ERROR" or task.last_rac_error:
            err = (
                task.last_rac_error
                or task.last_oj_error
                or "unknown syntax error"
            )
            return (_T_A_U_FAIL_SYNTAX, {"syntax_error": err})
        if task.last_oj_label == "LOGIC_ERROR" or task.last_oj_error:
            return (
                _T_A_U_FAIL_OJ,
                {"oj_error": task.last_oj_error or "unknown logic error"},
            )
        # 兜底：按 syntax error 重试（理论不应走到，但兜底为安全）
        _logger.warning(
            "TaskA[%s] 重试时无任何 last_* 字段，兜底用 A_U_Fail_Syntax",
            task.id,
        )
        return (_T_A_U_FAIL_SYNTAX, {"syntax_error": "unknown error"})

    # ------------------------------------------------------------------
    # 内部：终止条件
    # ------------------------------------------------------------------

    def _refresh_persisted_counters(self, task: Task) -> None:
        """从 DB 刷新终止判断依赖的持久化计数字段。

        运行中的 Task 对象会在多个 worker 队列间传递；入口处刷新一次，
        确保 TaskA 的终止判断和重启恢复看到的是同一份 DB 状态。
        """
        try:
            row = self._task_dao.get_runtime_counters(self._conn, task.id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("TaskA[%s] 刷新计数字段失败，沿用内存状态: %s", task.id, exc)
            return

        if not isinstance(row, dict):
            return

        for field in (
            "a_round",
            "task_a_logic_retry",
            "task_a_syntax_retry_consecutive",
            "task_a_syntax_retry_total",
            "pbt_pass_count",
            "oj_pass_count",
            "task_a_insufficient_input_consecutive",
            "task_a_insufficient_input_total",
            "mt_oj_feedback_round",
            "mt_rp_feedback_round",
            "rp_disabled",
        ):
            if field in row:
                setattr(task, field, int(row.get(field) or 0))

    def _check_terminations(self, task: Task) -> Optional[str]:
        """检查终止条件。返回触发的 ``failure_stage`` 或 ``None``。

        终止条件优先级（任一命中即终止）：

        1. ``a_round >= retry.a_round_max`` (Phase 4: 替代旧的 task_a_max_rounds)
        2. ``task_a_syntax_retry_consecutive >= task_a_syntax_consecutive_max``
        3. ``task_a_syntax_retry_total >= task_a_syntax_total_max``
        4. ``task_a_logic_retry >= task_a_logic_max`` (Phase 4: 仅 PBT 后条件
           失败累计, 不再由 OJ exit=6 触发)
        5. ``task_a_insufficient_input_consecutive >=
           task_a_insufficient_input_consecutive_max`` (Phase 4 新增)
        6. ``task_a_insufficient_input_total >=
           task_a_insufficient_input_total_max`` (Phase 4 新增)

        命中 5/6 时返回 ``"failed_pbt_insufficient_input"``。其它返回原有
        ``"exhausted_rounds"`` / ``"exhausted_syntax"``。
        """
        retry = self._config.retry

        # Phase 4 (2026-05-13): 改读 a_round_max 替代 task_a_max_rounds。
        # task_a_max_rounds 字段保留以兼容旧 YAML, 但代码不再读它。
        if task.a_round >= retry.a_round_max:
            return "exhausted_rounds"

        if task.task_a_syntax_retry_consecutive >= retry.task_a_syntax_consecutive_max:
            return "exhausted_syntax"
        if task.task_a_syntax_retry_total >= retry.task_a_syntax_total_max:
            return "exhausted_syntax"
        if task.task_a_logic_retry >= retry.task_a_logic_max:
            return "exhausted_rounds"
        # Phase 4 (2026-05-13): PBT 有效输入不足独立计数器
        # PBTProcessor 命中上限会直接 _route_to_final, 这里是 defense-in-depth
        # (例如重启后 task 恢复时仍能正确终止)
        if (
            task.task_a_insufficient_input_consecutive
            >= retry.task_a_insufficient_input_consecutive_max
        ):
            return "failed_pbt_insufficient_input"
        if (
            task.task_a_insufficient_input_total
            >= retry.task_a_insufficient_input_total_max
        ):
            return "failed_pbt_insufficient_input"
        return None

    # ------------------------------------------------------------------
    # 内部：syntax 失败处理
    # ------------------------------------------------------------------

    def _handle_source_fidelity_failure(
        self,
        task: Task,
        *,
        message_id: Optional[int] = None,
    ) -> ProcessResult:
        error_text = (
            "The previous answer modified the original Java method source. "
            "Only JML annotations may be changed."
        )
        current_failures = self._count_source_fidelity_failures(task) + 1
        if message_id is not None:
            try:
                self._message_dao.update_task_a_result(
                    self._conn,
                    message_id,
                    is_oj_pass=False,
                    is_pbt_pass=None,
                    action_taken="retry_source_fidelity",
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "TaskA[%s] 回填 source fidelity assistant[%d] 失败: %s",
                    task.id,
                    message_id,
                    exc,
                )

        task.current_jml_java = None
        task.current_message_id = None
        task.last_source_fidelity_error = error_text

        source_fidelity_max = self._config.retry.source_fidelity_max
        terminated = (
            "failed_source_fidelity"
            if current_failures >= source_fidelity_max
            else self._check_terminations(task)
        )
        if terminated is not None:
            return self._route_to_final(task, terminated)

        self._task_dao.update_step(self._conn, task.id, "task_a")
        self._conn.commit()
        _logger.info(
            "TaskA[%s, a_round=%d] source fidelity failed, retrying TaskA",
            task.id,
            task.a_round,
        )
        return ProcessResult(
            next_stage=NEXT_TASK_A,
            task=task,
            diagnostic="source fidelity failed",
        )

    def _count_source_fidelity_failures(self, task: Task) -> int:
        try:
            rows = self._message_dao.list_session(
                self._conn,
                task_id=task.id,
                session_type="task_a",
                a_round=0,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "TaskA[%s] count source fidelity failures failed: %s",
                task.id,
                exc,
            )
            return 0
        return sum(
            1
            for row in rows
            if row.get("role") == "assistant"
            and row.get("action_taken") == "retry_source_fidelity"
        )

    def _handle_syntax_failure(
        self,
        task: Task,
        error_text: str,
        *,
        message_id: Optional[int] = None,
    ) -> ProcessResult:
        """LLM 失败 / JML 提取失败的统一处理。

        - syntax 计数器 +1（连续 + 总计）
        - 设置 ``task.last_oj_error`` 给下一轮 TaskA 选模板用
        - 检查终止条件 → ``NEXT_FINAL`` 或自循环 ``NEXT_TASK_A``

        Args:
            task: 当前 task
            error_text: 给 LLM 看的错误描述(也存为 last_oj_error)
            message_id: D.4 (2026-05-03) 修复回归: 如果失败发生在 LLM 已 record_assistant
                之后(extract_jml_block / merge_jml_into_code 失败的场景),传入对应的
                assistant 行 id, 函数会回填 is_oj_pass=False + action_taken="retry_syntax",
                让 has_pending_assistant 状态消失,**避免下次 process 走
                _resume_from_pending_assistant 死循环**(阶段 C 修复引入的回归)。

                如果失败发生在 LLM 调用前 / 调用失败 / 模板渲染失败(此时还没有
                assistant 行落盘),传 None。
        """
        # D.4 修复: 回填 pending assistant 让其退出 pending 状态,
        # 避免阶段 C 的 _resume_from_pending_assistant 在 extract 失败时死循环
        if message_id is not None:
            try:
                self._message_dao.update_task_a_result(
                    self._conn,
                    message_id,
                    is_oj_pass=False,
                    is_pbt_pass=None,
                    action_taken="retry_syntax",
                )
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "TaskA[%s] 回填 assistant[%d] is_oj_pass=False 失败: %s",
                    task.id,
                    message_id,
                    exc,
                )

        task.task_a_syntax_retry_consecutive += 1
        task.task_a_syntax_retry_total += 1
        task.last_oj_label = "SYNTAX_ERROR"
        task.last_oj_error = error_text

        self._task_dao.increment_counter(
            self._conn,
            task.id,
            "task_a_syntax_retry_consecutive",
        )
        self._task_dao.increment_counter(
            self._conn,
            task.id,
            "task_a_syntax_retry_total",
        )

        terminated = self._check_terminations(task)
        if terminated is not None:
            return self._route_to_final(task, terminated)

        # 自循环回 TaskA
        self._task_dao.update_step(self._conn, task.id, "task_a")
        self._conn.commit()
        _logger.info(
            "TaskA[%s, a_round=%d] syntax 失败（%s），回 TaskA 重试",
            task.id,
            task.a_round,
            error_text[:80],
        )
        return ProcessResult(
            next_stage=NEXT_TASK_A,
            task=task,
            diagnostic=f"syntax 失败: {error_text[:80]}",
        )

    # ------------------------------------------------------------------
    # 内部：路由到 final
    # ------------------------------------------------------------------

    def _route_to_final(self, task: Task, failure_stage: str) -> ProcessResult:
        """触发终止条件，路由到 final_queue。"""
        task.pending_termination = failure_stage
        self._task_dao.update_step(self._conn, task.id, "final")
        self._conn.commit()
        _logger.info(
            "TaskA[%s] 终止条件触发: %s（a_round=%d, syntax_consec=%d, "
            "syntax_total=%d, logic=%d, ii_consec=%d, ii_total=%d）",
            task.id,
            failure_stage,
            task.a_round,
            task.task_a_syntax_retry_consecutive,
            task.task_a_syntax_retry_total,
            task.task_a_logic_retry,
            task.task_a_insufficient_input_consecutive,
            task.task_a_insufficient_input_total,
        )
        return ProcessResult(
            next_stage=NEXT_FINAL,
            task=task,
            diagnostic=f"terminated: {failure_stage}",
        )

    # ------------------------------------------------------------------
    # 内部：黑名单单例
    # ------------------------------------------------------------------

    def _rp_disabled(self, task: Task) -> bool:
        """本任务当前是否不走 RP 兜底。

        取或：全局配置 ``rp_fallback_enabled=False`` 或 per-task 运行时
        ``task.rp_disabled=True``（后者 P13 任务 3 引入；此处用 getattr 兼容
        字段尚未落库的中间状态）。
        """
        if not self._config.rp_fallback_enabled:
            return True
        return bool(getattr(task, "rp_disabled", False))

    def _build_oj_error_summary(self, task: Task) -> str:
        """P13 任务4/2.2: 构建回喂 task_a 的 OJ 失败摘要。

        作用：当任务走过 RP 路径(RAC/PBT/MT)后回 task_a 时, 把当初把候选
        打入 RP 的 OJ 失败原因一并带回, 引导模型往 ESC 友好写法靠拢。

        数据来源: 查 ``oj_attempts`` 表本任务最近一条 *非 pass* 记录
        (DB-backed, 天然支持重启恢复)。之所以不读 task.last_oj_error —— 那个
        字段在 RP 路径上被 RAC/PBT 多处清空, 回到 task_a 时多半已是 None。

        黑名单特例: ``status='skipped_blacklist'`` 表示 OJ 被黑名单跳过、
        没有真实 OJ 报错。此时返回预判说明("该语法在 ESC 下会超时, 请换写法"),
        并标注是预判而非实测。

        Args:
            task: 当前任务。

        Returns:
            OJ 失败摘要字符串; 找不到任何失败记录时返回空串(调用方据此决定
            是否在提示词里显示该段落)。
        """
        try:
            rows = self._oj_attempt_dao.list_by_task(self._conn, task.id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "TaskA[%s] 查询 oj_attempts 构建 OJ 摘要失败: %s", task.id, exc
            )
            return ""

        # 取最近一条非 pass 记录(list_by_task 按 a_round 升序, 反向取)
        for row in reversed(rows):
            status = row.get("status")
            if not status or status == "pass":
                continue
            detail = row.get("detail_truncated") or ""
            detail = detail.strip()
            # 黑名单跳过: 无真实报错, 用预判说明
            if status == "skipped_blacklist":
                return (
                    "Blacklisted JML syntax (e.g. \\sum, \\max, \\num_of) was "
                    "detected. These constructs are known to cause OpenJML ESC "
                    "timeout, so OJ was skipped — this is a PREDICTED issue, "
                    "not a tested failure. Please rewrite using standard, "
                    "ESC-verifiable JML syntax."
                )
            # 真实 OJ 失败: label + 截断报错
            label = status
            snippet = detail[:1500] if detail else "(no OJ error detail recorded)"
            return f"[OJ ESC status={label}] {snippet}"

        return ""

    def _build_oj_block(self, task: Task) -> str:
        """P13 任务4: 普通 RP 反馈(PostCondition / InsufficientInput)的 OJ 块。

        有 OJ 失败摘要 → 返回含引导句的完整段落(前后各一空行, 便于嵌入模板);
        无 → 返回空串(模板中该段消失)。
        """
        summary = self._build_oj_error_summary(task)
        if not summary:
            return ""
        return (
            "\n\nNOTE: The original OpenJML ESC attempt on this candidate "
            "failed. OJ ESC is the gold standard (full-input proof); runtime "
            "testing below is only a fallback. Try to eventually rewrite JML "
            "that passes ESC, while fixing the immediate runtime issue:\n"
            f"{summary}\n"
        )

    def _build_mt_rp_oj_block(self, task: Task) -> str:
        """P13 任务2.2: MT 反馈 path==rp 时的 OJ 块(含 OJ 优越性提醒)。

        RP 路径候选只通过了运行时检验、可信度低, 此处提醒模型转向 ESC 友好写法,
        并附带当初的 OJ 失败摘要。无摘要时仍保留优越性提醒(仅去摘要部分)。
        """
        summary = self._build_oj_error_summary(task)
        if summary:
            return (
                "\n\nREMINDER: Your current JML passed runtime testing "
                "(RAC+PBT) but could NOT pass OpenJML ESC static verification. "
                "Runtime testing only samples inputs and cannot guarantee "
                "correctness on boundary values or unseen paths — this is a "
                "LOW-CONFIDENCE result. Prioritize rewriting JML in an "
                "ESC-friendly style (simpler quantifiers, explicit bounds, "
                "shallow nesting) while strengthening it against the surviving "
                "mutants below.\n"
                f"Original OJ ESC failure: {summary}\n"
            )
        return (
            "\n\nREMINDER: Your current JML passed runtime testing "
            "(RAC+PBT) but could NOT pass OpenJML ESC static verification. "
            "Runtime testing only samples inputs and cannot guarantee "
            "correctness on boundary values or unseen paths — this is a "
            "LOW-CONFIDENCE result. Prioritize rewriting JML in an "
            "ESC-friendly style (simpler quantifiers, explicit bounds, "
            "shallow nesting) while strengthening it against the surviving "
            "mutants below.\n"
        )

    def _build_pbt_pass_oj_block(self, task: Task) -> str:
        """PBT 通过、但 OJ/ESC 未通过时回喂的真实 OJ 失败原因。

        供 ``A_U_PBT_Pass`` 模板使用：该模板只给出"好写法"建议、缺少真实报错。
        此处补上当初 OJ 失败的确切原因（黑名单关闭时为实测报错；黑名单开启且
        命中跳过时仍为预判说明，维持现状）。
        """
        summary = self._build_oj_error_summary(task)
        if not summary:
            return ""
        return (
            "Original OpenJML ESC failure (the actual reason it did not pass "
            f"static verification): {summary}"
        )

    def _get_blacklist_runtime(self) -> BlacklistRuntime:
        """返回构造时注入的实例，否则取全局单例。"""
        if self._blacklist_runtime is not None:
            return self._blacklist_runtime
        return get_instance()
