"""``rac_attempts`` 表 CRUD（DAO）。

依据 docs/design/数据库表字段设计.md §六。仅在进入 RP 路径时产生记录，
每次进入 RP，RAC 只执行一次。``(task_id, a_round)`` 是唯一键。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class RACAttemptDAO:
    """``rac_attempts`` 表的 CRUD 操作。"""

    @staticmethod
    def insert(
        conn: Any,
        *,
        task_id: int,
        a_round: int,
        status: str,
        exit_code: int,
        elapsed_seconds: float,
        action_taken: str,
        detail_truncated: Optional[str] = None,
        detail_full_length: int = 0,
    ) -> int:
        """插入一条 RAC 尝试记录，返回自增 id。重复键时更新。"""
        sql = (
            "INSERT INTO rac_attempts "
            "(task_id, a_round, status, exit_code, elapsed_seconds, "
            " detail_truncated, detail_full_length, action_taken) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "status=VALUES(status), exit_code=VALUES(exit_code), "
            "elapsed_seconds=VALUES(elapsed_seconds), "
            "detail_truncated=VALUES(detail_truncated), "
            "detail_full_length=VALUES(detail_full_length), "
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
                    action_taken,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def get_by_id(conn: Any, attempt_id: int) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rac_attempts WHERE id = %s", (attempt_id,))
            return cur.fetchone()

    @staticmethod
    def get_by_task_round(
        conn: Any,
        task_id: int,
        a_round: int,
    ) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM rac_attempts WHERE task_id = %s AND a_round = %s",
                (task_id, a_round),
            )
            return cur.fetchone()

    @staticmethod
    def list_by_task(conn: Any, task_id: int) -> List[Dict[str, Any]]:
        """按 a_round 升序返回任务的全部 RAC 记录（导出用）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM rac_attempts WHERE task_id = %s ORDER BY a_round ASC",
                (task_id,),
            )
            return list(cur.fetchall())

    @staticmethod
    def delete_by_task(conn: Any, task_id: int) -> int:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rac_attempts WHERE task_id = %s", (task_id,))
            return int(cur.rowcount)
