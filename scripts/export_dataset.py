"""数据集导出脚本(P8)。

从 MySQL 5 张表(tasks / task_llm_messages / oj_attempts / rac_attempts /
pbt_attempts) 提取已完成任务,生成 ``success_<ts>.jsonl`` +
``failed_<ts>.jsonl``,严格对齐 ``docs/design/数据集导出字段设计.md``。

用法::

    # 最简
    python scripts/export_dataset.py

    # 指定 config
    python scripts/export_dataset.py --config /path/to/config.yaml

    # 输出目录覆盖 config.paths.output_dir
    python scripts/export_dataset.py --output-dir D:/dump

    # 自定义本机标识(默认从 config.task_loading.source_machine 读)
    python scripts/export_dataset.py --source-machine cloud_server_1

    # 仅导出某一类
    python scripts/export_dataset.py --only success
    python scripts/export_dataset.py --only failed

    # 仅导出某来源数据集
    python scripts/export_dataset.py --dataset-name Code2Bench

    # 模式参数(独立测试用,主流程不要传)
    python scripts/export_dataset.py --no-llm-history       # 不输出 llm_history
    python scripts/export_dataset.py --no-call-meta         # 不输出 call_meta
    python scripts/export_dataset.py --no-failure-samples   # 不输出 failure_samples

输出结构(用户要求文件夹+文件双时间戳)::

    output/                          ← config.paths.output_dir
    └── 20260503_143022/             ← 时间戳子目录
        ├── success_20260503_143022.jsonl
        └── failed_20260503_143022.jsonl

同名冲突处理(同一秒撞车): 整个时间戳目录追加 ``_2``/``_3``,文件名内的
时间戳同步更新,保持目录名与文件内时间戳一致。

跳过的任务:
- ``status=pending`` / ``status=in_progress`` 的任务**不导出,静默跳过**
  (主流水线还没跑完的任务不算最终结果)
- 仅导出 ``status IN ('success','failed')``

字段语义参考: docs/design/数据集导出字段设计.md。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── 把 src/ 加到 sys.path,与 scripts/load_tasks.py / run_mt.py 一致 ─────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.config.settings import AppConfig, ExportConfig  # noqa: E402
from jspecforge.db.connection import get_connection  # noqa: E402
from jspecforge.db.message_dao import MessageDAO  # noqa: E402
from jspecforge.db.oj_attempt_dao import OJAttemptDAO  # noqa: E402
from jspecforge.db.pbt_attempt_dao import PBTAttemptDAO  # noqa: E402
from jspecforge.db.rac_attempt_dao import RACAttemptDAO  # noqa: E402
from jspecforge.db.task_dao import TaskDAO  # noqa: E402

logger = logging.getLogger("scripts.export_dataset")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "P8 数据集导出: 从 DB 5 张表生成 success/failed jsonl, "
            "字段对齐 docs/design/数据集导出字段设计.md"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_ROOT / "config.yaml",
        help="config.yaml 路径(默认: 仓库根目录)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="导出根目录(覆盖 config.paths.output_dir)",
    )
    parser.add_argument(
        "--source-machine",
        type=str,
        default=None,
        help=(
            "覆盖 source_machine 字段(默认从 config.task_loading.source_machine 读取)。"
            "导出时会把每条记录的 source_machine 强制写为此值,方便多机合并时区分"
        ),
    )
    parser.add_argument(
        "--only",
        choices=("success", "failed", "both"),
        default="both",
        help="只导出某一类: success / failed / both(默认 both)",
    )
    parser.add_argument(
        "--dataset-name",
        type=str,
        default=None,
        help="只导出指定 source_dataset_name 的任务(默认: 全部)",
    )
    parser.add_argument(
        "--no-llm-history",
        action="store_true",
        help="不输出 llm_history(覆盖 config.export.include_llm_history)",
    )
    parser.add_argument(
        "--no-call-meta",
        action="store_true",
        help="不输出 call_meta(覆盖 config.export.include_call_meta)",
    )
    parser.add_argument(
        "--no-failure-samples",
        action="store_true",
        help="不输出 pbt_attempts[].failure_samples(覆盖 config.export.include_failure_samples)",
    )
    parser.add_argument(
        "--timestamp",
        type=str,
        default=None,
        help="覆盖默认时间戳(格式 YYYYMMDD_HHMMSS,主要用于测试和确定性导出)",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 共用类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportOptions:
    """导出运行期选项,把 CLI flag + ExportConfig 合并后的最终值。"""

    include_llm_history: bool
    include_call_meta: bool
    include_failure_samples: bool
    float_precision: int
    source_machine: str
    only: str  # "success" | "failed" | "both"
    dataset_name: Optional[str]

    @classmethod
    def from_args(
        cls,
        export_cfg: ExportConfig,
        args: argparse.Namespace,
        source_machine_default: str,
    ) -> "ExportOptions":
        return cls(
            include_llm_history=(
                export_cfg.include_llm_history and not args.no_llm_history
            ),
            include_call_meta=(
                export_cfg.include_call_meta and not args.no_call_meta
            ),
            include_failure_samples=(
                export_cfg.include_failure_samples
                and not args.no_failure_samples
            ),
            float_precision=int(export_cfg.float_precision),
            source_machine=(
                args.source_machine
                if args.source_machine
                else source_machine_default
            ),
            only=args.only,
            dataset_name=args.dataset_name,
        )


# ---------------------------------------------------------------------------
# 通用值转换
# ---------------------------------------------------------------------------


def _to_float(value: Any, precision: int) -> Optional[float]:
    """DECIMAL/int/float → float(round 到 precision 位)。None 透传。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    elif isinstance(value, (int, float)):
        value = float(value)
    else:
        # 字符串(如 "1.23") 也兜一下
        value = float(value)
    return round(value, precision)


def _to_int(value: Any) -> Optional[int]:
    """None 透传,其余强转 int。bool 也按 0/1 处理。"""
    if value is None:
        return None
    return int(value)


def _to_bool(value: Any) -> Optional[bool]:
    """MySQL TINYINT(1) 取出是 int,这里转 bool。None 透传。"""
    if value is None:
        return None
    return bool(int(value))


def _decode_json_field(value: Any) -> Any:
    """如果还是字符串,做一次 json.loads;dict/list/None 透传。"""
    if value is None:
        return None
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


# ---------------------------------------------------------------------------
# 顶层 task 行 → JSONL 顶层字段
# ---------------------------------------------------------------------------


def _base_record(task_row: Dict[str, Any], source_machine: str) -> Dict[str, Any]:
    """提取顶层固定字段(id 后面统一覆盖,source_machine 用 CLI 强制值)。

    与 design §二 顶层字段表对齐。``id`` 字段在导出末尾按
    ``source_dataset_index`` 排序后从 1 起重新分配。
    """
    return {
        "id": int(task_row["id"]),  # 暂用 DB id,write 时再重排
        "source_dataset_name": task_row.get("source_dataset_name"),
        "source_dataset_index": _to_int(task_row.get("source_dataset_index")),
        "source_task_id": task_row.get("source_task_id"),
        "source_machine": source_machine,
        "nl_description": task_row.get("nl_description"),
        "method_code": task_row.get("method_code"),
        "full_code": task_row.get("full_code"),
        "input_generator_code": task_row.get("input_generator_code"),
        "branch_coverage": _to_float(task_row.get("branch_coverage"), 4),
        "path": task_row.get("path"),  # success 时有值,failed 时 null
    }


def _extract_mt_detail(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 attempt 行提取 mt_detail 对象。4 列全 NULL 时返回 None。"""
    killed = _to_int(row.get("mt_killed"))
    survived = _to_int(row.get("mt_survived"))
    total = _to_int(row.get("mt_total"))
    feedback_text = row.get("mt_feedback_text")
    if killed is None and survived is None and total is None and feedback_text is None:
        return None
    return {
        "killed": killed,
        "survived": survived,
        "total": total,
        "feedback_text": feedback_text,
    }


def _final_answer(
    task_row: Dict[str, Any],
    *,
    is_success: bool,
    conn: Any = None,
) -> Optional[Dict[str, Any]]:
    """构建 final_answer 对象。失败任务返回 None(对应 jsonl null)。

    双路径结构 ``{"oj": {...} | null, "rp": {...} | null}``:
    - 每条路径独立查 ``get_best_passing``,无候选则该子对象为 null
    - mt_detail 暂为 None,等待 mt_processor 落库后回填
    - 顶层 ``path`` 字段已在 ``_base_record`` 标识 final_processor 选定的赢家
    """
    if not is_success or conn is None:
        return None if not is_success else {"oj": None, "rp": None}

    task_id = int(task_row["id"])

    oj_part: Optional[Dict[str, Any]] = None
    oj_best = OJAttemptDAO.get_best_passing(conn, task_id)
    if oj_best is not None:
        oj_part = {
            "full_java_with_jml": oj_best.get("full_java_with_jml"),
            "a_round": _to_int(oj_best.get("a_round")),
            "mutation_score": _to_float(oj_best.get("mutation_score"), 4),
            "mt_detail": _extract_mt_detail(oj_best),
        }

    rp_part: Optional[Dict[str, Any]] = None
    pbt_best = PBTAttemptDAO.get_best_passing(conn, task_id)
    if pbt_best is not None:
        rp_part = {
            "full_java_with_jml": pbt_best.get("full_java_with_jml"),
            "a_round": _to_int(pbt_best.get("a_round")),
            "selected_candidate_index": _to_int(pbt_best.get("ig_round")),
            "mutation_score": _to_float(pbt_best.get("mutation_score"), 4),
            "mt_detail": _extract_mt_detail(pbt_best),
        }

    return {"oj": oj_part, "rp": rp_part}


def _failure_info(
    task_row: Dict[str, Any],
    *,
    is_success: bool,
) -> Optional[Dict[str, Any]]:
    """构建 failure_info 对象。success 任务返回 None。

    对齐 design §九。``failure_stage`` 取 ``tasks.failure_stage``,
    ``failure_reason`` 取 ``tasks.failure_reason``,
    ``last_error_detail`` 取 ``tasks.last_error_detail``。
    """
    if is_success:
        return None
    return {
        "failure_stage": task_row.get("failure_stage"),
        "failure_reason": task_row.get("failure_reason"),
        "last_error_detail": task_row.get("last_error_detail"),
    }


# ---------------------------------------------------------------------------
# llm_history 构建
# ---------------------------------------------------------------------------


def _common_msg_fields(
    msg_row: Dict[str, Any],
    *,
    include_call_meta: bool,
) -> Dict[str, Any]:
    """所有角色共用字段: round / seq / role / content / call_meta。"""
    role = msg_row.get("role")
    call_meta_value: Any = None
    if role == "assistant" and include_call_meta:
        call_meta_value = _decode_json_field(msg_row.get("call_meta"))
    return {
        "round": _to_int(msg_row.get("round")) or 0,
        "seq": _to_int(msg_row.get("seq")) or 0,
        "role": role,
        "content": msg_row.get("content"),
        "call_meta": call_meta_value,
    }


def _task_a_message(
    msg_row: Dict[str, Any],
    *,
    include_call_meta: bool,
) -> Dict[str, Any]:
    """task_a 会话的单条消息 → JSONL 元素(含 task_a 专用结果字段)。"""
    base = _common_msg_fields(msg_row, include_call_meta=include_call_meta)
    role = base["role"]
    if role == "assistant":
        base["is_oj_pass"] = _to_bool(msg_row.get("is_oj_pass"))
        base["is_pbt_pass"] = _to_bool(msg_row.get("is_pbt_pass"))
        base["action_taken"] = msg_row.get("action_taken")
    else:
        base["is_oj_pass"] = None
        base["is_pbt_pass"] = None
        base["action_taken"] = None
    return base


def _input_gen_message(
    msg_row: Dict[str, Any],
    *,
    include_call_meta: bool,
) -> Dict[str, Any]:
    """input_gen 会话的单条消息 → JSONL 元素(含 input_gen 专用结果字段)。"""
    base = _common_msg_fields(msg_row, include_call_meta=include_call_meta)
    role = base["role"]
    if role == "assistant":
        base["compile_status"] = msg_row.get("compile_status")
        base["compile_error"] = msg_row.get("compile_error")
        base["is_final_success"] = _to_bool(msg_row.get("is_final_success"))
    else:
        base["compile_status"] = None
        base["compile_error"] = None
        base["is_final_success"] = None
    return base


def _build_llm_history(
    conn: Any,
    task_id: int,
    *,
    include_call_meta: bool,
) -> Dict[str, Any]:
    """构建 ``llm_history`` 对象(含 task_a 数组和按 a_round 分组的 input_gen)。

    对齐 design §四。task_a 的 a_round 固定为 0(不分组), seq 全局递增;
    input_gen 按 a_round 分组,每组内 seq 从 0 独立递增。
    """
    # ── task_a: 单一连续列表 ────────────────────────────
    task_a_rows = MessageDAO.list_session(conn, task_id, "task_a", a_round=0)
    task_a_msgs = [
        _task_a_message(r, include_call_meta=include_call_meta)
        for r in task_a_rows
    ]

    # ── input_gen: 一次查全部,按 a_round 分组 ───────────
    # 用一条 SQL 拿所有 input_gen 消息(含 call_meta json 解码),再 Python 端 group
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM task_llm_messages "
            "WHERE task_id = %s AND session_type = 'input_gen' "
            "ORDER BY a_round ASC, seq ASC",
            (task_id,),
        )
        ig_raw_rows = list(cur.fetchall())

    ig_groups: Dict[int, List[Dict[str, Any]]] = {}
    for r in ig_raw_rows:
        # 手动做 call_meta json.loads(MessageDAO.list_session 才会自动 decode)
        if isinstance(r.get("call_meta"), (str, bytes, bytearray)):
            r["call_meta"] = json.loads(r["call_meta"])
        a_round = int(r.get("a_round") or 0)
        ig_groups.setdefault(a_round, []).append(r)

    input_gen_groups: List[Dict[str, Any]] = []
    for a_round in sorted(ig_groups.keys()):
        msgs = [
            _input_gen_message(r, include_call_meta=include_call_meta)
            for r in ig_groups[a_round]
        ]
        input_gen_groups.append({"a_round": a_round, "messages": msgs})

    return {"task_a": task_a_msgs, "input_gen": input_gen_groups}


# ---------------------------------------------------------------------------
# attempts 数组(oj / rac / pbt)
# ---------------------------------------------------------------------------


def _build_oj_attempts(
    rows: List[Dict[str, Any]],
    *,
    precision: int,
) -> List[Dict[str, Any]]:
    """oj_attempts 表行 → jsonl 数组元素。对齐 design §五。"""
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "a_round": _to_int(r.get("a_round")),
                "status": r.get("status"),
                "exit_code": _to_int(r.get("exit_code")),
                "elapsed_seconds": _to_float(
                    r.get("elapsed_seconds"), precision
                ),
                "detail_truncated": r.get("detail_truncated"),
                "detail_full_length": _to_int(r.get("detail_full_length")),
                "action_taken": r.get("action_taken"),
                "mutation_score": _to_float(r.get("mutation_score"), precision),
            }
        )
    return out


def _build_rac_attempts(
    rows: List[Dict[str, Any]],
    *,
    precision: int,
) -> List[Dict[str, Any]]:
    """rac_attempts 表行 → jsonl 数组元素。对齐 design §六。"""
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "a_round": _to_int(r.get("a_round")),
                "status": r.get("status"),
                "exit_code": _to_int(r.get("exit_code")),
                "elapsed_seconds": _to_float(
                    r.get("elapsed_seconds"), precision
                ),
                "detail_truncated": r.get("detail_truncated"),
                "detail_full_length": _to_int(r.get("detail_full_length")) or 0,
                "action_taken": r.get("action_taken"),
            }
        )
    return out


def _build_oj_candidates(
    oj_rows: List[Dict[str, Any]],
    *,
    precision: int,
) -> List[Dict[str, Any]]:
    """从 oj_attempts 过滤出通过的候选,只保留 a_round + mutation_score + mt_detail。

    用途: 快速查看 OJ 路径下每轮通过候选的 MT 评分,不用翻完整 attempts 数组。
    mt_detail 暂为 None,等 mt_processor 落库后回填。
    """
    out: List[Dict[str, Any]] = []
    for r in oj_rows:
        if r.get("status") != "pass":
            continue
        out.append(
            {
                "a_round": _to_int(r.get("a_round")),
                "mutation_score": _to_float(r.get("mutation_score"), precision),
                "mt_detail": _extract_mt_detail(r),
            }
        )
    return out


def _build_rp_candidates(
    pbt_rows: List[Dict[str, Any]],
    *,
    precision: int,
) -> List[Dict[str, Any]]:
    """从 pbt_attempts 过滤出 is_pass=1 的候选,只保留 a_round/ig_round + score + detail。"""
    out: List[Dict[str, Any]] = []
    for r in pbt_rows:
        if not _to_bool(r.get("is_pass")):
            continue
        out.append(
            {
                "a_round": _to_int(r.get("a_round")),
                "ig_round": _to_int(r.get("ig_round")),
                "mutation_score": _to_float(r.get("mutation_score"), precision),
                "mt_detail": _extract_mt_detail(r),
            }
        )
    return out


def _build_pbt_attempts(
    rows: List[Dict[str, Any]],
    *,
    precision: int,
    include_failure_samples: bool,
) -> List[Dict[str, Any]]:
    """pbt_attempts 表行 → jsonl 数组元素。对齐 design §七。

    - ``mutation_score`` 主流水线 DB 中始终为 NULL,由 MT 后处理回填,这里照搬
    - ``failure_samples`` 已在 PBTAttemptDAO 层 json.loads,无需再解
    - ``include_failure_samples=False`` 时强制写空数组(用户要求)
    - ``detail_truncated`` / ``detail_full_length`` 是 D.6(2026-05-03)引入的
      PBT TestDriver 编译/运行失败错误截断字段,**design §七 没列出**,
      经用户确认后导出(便于后续分析编译错误样本)
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        # blacklist_hits / failure_samples 在 DAO 层已 decode,但兜底再处理一次
        bl_hits = _decode_json_field(r.get("blacklist_hits")) or []
        fs_raw = _decode_json_field(r.get("failure_samples")) or []
        if include_failure_samples:
            failure_samples = fs_raw
        else:
            failure_samples = []
        out.append(
            {
                "a_round": _to_int(r.get("a_round")),
                "ig_round": _to_int(r.get("ig_round")),
                "full_java_with_jml": r.get("full_java_with_jml"),
                "has_blacklist_syntax": _to_bool(r.get("has_blacklist_syntax"))
                or False,
                "blacklist_hits": list(bl_hits) if bl_hits else [],
                "is_pass": _to_bool(r.get("is_pass")) or False,
                "total_runs": _to_int(r.get("total_runs")) or 0,
                "gen_error_count": _to_int(r.get("gen_error_count")) or 0,
                "precondition_reject_count": _to_int(
                    r.get("precondition_reject_count")
                )
                or 0,
                "post_fail_count": _to_int(r.get("post_fail_count")) or 0,
                "seed": _to_int(r.get("seed")) or 0,
                "elapsed_seconds": _to_float(
                    r.get("elapsed_seconds"), precision
                ),
                "failure_samples": failure_samples,
                "action_taken": r.get("action_taken"),
                "mutation_score": _to_float(r.get("mutation_score"), precision),
                "valid_input_ratio": _to_float(r.get("valid_input_ratio"), precision),
                # ↓ D.6 扩展(用户确认导出): TestDriver 编译/运行失败错误信息
                "detail_truncated": r.get("detail_truncated"),
                "detail_full_length": _to_int(r.get("detail_full_length"))
                or 0,
            }
        )
    return out


# ---------------------------------------------------------------------------
# global_summary
# ---------------------------------------------------------------------------


def _iter_call_meta(*message_lists: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    """从一组 jsonl 消息列表中抽取所有非空 call_meta dict。"""
    for msgs in message_lists:
        for m in msgs:
            cm = m.get("call_meta")
            if isinstance(cm, dict):
                yield cm


def _vendors_models_from_call_meta(
    history: Dict[str, Any],
) -> Tuple[List[str], List[str], int]:
    """遍历 llm_history 收集 vendors_used / models_used / total_circuit_breaker。

    - vendors_used: ``call_meta.vendor_name`` 去重(保留首次出现顺序)
    - models_used: ``"<vendor>/<model>"`` 去重(保留首次出现顺序)
    - total_circuit_breaker_count: ``call_meta.total_circuit_breaker_count`` 求和
    """
    task_a = history.get("task_a", []) if isinstance(history, dict) else []
    ig_groups = history.get("input_gen", []) if isinstance(history, dict) else []
    ig_msgs: List[Dict[str, Any]] = []
    for g in ig_groups:
        ig_msgs.extend(g.get("messages", []))

    vendors: List[str] = []
    models: List[str] = []
    cb_total = 0
    for cm in _iter_call_meta(task_a, ig_msgs):
        vendor = cm.get("vendor_name")
        model = cm.get("model_used")
        if vendor and vendor not in vendors:
            vendors.append(vendor)
        if vendor and model:
            combo = f"{vendor}/{model}"
            if combo not in models:
                models.append(combo)
        cb = cm.get("total_circuit_breaker_count")
        if isinstance(cb, (int, float)):
            cb_total += int(cb)
    return vendors, models, cb_total


def _total_elapsed_seconds(
    task_row: Dict[str, Any],
    precision: int,
) -> float:
    """从 started_at / finished_at 计算总耗时(秒)。任一缺失返回 0.0。"""
    started = task_row.get("started_at")
    finished = task_row.get("finished_at")
    if started is None or finished is None:
        return 0.0
    try:
        delta = finished - started
        return round(float(delta.total_seconds()), precision)
    except Exception:  # noqa: BLE001
        return 0.0


def _build_global_summary(
    task_row: Dict[str, Any],
    history: Dict[str, Any],
    *,
    precision: int,
) -> Dict[str, Any]:
    """构建 global_summary。对齐 design §八。

    Args:
        task_row: tasks 表的原始行(含 task_a_logic_retry 等计数器字段)。
        history: 已构建好的 llm_history dict(用于聚合 vendors/models)。
        precision: 浮点精度位数。

    扩展字段:
        ``total_pbt_compile_retry`` 是 D.8(2026-05-03)引入的 PBT TestDriver
        编译失败累计计数器,**design §八 没列出**,经用户确认后加入 summary。
    """
    vendors, models, cb_total = _vendors_models_from_call_meta(history)
    return {
        "total_elapsed_seconds": _total_elapsed_seconds(task_row, precision),
        "total_task_a_logic_retry": _to_int(
            task_row.get("task_a_logic_retry")
        )
        or 0,
        "total_task_a_syntax_retry": _to_int(
            task_row.get("task_a_syntax_retry_total")
        )
        or 0,
        "total_pbt_pass_count": _to_int(task_row.get("pbt_pass_count")) or 0,
        "total_input_gen_retry": _to_int(
            task_row.get("input_gen_retry_total")
        )
        or 0,
        "total_circuit_breaker_count": cb_total,
        "vendors_used": vendors,
        "models_used": models,
        "path": task_row.get("path"),
        # ↓ D.8 扩展(用户确认导出): PBT TestDriver 编译失败累计次数
        "total_pbt_compile_retry": _to_int(
            task_row.get("pbt_compile_retry_total")
        )
        or 0,
    }


# ---------------------------------------------------------------------------
# 主装配
# ---------------------------------------------------------------------------


def build_record(
    conn: Any,
    task_row: Dict[str, Any],
    *,
    options: ExportOptions,
) -> Dict[str, Any]:
    """从 DB 拼出单条 jsonl 记录(完整字段)。

    Args:
        conn: 数据库连接。
        task_row: tasks 表单行(必须含全部列)。
        options: ExportOptions 运行期选项。

    Returns:
        与 design §二 完全对齐的 dict,可直接 ``json.dumps``。
    """
    task_id = int(task_row["id"])
    is_success = task_row.get("status") == "success"
    precision = options.float_precision

    # ── 顶层 ───────────────────────────────────────
    record = _base_record(task_row, source_machine=options.source_machine)
    record["final_answer"] = _final_answer(task_row, is_success=is_success, conn=conn)

    # ── llm_history(可选关闭) ─────────────────────
    if options.include_llm_history:
        llm_history = _build_llm_history(
            conn,
            task_id,
            include_call_meta=options.include_call_meta,
        )
    else:
        llm_history = {"task_a": [], "input_gen": []}
    record["llm_history"] = llm_history

    # ── attempts ──────────────────────────────────
    oj_rows = OJAttemptDAO.list_by_task(conn, task_id)
    pbt_rows = PBTAttemptDAO.list_by_task(conn, task_id)
    record["oj_attempts"] = _build_oj_attempts(oj_rows, precision=precision)
    record["rac_attempts"] = _build_rac_attempts(
        RACAttemptDAO.list_by_task(conn, task_id),
        precision=precision,
    )
    record["pbt_attempts"] = _build_pbt_attempts(
        pbt_rows,
        precision=precision,
        include_failure_samples=options.include_failure_samples,
    )

    # ── 候选清单 (OJ pass / RP pass) ─────────────────
    record["oj_candidates"] = _build_oj_candidates(oj_rows, precision=precision)
    record["rp_candidates"] = _build_rp_candidates(pbt_rows, precision=precision)

    # ── global_summary / failure_info ──────────────
    record["global_summary"] = _build_global_summary(
        task_row, llm_history, precision=precision
    )
    record["failure_info"] = _failure_info(task_row, is_success=is_success)

    return record


# ---------------------------------------------------------------------------
# 输出目录解析(时间戳冲突处理)
# ---------------------------------------------------------------------------


def _generate_timestamp() -> str:
    """生成默认时间戳: ``YYYYMMDD_HHMMSS``。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass(frozen=True)
class OutputPaths:
    """已解析的输出路径集合。"""

    dir_path: Path
    file_timestamp: str  # 用于文件名内的时间戳,可能含 _2 后缀
    success_path: Path
    failed_path: Path


def resolve_output_paths(
    base_dir: Path,
    timestamp: str,
) -> OutputPaths:
    """根据时间戳生成目录,撞车自动加 ``_2`` / ``_3`` 后缀(目录+文件名同步)。

    例: 时间戳 ``20260503_143022``,目录已存在则尝试 ``20260503_143022_2``。
    内部 jsonl 文件名也使用相同后缀,保持目录与文件内时间戳一致。
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    candidate_ts = timestamp
    suffix = 1
    while True:
        candidate_dir = base_dir / candidate_ts
        if not candidate_dir.exists():
            candidate_dir.mkdir(parents=True, exist_ok=False)
            return OutputPaths(
                dir_path=candidate_dir,
                file_timestamp=candidate_ts,
                success_path=candidate_dir / f"success_{candidate_ts}.jsonl",
                failed_path=candidate_dir / f"failed_{candidate_ts}.jsonl",
            )
        suffix += 1
        candidate_ts = f"{timestamp}_{suffix}"


# ---------------------------------------------------------------------------
# 主导出流程
# ---------------------------------------------------------------------------


def _filter_tasks(
    rows: List[Dict[str, Any]],
    *,
    dataset_name: Optional[str],
) -> List[Dict[str, Any]]:
    """按 ``--dataset-name`` 过滤(None 不过滤)。"""
    if not dataset_name:
        return rows
    return [r for r in rows if r.get("source_dataset_name") == dataset_name]


def _split_records(
    records: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 ``final_answer is None`` 分流(等价于 status='failed')。

    与 design §15 一致: ``final_answer is not None`` → success, 否则 failed。
    """
    success: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for r in records:
        if r.get("final_answer") is not None:
            success.append(r)
        else:
            failed.append(r)
    return success, failed


def _reassign_ids(records: List[Dict[str, Any]]) -> None:
    """单机内 id 按 ``source_dataset_index`` 排序后从 1 起重新分配(就地修改)。

    多机合并由 merge_datasets.py 再做一次全局 ID 重分配,与 design §13 一致。
    """
    records.sort(key=lambda r: (r.get("source_dataset_index") or 0))
    for i, r in enumerate(records, 1):
        r["id"] = i


def _write_jsonl(
    path: Path,
    records: List[Dict[str, Any]],
) -> int:
    """逐行 ``json.dumps(ensure_ascii=False)`` 写入。返回写入条数。

    空列表也会创建空文件(便于下游脚本判断"已导出但 0 条")。
    """
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def _fetch_completed_tasks(
    conn: Any,
    *,
    only: str,
) -> List[Dict[str, Any]]:
    """按 ``only`` 拉 success / failed / 两者的任务行(in_progress/pending 跳过)。

    ``only`` ∈ ``{"success","failed","both"}``。
    """
    rows: List[Dict[str, Any]] = []
    if only in ("success", "both"):
        rows.extend(TaskDAO.list_by_status(conn, "success"))
    if only in ("failed", "both"):
        rows.extend(TaskDAO.list_by_status(conn, "failed"))
    return rows


def run_export(
    config: AppConfig,
    *,
    output_dir: Optional[Path] = None,
    timestamp: Optional[str] = None,
    options: Optional[ExportOptions] = None,
) -> Dict[str, Any]:
    """完整导出流程:DB → 装配 → 写文件。

    Args:
        config: ``AppConfig``。
        output_dir: 覆盖 ``config.paths.output_dir``。
        timestamp: 覆盖默认时间戳(主要用于测试)。
        options: 已构建好的 ExportOptions;未提供时按 config + 默认值构建。

    Returns:
        ``{"success_count": int, "failed_count": int, "skipped": int,
        "output_dir": str, "success_path": str, "failed_path": str,
        "timestamp": str}``。
    """
    # ── 解析 options ──────────────────────────────
    if options is None:
        options = ExportOptions(
            include_llm_history=config.export.include_llm_history,
            include_call_meta=config.export.include_call_meta,
            include_failure_samples=config.export.include_failure_samples,
            float_precision=config.export.float_precision,
            source_machine=config.task_loading.source_machine,
            only="both",
            dataset_name=None,
        )

    # ── 解析输出路径(撞车自动加 _2) ──────────────
    base_dir = output_dir if output_dir else Path(config.paths.output_dir)
    ts = timestamp or _generate_timestamp()
    out = resolve_output_paths(base_dir, ts)

    # ── 拉任务 + 装配记录 ─────────────────────────
    success_records: List[Dict[str, Any]] = []
    failed_records: List[Dict[str, Any]] = []
    raw_count = 0

    with get_connection(config.database) as conn:
        rows = _fetch_completed_tasks(conn, only=options.only)
        rows = _filter_tasks(rows, dataset_name=options.dataset_name)
        raw_count = len(rows)
        for task_row in rows:
            record = build_record(conn, task_row, options=options)
            if record["final_answer"] is not None:
                success_records.append(record)
            else:
                failed_records.append(record)

    # ── id 重排 + 落盘 ────────────────────────────
    _reassign_ids(success_records)
    _reassign_ids(failed_records)

    s_count = (
        _write_jsonl(out.success_path, success_records)
        if options.only in ("success", "both")
        else 0
    )
    f_count = (
        _write_jsonl(out.failed_path, failed_records)
        if options.only in ("failed", "both")
        else 0
    )

    logger.info(
        "导出完成: success=%d, failed=%d → %s",
        s_count,
        f_count,
        out.dir_path,
    )

    return {
        "success_count": s_count,
        "failed_count": f_count,
        "raw_task_count": raw_count,
        "output_dir": str(out.dir_path),
        "success_path": str(out.success_path),
        "failed_path": str(out.failed_path),
        "timestamp": out.file_timestamp,
    }


# ---------------------------------------------------------------------------
# main 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _parse_args(argv)
    config = load_config(args.config)

    options = ExportOptions.from_args(
        config.export,
        args,
        source_machine_default=config.task_loading.source_machine,
    )

    stats = run_export(
        config,
        output_dir=args.output_dir,
        timestamp=args.timestamp,
        options=options,
    )

    logger.info("=" * 60)
    logger.info("数据集导出汇总:")
    logger.info("  目录:              %s", stats["output_dir"])
    logger.info("  时间戳:            %s", stats["timestamp"])
    logger.info("  抓取的任务数:      %d", stats["raw_task_count"])
    logger.info("  success 输出:     %d 条 → %s", stats["success_count"], stats["success_path"])
    logger.info("  failed  输出:     %d 条 → %s", stats["failed_count"], stats["failed_path"])
    logger.info("  注: in_progress / pending 任务静默跳过")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
