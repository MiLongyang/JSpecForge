"""``oj_bug_reports`` 表 CRUD（DAO）。

P10 Phase 4 (2026-05-13) 引入。

触发条件 (2026-05-16 泛化):
OJ ESC 已通过语法校验 (last_oj_label ∈ {LOGIC_ERROR, OPENJML_TIMEOUT,
HARNESS_TIMEOUT, UNSUPPORTED_JML}) 但 RAC 仍编译失败时, 视为 OJ 工具自身
bug, 抽取错误首行作为 ``error_pattern`` 写入一行。原先仅匹配
``NoSuchMethodError: <name>``, 现已不再枚举具体模式, 避免每出新报错都要
扩白名单。

终止判定 (双阈值):
- 同一 task 同一 ``error_pattern`` *连续* 出现达
  ``retry.rac_error_pattern_max`` (A=3) 次 → 标 ``failed_oj_bug`` 终止;
- 同一 task 不区分 pattern 累计达 ``retry.rac_oj_bug_total_max`` (B=8) 次
  → 同上终止, 用于拦截不同 pattern 交替导致 A 永远清零的死循环。

注意:
- 不写 unique key —— 同一 task 同一 a_round 同一 pattern 可能重复出现
  (PBT 失败后回 task_a 重新生成 JML 又触发, 等等), 都记录便于统计。
- ``error_pattern`` 上有索引以支撑跨任务计数查询。
- 仅黑名单跳过 OJ 的路径 (没经过 OJ 校验) 不写本表, 走正常 syntax 重试。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class OJBugReportDAO:
    """``oj_bug_reports`` 表的 CRUD 操作。"""

    @staticmethod
    def insert(
        conn: Any,
        *,
        error_pattern: str,
        task_id: int,
        a_round: int,
        jml_source: Optional[str] = None,
        method_code: Optional[str] = None,
        rac_detail: Optional[str] = None,
    ) -> int:
        """插入一条 OJ bug 报告记录, 返回自增 id。"""
        sql = (
            "INSERT INTO oj_bug_reports "
            "(error_pattern, task_id, a_round, jml_source, method_code, rac_detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    error_pattern,
                    task_id,
                    a_round,
                    jml_source,
                    method_code,
                    rac_detail,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def count_by_pattern_for_task(
        conn: Any,
        task_id: int,
        error_pattern: str,
    ) -> int:
        """统计某 task 上同一错误模式累计出现次数, 用于触发终止判定。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM oj_bug_reports "
                "WHERE task_id = %s AND error_pattern = %s",
                (task_id, error_pattern),
            )
            row = cur.fetchone()
        return int(row["cnt"])

    @staticmethod
    def count_by_task(conn: Any, task_id: int) -> int:
        """统计某 task 不区分 pattern 的 OJ-bug 累计次数 (参数 B 阈值用)。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM oj_bug_reports WHERE task_id = %s",
                (task_id,),
            )
            row = cur.fetchone()
        return int(row["cnt"])

    @staticmethod
    def list_by_task(conn: Any, task_id: int) -> List[Dict[str, Any]]:
        """按时间升序返回某 task 所有打标记录（导出/复盘用）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM oj_bug_reports WHERE task_id = %s ORDER BY id ASC",
                (task_id,),
            )
            return list(cur.fetchall())

    @staticmethod
    def list_patterns(conn: Any) -> List[Dict[str, Any]]:
        """按出现总次数倒序列出所有错误模式 (运维/统计脚本用)。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_pattern, COUNT(*) AS cnt "
                "FROM oj_bug_reports GROUP BY error_pattern ORDER BY cnt DESC"
            )
            return list(cur.fetchall())

    @staticmethod
    def delete_by_task(conn: Any, task_id: int) -> int:
        """物理删除某 task 的所有打标行 (重置脚本用 —— 不删, 见 plan 6.3 注释)。

        注: Phase 4 重置脚本(``scripts/reset_oj_bug_tasks.py``)默认 **保留**
        oj_bug_reports 历史以供后续分析；本方法仅在显式需要清理时调用。
        """
        with conn.cursor() as cur:
            cur.execute("DELETE FROM oj_bug_reports WHERE task_id = %s", (task_id,))
            return int(cur.rowcount)
