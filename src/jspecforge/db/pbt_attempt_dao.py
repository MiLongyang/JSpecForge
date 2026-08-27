"""``pbt_attempts`` 表 CRUD（DAO）。

依据 docs/design/数据库表字段设计.md §七。存储所有尝试（通过+失败），由
``(task_id, a_round, ig_round)`` 组合唯一标识一次 PBT 执行。

JSON 字段：

- ``blacklist_hits`` — 命中的黑名单关键字列表 ``["\\sum", ...]``，无命中时为 ``[]``
- ``failure_samples`` — 失败样本数组（结构见设计文档 §七）

DAO 层做 ``json.dumps`` / ``json.loads``，对外暴露 ``list`` / ``dict``。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


_JSON_COLUMNS = ("blacklist_hits", "failure_samples")


def _decode_json_columns(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    for col in _JSON_COLUMNS:
        raw = row.get(col)
        if isinstance(raw, (str, bytes, bytearray)):
            row[col] = json.loads(raw)
    return row


class PBTAttemptDAO:
    """``pbt_attempts`` 表的 CRUD 操作。"""

    @staticmethod
    def insert(
        conn: Any,
        *,
        task_id: int,
        a_round: int,
        ig_round: int,
        full_java_with_jml: str,
        input_generator_code: str,
        is_pass: bool,
        total_runs: int,
        seed: int,
        elapsed_seconds: float,
        action_taken: str,
        has_blacklist_syntax: bool = False,
        blacklist_hits: Optional[List[str]] = None,
        gen_error_count: int = 0,
        precondition_reject_count: int = 0,
        post_fail_count: int = 0,
        failure_samples: Optional[List[Dict[str, Any]]] = None,
        detail_truncated: Optional[str] = None,
        detail_full_length: int = 0,
        valid_input_ratio: Optional[float] = None,
    ) -> int:
        """插入一条 PBT 尝试记录，返回自增 id。

        D.6 (2026-05-03) 新增 detail_truncated + detail_full_length 字段:
        PBT TestDriver 编译/运行失败时填入截断后的错误信息;
        正常 PBT 测试运行时为 None / 0。

        P10 新增 valid_input_ratio 字段:
        有效输入占比 = (total_runs - precondition_reject_count) / total_runs。
        """
        sql = (
            "INSERT INTO pbt_attempts ("
            " task_id, a_round, ig_round, full_java_with_jml, input_generator_code,"
            " has_blacklist_syntax, blacklist_hits, is_pass, total_runs,"
            " gen_error_count, precondition_reject_count, post_fail_count,"
            " seed, elapsed_seconds, failure_samples, action_taken,"
            " detail_truncated, detail_full_length, valid_input_ratio"
            ") VALUES ("
            " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE "
            "full_java_with_jml=VALUES(full_java_with_jml), "
            "input_generator_code=VALUES(input_generator_code), "
            "has_blacklist_syntax=VALUES(has_blacklist_syntax), "
            "blacklist_hits=VALUES(blacklist_hits), "
            "is_pass=VALUES(is_pass), total_runs=VALUES(total_runs), "
            "gen_error_count=VALUES(gen_error_count), "
            "precondition_reject_count=VALUES(precondition_reject_count), "
            "post_fail_count=VALUES(post_fail_count), "
            "seed=VALUES(seed), elapsed_seconds=VALUES(elapsed_seconds), "
            "failure_samples=VALUES(failure_samples), "
            "action_taken=VALUES(action_taken), "
            "detail_truncated=VALUES(detail_truncated), "
            "detail_full_length=VALUES(detail_full_length), "
            "valid_input_ratio=VALUES(valid_input_ratio)"
        )
        bl_json = json.dumps(blacklist_hits if blacklist_hits is not None else [], ensure_ascii=False)
        fs_json = (
            json.dumps(failure_samples, ensure_ascii=False)
            if failure_samples is not None
            else None
        )
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    task_id,
                    a_round,
                    ig_round,
                    full_java_with_jml,
                    input_generator_code,
                    int(bool(has_blacklist_syntax)),
                    bl_json,
                    int(bool(is_pass)),
                    total_runs,
                    gen_error_count,
                    precondition_reject_count,
                    post_fail_count,
                    seed,
                    elapsed_seconds,
                    fs_json,
                    action_taken,
                    detail_truncated,
                    detail_full_length,
                    valid_input_ratio,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def get_by_id(conn: Any, attempt_id: int) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM pbt_attempts WHERE id = %s", (attempt_id,))
            return _decode_json_columns(cur.fetchone())

    @staticmethod
    def get_by_task_round_ig(
        conn: Any,
        task_id: int,
        a_round: int,
        ig_round: int,
    ) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pbt_attempts "
                "WHERE task_id = %s AND a_round = %s AND ig_round = %s",
                (task_id, a_round, ig_round),
            )
            return _decode_json_columns(cur.fetchone())

    @staticmethod
    def list_by_task(conn: Any, task_id: int) -> List[Dict[str, Any]]:
        """按 (a_round, ig_round) 升序返回任务的全部 PBT 记录（导出用）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pbt_attempts WHERE task_id = %s "
                "ORDER BY a_round ASC, ig_round ASC",
                (task_id,),
            )
            rows = list(cur.fetchall())
        for r in rows:
            _decode_json_columns(r)
        return rows

    @staticmethod
    def list_passing(conn: Any, task_id: int) -> List[Dict[str, Any]]:
        """获取任务下 ``is_pass=1`` 的候选列表（用于 RP 路径选最终答案）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pbt_attempts "
                "WHERE task_id = %s AND is_pass = 1 "
                "ORDER BY a_round ASC, ig_round ASC",
                (task_id,),
            )
            rows = list(cur.fetchall())
        for r in rows:
            _decode_json_columns(r)
        return rows

    @staticmethod
    def get_best_passing(conn: Any, task_id: int) -> Optional[Dict[str, Any]]:
        """获取指定 task 的最优 RP-pass 候选。

        两层逻辑：
        1. 优先选 mutation_score > 0 中最高的（按 score DESC, a_round DESC, ig_round DESC）
        2. 回退选任何 is_pass=1 候选（按 a_round DESC, ig_round DESC 取最新）
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM pbt_attempts "
                "WHERE task_id = %s AND is_pass = 1 "
                "  AND mutation_score IS NOT NULL AND mutation_score > 0 "
                "ORDER BY mutation_score DESC, a_round DESC, ig_round DESC "
                "LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
            if row is not None:
                return _decode_json_columns(row)

            cur.execute(
                "SELECT * FROM pbt_attempts "
                "WHERE task_id = %s AND is_pass = 1 "
                "ORDER BY a_round DESC, ig_round DESC "
                "LIMIT 1",
                (task_id,),
            )
            return _decode_json_columns(cur.fetchone())

    @staticmethod
    def update_mutation_score(
        conn: Any,
        attempt_id: int,
        score: Optional[float],
    ) -> None:
        """MT 后处理脚本回填 mutation_score。主流水线不调用。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pbt_attempts SET mutation_score = %s WHERE id = %s",
                (score, attempt_id),
            )

    @staticmethod
    def update_mutation_score_by_round(
        conn: Any,
        task_id: int,
        a_round: int,
        ig_round: int,
        mutation_score: float,
    ) -> None:
        """内联 MT 跑完后按 (task_id, a_round, ig_round) 回填 mutation_score。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pbt_attempts SET mutation_score = %s "
                "WHERE task_id = %s AND a_round = %s AND ig_round = %s",
                (mutation_score, task_id, a_round, ig_round),
            )

    @staticmethod
    def update_mt_detail(
        conn: Any,
        task_id: int,
        a_round: int,
        ig_round: int,
        *,
        killed: Optional[int],
        survived: Optional[int],
        total: Optional[int],
        feedback_text: Optional[str],
    ) -> None:
        """MT 跑完后回填 killed/survived/total/feedback_text 4 列。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pbt_attempts SET "
                " mt_killed = %s, mt_survived = %s, mt_total = %s, "
                " mt_feedback_text = %s "
                "WHERE task_id = %s AND a_round = %s AND ig_round = %s",
                (killed, survived, total, feedback_text, task_id, a_round, ig_round),
            )

    @staticmethod
    def delete_by_task(conn: Any, task_id: int) -> int:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pbt_attempts WHERE task_id = %s", (task_id,))
            return int(cur.rowcount)
