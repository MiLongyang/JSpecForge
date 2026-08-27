"""``task_llm_messages`` 表 CRUD（DAO）。

依据 docs/design/数据库表字段设计.md §四 字段定义和 §九 写入时序。

使用规则要点：

- ``session_type='task_a'`` 时 ``a_round`` 固定为 0（不分组）；
  ``session_type='input_gen'`` 时 ``a_round`` 是分组键，每个 a_round
  从 ``seq=0`` 重新计数。
- 角色相关的结果列（``is_oj_pass`` / ``is_pbt_pass`` / ``action_taken``
  / ``compile_status`` / ``compile_error`` / ``is_final_success``）只对
  对应会话的 assistant 行有意义。LLM 返回后先 INSERT，结果列保持 NULL
  作为 pending 标记；验证 / 编译完成后调用对应 update_* 回填。
- ``call_meta`` / 失败样本等 JSON 字段：DAO 层做 ``json.dumps`` /
  ``json.loads`` 转换，对外暴露 ``dict``。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

from jspecforge.db.enums import MessageRole, SessionType


def _session_value(s: Union[str, SessionType]) -> str:
    return s.value if isinstance(s, SessionType) else s


def _role_value(r: Union[str, MessageRole]) -> str:
    return r.value if isinstance(r, MessageRole) else r


def _decode_call_meta(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """对 SELECT 结果中的 call_meta 做 json.loads 转换（如果是字符串）。"""
    if row is None:
        return None
    raw = row.get("call_meta")
    if isinstance(raw, (str, bytes, bytearray)):
        row["call_meta"] = json.loads(raw)
    return row


class MessageDAO:
    """``task_llm_messages`` 表的 CRUD 操作。"""

    # ─────────────── 创建 ───────────────

    @staticmethod
    def insert(
        conn: Any,
        *,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int,
        seq: int,
        round: int,
        role: Union[str, MessageRole],
        content: str,
        call_meta: Optional[Dict[str, Any]] = None,
    ) -> int:
        """通用插入；返回生成的 message id。

        所有结果标记列（is_oj_pass 等）默认 NULL，表示 pending。
        非 assistant 角色 / 非对应会话的结果列由调用方负责保持 NULL。
        """
        sql = (
            "INSERT INTO task_llm_messages "
            "(task_id, session_type, a_round, seq, round, role, content, call_meta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        )
        meta_json = json.dumps(call_meta, ensure_ascii=False) if call_meta is not None else None
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    task_id,
                    _session_value(session_type),
                    a_round,
                    seq,
                    round,
                    _role_value(role),
                    content,
                    meta_json,
                ),
            )
            return int(cur.lastrowid)

    # ─────────────── 序号生成 ───────────────

    @staticmethod
    def next_seq(
        conn: Any,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int = 0,
    ) -> int:
        """返回下一条消息的 seq：当前最大 seq + 1，无记录则 0。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), -1) AS max_seq FROM task_llm_messages "
                "WHERE task_id = %s AND session_type = %s AND a_round = %s",
                (task_id, _session_value(session_type), a_round),
            )
            row = cur.fetchone()
        return int(row["max_seq"]) + 1

    # ─────────────── 查询 ───────────────

    @staticmethod
    def get_by_id(conn: Any, message_id: int) -> Optional[Dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM task_llm_messages WHERE id = %s",
                (message_id,),
            )
            return _decode_call_meta(cur.fetchone())

    @staticmethod
    def list_session(
        conn: Any,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int = 0,
    ) -> List[Dict[str, Any]]:
        """返回单个会话内 seq 升序的所有消息。

        恢复 LLM 对话上下文用：input_gen 必须传 a_round；task_a 留默认 0 即可。
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM task_llm_messages "
                "WHERE task_id = %s AND session_type = %s AND a_round = %s "
                "ORDER BY seq ASC",
                (task_id, _session_value(session_type), a_round),
            )
            rows = list(cur.fetchall())
        for r in rows:
            _decode_call_meta(r)
        return rows

    @staticmethod
    def get_latest_assistant(
        conn: Any,
        task_id: int,
        session_type: Union[str, SessionType],
        a_round: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """获取会话内最后一条 ``assistant`` 消息（用于中断恢复 pending 检测）。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM task_llm_messages "
                "WHERE task_id = %s AND session_type = %s "
                "  AND a_round = %s AND role = 'assistant' "
                "ORDER BY seq DESC LIMIT 1",
                (task_id, _session_value(session_type), a_round),
            )
            row = cur.fetchone()
        return _decode_call_meta(row)

    @staticmethod
    def count_input_gen_retries_in_round(
        conn: Any,
        task_id: int,
        a_round: int,
    ) -> int:
        """当前 a_round 内已完成且失败的 input_gen 重试次数。

        计算方式与设计文档 §十 中的运行时查询一致：
        ``compile_status IS NOT NULL AND is_final_success = 0`` 的 assistant
        条数。
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM task_llm_messages "
                "WHERE task_id = %s AND session_type = 'input_gen' "
                "  AND a_round = %s AND role = 'assistant' "
                "  AND compile_status IS NOT NULL AND is_final_success = 0",
                (task_id, a_round),
            )
            row = cur.fetchone()
        return int(row["cnt"])

    # ─────────────── 结果回填 ───────────────

    @staticmethod
    def update_task_a_result(
        conn: Any,
        message_id: int,
        *,
        is_oj_pass: Optional[bool],
        is_pbt_pass: Optional[bool],
        action_taken: Optional[str],
    ) -> None:
        """回填 task_a assistant 行的 OJ/PBT/action 结果列。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE task_llm_messages SET "
                "  is_oj_pass = %s, is_pbt_pass = %s, action_taken = %s "
                "WHERE id = %s",
                (
                    None if is_oj_pass is None else int(bool(is_oj_pass)),
                    None if is_pbt_pass is None else int(bool(is_pbt_pass)),
                    action_taken,
                    message_id,
                ),
            )

    @staticmethod
    def update_input_gen_result(
        conn: Any,
        message_id: int,
        *,
        compile_status: Optional[str],
        compile_error: Optional[str],
        is_final_success: Optional[bool],
    ) -> None:
        """回填 input_gen assistant 行的编译结果列。"""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE task_llm_messages SET "
                "  compile_status = %s, compile_error = %s, is_final_success = %s "
                "WHERE id = %s",
                (
                    compile_status,
                    compile_error,
                    None if is_final_success is None else int(bool(is_final_success)),
                    message_id,
                ),
            )

    @staticmethod
    def update_call_meta(
        conn: Any,
        message_id: int,
        call_meta: Optional[Dict[str, Any]],
    ) -> None:
        """单独回填 / 修正 ``call_meta`` 列。"""
        meta_json = json.dumps(call_meta, ensure_ascii=False) if call_meta is not None else None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE task_llm_messages SET call_meta = %s WHERE id = %s",
                (meta_json, message_id),
            )
