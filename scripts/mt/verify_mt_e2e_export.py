#!/usr/bin/env python
"""Verify exported dataset reflects applied MT rerun results."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.db.connection import connect_raw  # noqa: E402
from jspecforge.db.oj_attempt_dao import OJAttemptDAO  # noqa: E402
from jspecforge.db.pbt_attempt_dao import PBTAttemptDAO  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def score_equal(left: Any, right: Any, *, precision: int = 4) -> bool:
    if left is None or right is None:
        return left is right
    return round(float(left), precision) == round(float(right), precision)


def fetch_task_keys(conn: Any, task_ids: set[int]) -> dict[int, tuple[str, int]]:
    if not task_ids:
        return {}
    placeholders = ",".join(["%s"] * len(task_ids))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_dataset_name, source_dataset_index "
            f"FROM tasks WHERE id IN ({placeholders})",
            tuple(sorted(task_ids)),
        )
        return {
            int(row["id"]): (row["source_dataset_name"], int(row["source_dataset_index"]))
            for row in cur.fetchall()
        }


def find_candidate(record: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if result["path"] == "oj":
        for candidate in record.get("oj_candidates") or []:
            if candidate.get("a_round") == result.get("a_round"):
                return candidate
    else:
        for candidate in record.get("rp_candidates") or []:
            if (
                candidate.get("a_round") == result.get("a_round")
                and candidate.get("ig_round") == result.get("ig_round")
            ):
                return candidate
    return None


def verify_export(
    *,
    conn: Any,
    results: list[dict[str, Any]],
    exported_records: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    task_keys = fetch_task_keys(conn, {int(r["task_id"]) for r in results})
    records_by_key = {
        (r.get("source_dataset_name"), int(r.get("source_dataset_index"))): r
        for r in exported_records
    }

    for result in results:
        uid = result["candidate_uid"]
        task_id = int(result["task_id"])
        key = task_keys.get(task_id)
        if key is None:
            errors.append(f"{uid}: DB 中找不到 task_id={task_id}")
            continue
        record = records_by_key.get(key)
        if record is None:
            errors.append(f"{uid}: 导出 JSONL 中找不到 source key={key}")
            continue

        candidate = find_candidate(record, result)
        if candidate is None:
            errors.append(f"{uid}: 导出候选池中找不到 a_round/ig_round 对应候选")
            continue
        if not score_equal(candidate.get("mutation_score"), result.get("mutation_score")):
            errors.append(
                f"{uid}: 候选池 score 不一致 export={candidate.get('mutation_score')} "
                f"result={result.get('mutation_score')}"
            )
        detail = candidate.get("mt_detail") or {}
        for field, result_field in (
            ("killed", "mt_killed"),
            ("survived", "mt_survived"),
            ("total", "mt_total"),
        ):
            if detail.get(field) != result.get(result_field):
                errors.append(
                    f"{uid}: mt_detail.{field} 不一致 export={detail.get(field)} "
                    f"result={result.get(result_field)}"
                )

        final_answer = record.get("final_answer") or {}
        if result["path"] == "oj":
            best = OJAttemptDAO.get_best_passing(conn, task_id)
            final_part = final_answer.get("oj")
        else:
            best = PBTAttemptDAO.get_best_passing(conn, task_id)
            final_part = final_answer.get("rp")
        if best is None:
            errors.append(f"{uid}: DB get_best_passing 返回空")
            continue
        if final_part is None:
            errors.append(f"{uid}: final_answer.{result['path']} 为空")
            continue
        if not score_equal(final_part.get("mutation_score"), best.get("mutation_score")):
            errors.append(
                f"{uid}: final_answer.{result['path']} 与 DAO best score 不一致 "
                f"final={final_part.get('mutation_score')} best={best.get('mutation_score')}"
            )
        best_attempt_id = int(best["id"])
        if best_attempt_id == int(result["attempt_id"]):
            notes.append(f"{uid}: 写回候选成为 final_answer.{result['path']} 的 best")
        else:
            notes.append(
                f"{uid}: 写回候选已在候选池刷新；当前 best 是 {result['path']}:{best_attempt_id}"
            )
    return errors, notes


def render_report(errors: list[str], notes: list[str], *, result_count: int, export_count: int) -> str:
    lines = [
        "# MT 小批量闭环导出验证报告",
        "",
        f"- 生成时间: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- 验证结果数: {result_count}",
        f"- 导出 success 记录数: {export_count}",
        f"- 错误数: {len(errors)}",
        "",
        "## 结论明细",
        "",
    ]
    for note in notes:
        lines.append(f"- {note}")
    if errors:
        lines.extend(["", "## 错误", ""])
        for error in errors:
            lines.append(f"- {error}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证导出 JSONL 是否反映 MT 写回结果")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--results", required=True, help="合并后的 MT 结果 JSONL")
    parser.add_argument("--success-jsonl", required=True, help="导出的 success JSONL")
    parser.add_argument("--report", required=True, help="验证报告路径")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    results = read_jsonl(Path(args.results))
    exported = read_jsonl(Path(args.success_jsonl))
    conn = connect_raw(config.database, config.database.database)
    try:
        errors, notes = verify_export(conn=conn, results=results, exported_records=exported)
    finally:
        conn.close()
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(errors, notes, result_count=len(results), export_count=len(exported)),
        encoding="utf-8",
    )
    print(f"验证报告已写入: {report_path}")
    if errors:
        raise SystemExit("导出验证失败")


if __name__ == "__main__":
    main()
