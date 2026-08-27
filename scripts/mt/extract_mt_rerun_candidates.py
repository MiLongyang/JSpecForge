#!/usr/bin/env python
"""Extract candidate-level MT rerun inputs from MySQL.

This script is read-only. It exports OJ/RP pass candidates whose MT score is
NULL or -1, keeping enough source material for a standalone MT rerun.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.db.connection import connect_raw  # noqa: E402


DEFAULT_REASONS = ("score_null", "score_neg1")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def reason_for_score(value: Any) -> str | None:
    score = _as_float(value)
    if score is None:
        return "score_null"
    if score == -1:
        return "score_neg1"
    return None


def candidate_uid(path: str, attempt_id: int) -> str:
    return f"{path}:{int(attempt_id)}"


def build_candidate_record(row: Mapping[str, Any]) -> dict[str, Any]:
    path = str(row["path"])
    attempt_id = int(row["attempt_id"])
    old_score = _as_float(row.get("mutation_score"))
    reason = reason_for_score(old_score)
    if reason is None:
        raise ValueError(f"candidate {path}:{attempt_id} is not a rerun target")

    return {
        "candidate_uid": candidate_uid(path, attempt_id),
        "path": path,
        "task_id": int(row["task_id"]),
        "attempt_id": attempt_id,
        "a_round": int(row.get("a_round") or 0),
        "ig_round": None if row.get("ig_round") is None else int(row["ig_round"]),
        "source_dataset_name": row.get("source_dataset_name"),
        "source_dataset_index": row.get("source_dataset_index"),
        "source_task_id": row.get("source_task_id"),
        "full_java_with_jml": row.get("full_java_with_jml"),
        "method_code": row.get("method_code"),
        "input_generator_code": row.get("input_generator_code"),
        "old_mutation_score": old_score,
        "old_mt_killed": row.get("mt_killed"),
        "old_mt_survived": row.get("mt_survived"),
        "old_mt_total": row.get("mt_total"),
        "rerun_reason": reason,
    }


def _query_all(conn: Any, sql: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def collect_candidate_rows(conn: Any) -> list[dict[str, Any]]:
    oj_sql = (
        "SELECT 'oj' AS path, o.id AS attempt_id, o.task_id, o.a_round, "
        "NULL AS ig_round, t.source_dataset_name, t.source_dataset_index, "
        "t.source_task_id, o.full_java_with_jml, t.method_code, "
        "t.input_generator_code, o.mutation_score, o.mt_killed, "
        "o.mt_survived, o.mt_total "
        "FROM oj_attempts o JOIN tasks t ON t.id = o.task_id "
        "WHERE t.status = 'success' AND o.status = 'pass' "
        "AND (o.mutation_score IS NULL OR o.mutation_score = -1)"
    )
    rp_sql = (
        "SELECT 'rp' AS path, p.id AS attempt_id, p.task_id, p.a_round, "
        "p.ig_round, t.source_dataset_name, t.source_dataset_index, "
        "t.source_task_id, p.full_java_with_jml, t.method_code, "
        "COALESCE(t.input_generator_code, p.input_generator_code) AS input_generator_code, "
        "p.mutation_score, p.mt_killed, p.mt_survived, p.mt_total "
        "FROM pbt_attempts p JOIN tasks t ON t.id = p.task_id "
        "WHERE t.status = 'success' AND p.is_pass = 1 "
        "AND (p.mutation_score IS NULL OR p.mutation_score = -1)"
    )
    return _query_all(conn, oj_sql) + _query_all(conn, rp_sql)


def filter_records(
    records: list[dict[str, Any]],
    *,
    reasons: set[str],
    limit_per_kind: int | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    counts: Counter[tuple[str, str]] = Counter()
    for record in sorted(
        records,
        key=lambda r: (r["path"], r["rerun_reason"], r["task_id"], r["attempt_id"]),
    ):
        reason = record["rerun_reason"]
        if reason not in reasons:
            continue
        key = (record["path"], reason)
        if limit_per_kind is not None and counts[key] >= limit_per_kind:
            continue
        selected.append(record)
        counts[key] += 1
    return selected


def validate_record(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not record.get("full_java_with_jml"):
        errors.append("missing full_java_with_jml")
    if record.get("path") == "rp":
        if not record.get("method_code"):
            errors.append("missing method_code")
        if not record.get("input_generator_code"):
            errors.append("missing input_generator_code")
    return errors


def write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def render_report(records: list[Mapping[str, Any]], validation_errors: dict[str, list[str]]) -> str:
    counts = Counter((r["path"], r["rerun_reason"]) for r in records)
    lines = [
        "# MT 重跑候选提取报告",
        "",
        f"- 生成时间: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- 候选总数: {len(records)}",
        f"- 字段校验失败候选数: {len(validation_errors)}",
        "",
        "## 候选分布",
        "",
        "| path | reason | count |",
        "|---|---|---:|",
    ]
    for key in sorted(counts):
        lines.append(f"| {key[0]} | {key[1]} | {counts[key]} |")
    if validation_errors:
        lines.extend(["", "## 字段校验错误", ""])
        for uid, errors in validation_errors.items():
            lines.append(f"- `{uid}`: {', '.join(errors)}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取 MT 重跑候选 JSONL")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", required=True, help="候选 JSONL 输出路径")
    parser.add_argument(
        "--report",
        default=None,
        help="中文报告路径，默认与 output 同目录同名 .md",
    )
    parser.add_argument(
        "--reasons",
        nargs="+",
        default=list(DEFAULT_REASONS),
        choices=list(DEFAULT_REASONS),
    )
    parser.add_argument("--limit-per-kind", type=int, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    conn = connect_raw(config.database, config.database.database)
    try:
        rows = collect_candidate_rows(conn)
    finally:
        conn.close()

    records = [build_candidate_record(row) for row in rows]
    selected = filter_records(
        records,
        reasons=set(args.reasons),
        limit_per_kind=args.limit_per_kind,
    )
    validation_errors = {
        r["candidate_uid"]: errors
        for r in selected
        if (errors := validate_record(r))
    }
    output = Path(args.output)
    report = Path(args.report) if args.report else output.with_suffix(".md")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_report(selected, validation_errors), encoding="utf-8")
    if validation_errors:
        raise SystemExit(
            f"候选字段不完整，已拒绝输出。请查看报告: {report}"
        )

    write_jsonl(output, selected)
    print(f"候选 JSONL 已写入: {output}")
    print(f"候选报告已写入: {report}")


if __name__ == "__main__":
    main()
