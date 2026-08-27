"""``tasks`` 表 CRUD（DAO）。

依据 docs/design/数据库表字段设计.md §三 的字段定义和 §九 的写入时序。

DAO 函数风格：

- 类名 :class:`TaskDAO` 仅作命名空间，所有方法均为 ``@staticmethod``
- 每个方法第一参数为 ``conn``（PyMySQL 连接或池借出的连接）
- 不在内部调用 ``conn.commit()`` —— 事务边界由调用方控制
- SELECT 类方法返回 ``dict`` 或 ``list[dict]``（DictCursor）
- 所有 SQL 使用参数化查询；动态列名走白名单
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from jspecforge.db.enums import TaskStatus

# 允许 increment_counter 操作的列白名单，防止 SQL 注入
_INCREMENTABLE_COLUMNS = frozenset(
    {
        "task_a_logic_retry",
        "task_a_syntax_retry_consecutive",
        "task_a_syntax_retry_total",
        "pbt_pass_count",
        # P13: OJ ESC 通过候选累计数
        "oj_pass_count",
        "input_gen_retry_total",
        # D.8 (2026-05-03): PBT TestDriver 编译失败独立计数器
        "pbt_compile_retry_consecutive",
        "pbt_compile_retry_total",
        # Phase 4 (2026-05-13): PBT 有效输入不足独立计数器
        "task_a_insufficient_input_consecutive",
        "task_a_insufficient_input_total",
        # P11: MT 反馈循环计数器
        "mt_oj_feedback_round",
        "mt_rp_feedback_round",
    }
)


def _status_value(status: Union[str, TaskStatus]) -> str:
    return status.value if isinstance(status, TaskStatus) else status


class TaskDAO:
    """``tasks`` 表的 CRUD 操作。"""

    # ─────────────── 创建 ───────────────

    @staticmethod
    def insert(
        conn: Any,
        *,
        source_dataset_name: str,
        source_dataset_index: int,
        source_task_id: str,
        source_machine: str,
        nl_description: str,
        method_code: str,
        full_code: str,
        input_generator_code: Optional[str] = None,
        branch_coverage: Optional[float] = None,
    ) -> int:
        """插入新任务（status=pending），返回生成的自增 id。"""
        sql = (
            "INSERT INTO tasks "
            "(source_dataset_name, source_dataset_index, source_task_id, "
            " source_machine, nl_description, method_code, full_code, "
            " input_generator_code, branch_coverage) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    source_dataset_name,
                    source_dataset_index,
                    source_task_id,
                    source_machine,
                    nl_description,
                    method_code,
                    full_code,
                    input_generator_code,
                    branch_coverage,
                ),
            )
            return int(cur.lastrowid)

    # ─────────────── 查询 ───────────────

    @staticmethod
    def get_by_id(conn: Any, task_id: int) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            return cur.fetchone()

    @staticmethod
    def get_runtime_counters(conn: Any, task_id: int) -> Optional[Dict[str, Any]]:
        """读取运行时终止判断依赖的轻量计数字段。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a_round, task_a_logic_retry, "
                "task_a_syntax_retry_consecutive, task_a_syntax_retry_total, "
                "pbt_pass_count, oj_pass_count, "
                "task_a_insufficient_input_consecutive, "
                "task_a_insufficient_input_total, mt_oj_feedback_round, "
                "mt_rp_feedback_round, rp_disabled "
                "FROM tasks WHERE id = %s",
                (task_id,),
            )
            return cur.fetchone()

    @staticmethod
    def get_by_source(
        conn: Any,
        dataset_name: str,
        dataset_index: int,
    ) -> Optional[Dict[str, Any]]:
        """按唯一键 (source_dataset_name, source_dataset_index) 查询。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tasks "
                "WHERE source_dataset_name = %s AND source_dataset_index = %s",
                (dataset_name, dataset_index),
            )
            return cur.fetchone()

    @staticmethod
    def list_by_status(
        conn: Any,
        status: Union[str, TaskStatus],
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE status = %s ORDER BY id ASC"
        params: List[Any] = [_status_value(status)]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall())

    @staticmethod
    def list_in_progress(conn: Any) -> List[Dict[str, Any]]:
        """供中断恢复使用：列出全部 status='in_progress' 的任务。"""
        return TaskDAO.list_by_status(conn, TaskStatus.IN_PROGRESS)

    @staticmethod
    def count_by_status(conn: Any) -> Dict[str, int]:
        """监控面板：按 status 统计任务数。返回所有 4 个枚举值（缺失补 0）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
            )
            rows = cur.fetchall()
        result: Dict[str, int] = {s.value: 0 for s in TaskStatus}
        for row in rows:
            result[row["status"]] = int(row["cnt"])
        return result

    @staticmethod
    def count_in_progress_by_step(conn: Any) -> Dict[str, int]:
        """监控面板：处理中任务的 current_step 分布。NULL 归入 ``"_unknown"``。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_step, COUNT(*) AS cnt FROM tasks "
                "WHERE status = 'in_progress' GROUP BY current_step"
            )
            rows = cur.fetchall()
        result: Dict[str, int] = {}
        for row in rows:
            key = row["current_step"] if row["current_step"] is not None else "_unknown"
            result[key] = int(row["cnt"])
        return result

    # ─────────────── 状态更新 ───────────────

    @staticmethod
    def mark_in_progress(conn: Any, task_id: int) -> None:
        """首次进入处理：status → in_progress，started_at 设为当前时间。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status = 'in_progress', "
                "started_at = COALESCE(started_at, CURRENT_TIMESTAMP) "
                "WHERE id = %s",
                (task_id,),
            )

    @staticmethod
    def update_step(conn: Any, task_id: int, step: Optional[str]) -> None:
        """更新 current_step；step=None 表示置空。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET current_step = %s WHERE id = %s",
                (step, task_id),
            )

    @staticmethod
    def update_a_round(conn: Any, task_id: int, a_round: int) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET a_round = %s WHERE id = %s",
                (a_round, task_id),
            )

    # ─────────────── 计数器 ───────────────

    @staticmethod
    def increment_counter(
        conn: Any,
        task_id: int,
        field: str,
        delta: int = 1,
    ) -> None:
        """对计数器列做 ``col = col + delta``。

        Args:
            field: 必须是 :data:`_INCREMENTABLE_COLUMNS` 中的列名。
            delta: 默认 +1。
        """
        if field not in _INCREMENTABLE_COLUMNS:
            raise ValueError(f"不允许递增的列名: {field!r}")
        sql = f"UPDATE tasks SET `{field}` = `{field}` + %s WHERE id = %s"
        with conn.cursor() as cur:
            cur.execute(sql, (delta, task_id))

    @staticmethod
    def reset_syntax_consecutive(conn: Any, task_id: int) -> None:
        """语法正确时重置 task_a_syntax_retry_consecutive 为 0。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET task_a_syntax_retry_consecutive = 0 "
                "WHERE id = %s",
                (task_id,),
            )

    @staticmethod
    def reset_insufficient_input_consecutive(conn: Any, task_id: int) -> None:
        """PBT valid_ratio 达标时重置 task_a_insufficient_input_consecutive 为 0。

        Phase 4 (2026-05-13) 引入: insufficient_input 的 consecutive 计数
        与 syntax_consecutive 同语义, 仅"当次有效输入足够"就清零。
        """
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET task_a_insufficient_input_consecutive = 0 "
                "WHERE id = %s",
                (task_id,),
            )

    @staticmethod
    def update_rp_disabled(
        conn: Any, task_id: int, disabled: bool
    ) -> None:
        """置位/清除本任务的 RP 通道关闭标志（P13）。

        由 RP 候选满 / RP 路径 MT 达标触发 ``disabled=True``。
        持久化到 DB, 支持重启恢复。
        """
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET rp_disabled = %s WHERE id = %s",
                (1 if disabled else 0, task_id),
            )

    # ─────────────── 终态 ───────────────

    @staticmethod
    def mark_success(
        conn: Any,
        task_id: int,
        *,
        path: str,
        final_full_java_with_jml: str,
        final_a_round: int,
        final_selected_candidate_index: Optional[int] = None,
    ) -> None:
        """成功终态：status=success + path + final_* + finished_at + current_step=NULL。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET "
                "  status = 'success', "
                "  current_step = NULL, "
                "  path = %s, "
                "  final_full_java_with_jml = %s, "
                "  final_a_round = %s, "
                "  final_selected_candidate_index = %s, "
                "  finished_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (
                    path,
                    final_full_java_with_jml,
                    final_a_round,
                    final_selected_candidate_index,
                    task_id,
                ),
            )

    @staticmethod
    def mark_failed(
        conn: Any,
        task_id: int,
        *,
        failure_stage: str,
        failure_reason: str,
        last_error_detail: Optional[str] = None,
    ) -> None:
        """失败终态：status=failed + failure_* + finished_at + current_step=NULL。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET "
                "  status = 'failed', "
                "  current_step = NULL, "
                "  failure_stage = %s, "
                "  failure_reason = %s, "
                "  last_error_detail = %s, "
                "  finished_at = CURRENT_TIMESTAMP "
                "WHERE id = %s",
                (failure_stage, failure_reason, last_error_detail, task_id),
            )

    # ─────────────── 删除（测试 / 清理用）───────────────

    @staticmethod
    def delete(conn: Any, task_id: int) -> int:
        """物理删除一行，返回受影响行数。生产环境通常不调用此方法。"""
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            return int(cur.rowcount)
