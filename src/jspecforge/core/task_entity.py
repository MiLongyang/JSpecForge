"""Task 实体（队列流转载体）。

设计依据：

- ``docs/design/系统架构与调度设计.md`` §三 / §5.3：风格 B（独立 processor + 队列）
  下，Task 实体作为队列里流转的载体，承载 task_id + 计数器 + 当前轮次状态
- ``docs/design/数据库表字段设计.md`` §三 ``tasks`` 表字段：DB 持久化字段子集
- ``docs/design/核心工作流程设计.md`` §二 计数器定义 + §三 Task A 触发场景

**P6 ↔ P7 契约**（plan §4.3）：

- P7 编排器从 DB 读 ``tasks`` 行 → 调 :meth:`Task.from_db_row` 构建 Task →
  入对应队列；processor 从队列取 Task → 调用 ``processor.process(task)`` →
  得到 ``ProcessResult`` → 路由到下一队列
- P7 可向 :class:`Task` 加新字段（如 ``worker_id`` / 重试时间戳等），
  **不能改已有字段语义**
- 计数器字段名严格对齐 DB 表字段，便于 P7 调 ``TaskDAO.increment_counter``

**字段分组**：

1. **主键 + 来源**（不变，从 DB 读入）
2. **运行状态**（与 DB tasks 表实时同步，processor 改后 P7 刷写）
3. **重试计数器**（与 DB tasks 表实时同步）
4. **当前轮次临时字段**（不入库，processor 间通过 Task 传递）
5. **决策标记**（终止 / OJ 直通等 short-circuit 标记）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass
class Task:
    """单个任务在 P6 处理器之间流转的载体。

    本类**可变**（计数器需要被 processor 修改）。线程安全由 P7 编排器保证
    （单个 Task 同时只在一个 processor 中被处理）。

    Attributes:
        id: ``tasks.id``。
        source_dataset_name: 数据集名（如 ``"Code2Bench"``）。
        source_dataset_index: 数据集内序号。
        source_task_id: 原始数据集中的 task_id（如 ``"Code2Bench::p1"``）。
        source_machine: 来源机器标识。
        nl_description: 自然语言需求描述。
        method_code: 方法级 Java 源码。
        full_code: 完整类 Java 源码（未含 JML）。

        status: ``"pending"`` / ``"in_progress"`` / ``"success"`` / ``"failed"``。
        current_step: 当前所处阶段名（与 ``CURRENT_STEPS`` 集合对应）。
        a_round: 当前 Task A 轮次（0-based，已成功生成的 JML 数）。

        task_a_logic_retry: OJ 逻辑失败 + postcondition 失败累计。
        task_a_syntax_retry_consecutive: 连续语法错误次数（生成正确 JML 后
            重置为 0）。
        task_a_syntax_retry_total: 累计语法错误次数（不重置）。
        pbt_pass_count: PBT 100% 通过累计次数。
        input_gen_retry_total: 输入生成器重试累计（跨所有 a_round）。

        current_jml_java: 本轮 Task A 生成的 ``full_java_with_jml``。
            TaskAProcessor 写入；OJ / RAC / PBT 读取。
        current_input_gen_code: 本轮 InputGen 生成的方法源码。
            InputGenProcessor 写入；PBT 读取。
        current_message_id: 本轮触发当前流程的 assistant 消息 ID（用于
            后续 OJ/PBT/编译验证完成后回填结果列）。
        last_oj_label: OJ 最近一次的分类标签（用于 next_stage 决策与
            选下一轮 Task A 模板）。
        last_oj_error: 截断后的 OJ stdout/stderr，喂给 LLM 重试用。
        last_rac_error: 截断后的 RAC 编译错误。
        last_compile_error: 截断后的 javac 编译错误（input_gen 用）。
        last_pbt_failure_text: 格式化后的 PBT 失败样本（A_U_Fail_PostCondition
            模板用）。
        last_pbt_valid_input_stats: 格式化后的 PBT 统计数据
            （IG_U_Fail_ValidInput 模板用）。
        last_pbt_precondition_text: 格式化后的 precondition 违反样本
            （IG_U_Fail_ValidInput 模板用）。
        rac_class_name: 本轮 RAC 编译的目标类名（PBT 用）。
        rac_output_dir: 本轮 RAC 编译输出目录（PBT 用）。

        has_oj_passed: OJ 直通标记（OJProcessor 设 True 时直接 short-circuit
            到 ``done_oj_pass``，不走 final_queue）。
        pending_termination: 终止原因标记（``"exhausted_syntax"`` /
            ``"exhausted_rounds"`` 等）。FinalProcessor 据此填 DB
            ``failure_stage``。
        final_jml_for_oj_pass: OJ 直通时的最终 JML（即 task.current_jml_java
            的快照）。OJProcessor 在 mark_success 时使用。
    """

    # ─────── 主键 + 来源 ───────
    id: int
    source_dataset_name: str = ""
    source_dataset_index: int = 0
    source_task_id: str = ""
    source_machine: str = "local"
    nl_description: str = ""
    method_code: str = ""
    full_code: str = ""
    input_generator_code: Optional[str] = None
    branch_coverage: Optional[float] = None

    # ─────── 运行状态 ───────
    status: str = "pending"
    current_step: Optional[str] = None
    a_round: int = 0

    # ─────── 重试计数器 ───────
    task_a_logic_retry: int = 0
    task_a_syntax_retry_consecutive: int = 0
    task_a_syntax_retry_total: int = 0
    pbt_pass_count: int = 0
    # P13: OJ ESC 通过候选累计数, 与 pbt_pass_count 对称且独立。
    # 达 retry.oj_pass_count_max 时终止成功。
    oj_pass_count: int = 0
    input_gen_retry_total: int = 0
    # D.8 (2026-05-03): PBT TestDriver 编译失败独立计数器
    pbt_compile_retry_consecutive: int = 0
    pbt_compile_retry_total: int = 0
    # Phase 4 (2026-05-13): PBT 有效输入不足独立计数器
    task_a_insufficient_input_consecutive: int = 0
    task_a_insufficient_input_total: int = 0
    # P11: MT 反馈循环计数器
    mt_oj_feedback_round: int = 0
    mt_rp_feedback_round: int = 0
    # P13: 本任务是否已关闭 RP 通道(持久化, 支持重启恢复)。
    # 由 RP 候选满 / RP 路径 MT 达标动态置位; 置位后 OJ 失败不再进 RP、
    # 黑名单命中也忽略, 继续逼 OJ。
    rp_disabled: bool = False

    # ─────── 当前轮次临时字段（不入库）───────
    current_jml_java: Optional[str] = None
    current_input_gen_code: Optional[str] = None
    current_message_id: Optional[int] = None
    last_oj_label: Optional[str] = None
    last_oj_error: Optional[str] = None
    last_rac_error: Optional[str] = None
    last_compile_error: Optional[str] = None
    # D.5 (2026-05-03): PBT TestDriver 编译错误专用字段
    # 跟 last_rac_error 区分 - 之前 PBT compile failed 错存到 last_rac_error
    # 导致 final_processor 写"最后一次 RAC 编译错误"标签错位。
    last_pbt_compile_error: Optional[str] = None
    last_pbt_failure_text: Optional[str] = None
    last_pbt_valid_input_stats: Optional[str] = None
    last_pbt_precondition_text: Optional[str] = None
    last_pbt_pass_a_round: Optional[int] = None
    # Phase 4 (2026-05-14): PBT TestDriver 运行失败的子分类提示词
    # ('timeout' / 'jvm_crash' / 'stdout_parse_failed' / 'unknown'),
    # 由 PBTProcessor._on_run_fail 写入, FinalProcessor 据此组装 failure_reason 头部。
    last_pbt_run_failure_reason: Optional[str] = None
    rac_class_name: Optional[str] = None
    rac_output_dir: Optional[str] = None
    # P11: MT 反馈相关临时字段
    last_mt_feedback_text: Optional[str] = None
    last_mt_path: Optional[str] = None
    last_mt_score: Optional[float] = None
    last_mt_killed: Optional[int] = None
    last_mt_survived: Optional[int] = None
    last_mt_total: Optional[int] = None
    last_source_fidelity_error: Optional[str] = None

    # ─────── 决策标记 ───────
    has_oj_passed: bool = False
    pending_termination: Optional[str] = None
    final_jml_for_oj_pass: Optional[str] = None

    # ─────── 扩展点（P7 自由使用，P6 不读不写）───────
    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 构造器
    # ------------------------------------------------------------------

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "Task":
        """从 ``TaskDAO.get_by_id`` 返回的 dict 构建 Task 实例。

        临时字段（``current_*`` / ``last_*`` / ``has_oj_passed`` 等）保持
        默认值；仅恢复 DB 持久化字段。

        Args:
            row: ``TaskDAO`` 返回的 dict（DictCursor）。

        Returns:
            Task 实例。

        Raises:
            KeyError: ``row`` 缺 ``id`` 字段。
        """
        return cls(
            id=int(row["id"]),
            source_dataset_name=str(row.get("source_dataset_name") or ""),
            source_dataset_index=int(row.get("source_dataset_index") or 0),
            source_task_id=str(row.get("source_task_id") or ""),
            source_machine=str(row.get("source_machine") or "local"),
            nl_description=str(row.get("nl_description") or ""),
            method_code=str(row.get("method_code") or ""),
            full_code=str(row.get("full_code") or ""),
            input_generator_code=row.get("input_generator_code"),
            branch_coverage=float(row["branch_coverage"]) if row.get("branch_coverage") is not None else None,
            status=str(row.get("status") or "pending"),
            current_step=row.get("current_step"),
            a_round=int(row.get("a_round") or 0),
            task_a_logic_retry=int(row.get("task_a_logic_retry") or 0),
            task_a_syntax_retry_consecutive=int(
                row.get("task_a_syntax_retry_consecutive") or 0
            ),
            task_a_syntax_retry_total=int(
                row.get("task_a_syntax_retry_total") or 0
            ),
            pbt_pass_count=int(row.get("pbt_pass_count") or 0),
            oj_pass_count=int(row.get("oj_pass_count") or 0),
            input_gen_retry_total=int(row.get("input_gen_retry_total") or 0),
            pbt_compile_retry_consecutive=int(
                row.get("pbt_compile_retry_consecutive") or 0
            ),
            pbt_compile_retry_total=int(row.get("pbt_compile_retry_total") or 0),
            task_a_insufficient_input_consecutive=int(
                row.get("task_a_insufficient_input_consecutive") or 0
            ),
            task_a_insufficient_input_total=int(
                row.get("task_a_insufficient_input_total") or 0
            ),
            mt_oj_feedback_round=int(row.get("mt_oj_feedback_round") or 0),
            mt_rp_feedback_round=int(row.get("mt_rp_feedback_round") or 0),
            rp_disabled=bool(row.get("rp_disabled") or 0),
        )

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def to_db_dict(self) -> Dict[str, Any]:
        """返回 DB 持久化字段子集（供 P7 编排器刷写 DB 用）。

        临时字段不入库。
        """
        return {
            "id": self.id,
            "source_dataset_name": self.source_dataset_name,
            "source_dataset_index": self.source_dataset_index,
            "source_task_id": self.source_task_id,
            "source_machine": self.source_machine,
            "nl_description": self.nl_description,
            "method_code": self.method_code,
            "full_code": self.full_code,
            "input_generator_code": self.input_generator_code,
            "branch_coverage": self.branch_coverage,
            "status": self.status,
            "current_step": self.current_step,
            "a_round": self.a_round,
            "task_a_logic_retry": self.task_a_logic_retry,
            "task_a_syntax_retry_consecutive": self.task_a_syntax_retry_consecutive,
            "task_a_syntax_retry_total": self.task_a_syntax_retry_total,
            "pbt_pass_count": self.pbt_pass_count,
            "oj_pass_count": self.oj_pass_count,
            "input_gen_retry_total": self.input_gen_retry_total,
            "pbt_compile_retry_consecutive": self.pbt_compile_retry_consecutive,
            "pbt_compile_retry_total": self.pbt_compile_retry_total,
            "task_a_insufficient_input_consecutive": self.task_a_insufficient_input_consecutive,
            "task_a_insufficient_input_total": self.task_a_insufficient_input_total,
            "mt_oj_feedback_round": self.mt_oj_feedback_round,
            "mt_rp_feedback_round": self.mt_rp_feedback_round,
            "rp_disabled": self.rp_disabled,
        }

    def reset_round_temp(self) -> None:
        """重置当前轮次的临时字段（用于新一轮 Task A 开始时）。

        保留：``a_round`` / 计数器 / DB 持久化字段。
        清空：``current_*`` 临时字段。

        **不清** ``last_*`` 反馈字段——它们是上一阶段（OJ / RAC / PBT / MT）
        给本轮 task_a 的反馈输入，必须保留到 ``_select_retry_template``
        消费完成后才能清空（清空职责由 ``TaskAProcessor._post_llm_pipeline``
        承担）。早清会导致兜底 prompt 替换真实反馈，已踩过坑。
        """
        self.current_jml_java = None
        self.current_input_gen_code = None
        self.current_message_id = None
        self.rac_class_name = None
        self.rac_output_dir = None
