"""``oj_attempts`` 表 CRUD（DAO）。

依据 docs/design/数据库表字段设计.md §五。每个 Task A round 产生一条记录；
黑名单命中时以 ``status='skipped_blacklist'`` 记录、``exit_code`` /
``elapsed_seconds`` / ``detail_*`` 全部 NULL。

``(task_id, a_round)`` 是唯一键。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class OJAttemptDAO:
    """``oj_attempts`` 表的 CRUD 操作。"""

    @staticmethod
    def insert(
        conn: Any,
        *,
        task_id: int,
        a_round: int,
        status: str,
        action_taken: str,
        exit_code: Optional[int] = None,
        elapsed_seconds: Optional[float] = None,
        detail_truncated: Optional[str] = None,
        detail_full_length: Optional[int] = None,
        full_java_with_jml: Optional[str] = None,
        mutation_score: Optional[float] = None,
    ) -> int:
        """插入一条 OJ 尝试记录，返回自增 id。重复键时更新。"""
        sql = (
            "INSERT INTO oj_attempts "
            "(task_id, a_round, status, exit_code, elapsed_seconds, "
            " detail_truncated, detail_full_length, full_java_with_jml, "
            " mutation_score, action_taken) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "status=VALUES(status), exit_code=VALUES(exit_code), "
            "elapsed_seconds=VALUES(elapsed_seconds), "
            "detail_truncated=VALUES(detail_truncated), "
            "detail_full_length=VALUES(detail_full_length), "
            "full_java_with_jml=VALUES(full_java_with_jml), "
            "mutation_score=VALUES(mutation_score), "
            "action_taken=VALUES(action_taken)"
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    task_id,
                    a_round,
                    status,
                    exit_code,
                    elapsed_seconds,
                    detail_truncated,
                    detail_full_length,
                    full_java_with_jml,
                    mutation_score,
                    action_taken,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def get_by_id(conn: Any, attempt_id: int) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM oj_attempts WHERE id = %s", (attempt_id,))
            return cur.fetchone()

    @staticmethod
    def get_by_task_round(
        conn: Any,
        task_id: int,
        a_round: int,
    ) -> Optional[Dict[str, Any]]:
        """按唯一键 (task_id, a_round) 查询。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM oj_attempts WHERE task_id = %s AND a_round = %s",
                (task_id, a_round),
            )
            return cur.fetchone()

    @staticmethod
    def list_by_task(conn: Any, task_id: int) -> List[Dict[str, Any]]:
        """按 a_round 升序返回任务的全部 OJ 记录（导出用）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM oj_attempts WHERE task_id = %s ORDER BY a_round ASC",
                (task_id,),
            )
            return list(cur.fetchall())

    @staticmethod
    def delete_by_task(conn: Any, task_id: int) -> int:
        """删除任务下所有 OJ 记录（测试 / 清理用）。返回受影响行数。"""
        with conn.cursor() as cur:
            cur.execute("DELETE FROM oj_attempts WHERE task_id = %s", (task_id,))
            return int(cur.rowcount)

    @staticmethod
    def get_best_passing(conn: Any, task_id: int) -> Optional[Dict[str, Any]]:
        """获取指定 task 的最优 OJ-pass 候选。

        两层逻辑：
        1. 优先选 mutation_score > 0 中最高的（按 score DESC, a_round DESC）
        2. 回退选任何 status='pass' 候选（按 a_round DESC 取最新）
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM oj_attempts "
                "WHERE task_id = %s AND status = 'pass' "
                "  AND mutation_score IS NOT NULL AND mutation_score > 0 "
                "ORDER BY mutation_score DESC, a_round DESC "
                "LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
            if row is not None:
                return row

            cur.execute(
                "SELECT * FROM oj_attempts "
                "WHERE task_id = %s AND status = 'pass' "
                "ORDER BY a_round DESC "
                "LIMIT 1",
                (task_id,),
            )
            return cur.fetchone()

    @staticmethod
    def update_mutation_score(
        conn: Any, task_id: int, a_round: int, mutation_score: float
    ) -> None:
        """MT 跑完后回填 mutation_score。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE oj_attempts SET mutation_score = %s "
                "WHERE task_id = %s AND a_round = %s",
                (mutation_score, task_id, a_round),
            )

    @staticmethod
    def update_mt_detail(
        conn: Any,
        task_id: int,
        a_round: int,
        *,
        killed: Optional[int],
        survived: Optional[int],
        total: Optional[int],
        feedback_text: Optional[str],
    ) -> None:
        """MT 跑完后回填 killed/survived/total/feedback_text 4 列。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE oj_attempts SET "
                " mt_killed = %s, mt_survived = %s, mt_total = %s, "
                " mt_feedback_text = %s "
                "WHERE task_id = %s AND a_round = %s",
                (killed, survived, total, feedback_text, task_id, a_round),
            )
