#!/usr/bin/env python
"""Dry-run, apply, or restore MT rerun results in MySQL attempt tables."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.db.connection import connect_raw  # noqa: E402


WRITE_FIELDS = ("mutation_score", "mt_killed", "mt_survived", "mt_total", "mt_feedback_text")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def attempt_table(path: str) -> str:
    if path == "oj":
        return "oj_attempts"
    if path == "rp":
        return "pbt_attempts"
    raise ValueError(f"unknown path={path!r}")


def normalize_write_values(result: Mapping[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status not in ("ok", "failed"):
        raise ValueError(f"invalid status={status!r}")
    score = float(result.get("mutation_score")) if result.get("mutation_score") is not None else -1.0
    if status == "failed":
        score = -1.0
        feedback_text = (
            "[MT_RERUN_FAILED] "
            f"{result.get('error_type') or 'unknown'}: {result.get('error_message') or ''}"
        )
        return {
            "mutation_score": score,
            "mt_killed": None,
            "mt_survived": None,
            "mt_total": None,
            "mt_feedback_text": feedback_text[:65535],
        }
    return {
        "mutation_score": score,
        "mt_killed": result.get("mt_killed"),
        "mt_survived": result.get("mt_survived"),
        "mt_total": result.get("mt_total"),
        "mt_feedback_text": result.get("mt_feedback_text"),
    }


def fetch_attempt(conn: Any, path: str, attempt_id: int) -> dict[str, Any] | None:
    table = attempt_table(path)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, task_id, a_round, "
            f"{'NULL AS ig_round,' if path == 'oj' else 'ig_round,'} "
            "mutation_score, mt_killed, mt_survived, mt_total, mt_feedback_text "
            f"FROM {table} WHERE id = %s",
            (attempt_id,),
        )
        return cur.fetchone()


def build_update_plan(conn: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    path = str(result.get("path"))
    attempt_id = int(result.get("attempt_id"))
    old = fetch_attempt(conn, path, attempt_id)
    if old is None:
        raise ValueError(f"attempt not found: {path}:{attempt_id}")
    new_values = normalize_write_values(result)
    return {
        "candidate_uid": result.get("candidate_uid"),
        "path": path,
        "attempt_id": attempt_id,
        "task_id": int(result.get("task_id")),
        "status": result.get("status"),
        "old": {field: old.get(field) for field in WRITE_FIELDS},
        "new": new_values,
    }


def execute_update(conn: Any, plan: Mapping[str, Any]) -> None:
    table = attempt_table(str(plan["path"]))
    new = plan["new"]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET "
            "mutation_score = %s, mt_killed = %s, mt_survived = %s, "
            "mt_total = %s, mt_feedback_text = %s "
            "WHERE id = %s",
            (
                new["mutation_score"],
                new["mt_killed"],
                new["mt_survived"],
                new["mt_total"],
                new["mt_feedback_text"],
                int(plan["attempt_id"]),
            ),
        )
        if cur.rowcount not in (0, 1):
            raise RuntimeError(f"unexpected update rowcount={cur.rowcount} for {plan['candidate_uid']}")


def execute_restore(conn: Any, backup_row: Mapping[str, Any]) -> None:
    table = attempt_table(str(backup_row["path"]))
    old = backup_row["old"]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET "
            "mutation_score = %s, mt_killed = %s, mt_survived = %s, "
            "mt_total = %s, mt_feedback_text = %s "
            "WHERE id = %s",
            (
                old.get("mutation_score"),
                old.get("mt_killed"),
                old.get("mt_survived"),
                old.get("mt_total"),
                old.get("mt_feedback_text"),
                int(backup_row["attempt_id"]),
            ),
        )
        if cur.rowcount not in (0, 1):
            raise RuntimeError(f"unexpected restore rowcount={cur.rowcount} for {backup_row['candidate_uid']}")


def render_apply_report(plans: list[Mapping[str, Any]], *, applied: bool, restored: bool = False) -> str:
    counts = Counter((p["path"], p.get("status", "restore")) for p in plans)
    action = "restore" if restored else "apply" if applied else "dry-run"
    lines = [
        "# MT 重跑结果写回报告",
        "",
        f"- 生成时间: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- 动作: `{action}`",
        f"- 计划记录数: {len(plans)}",
        "",
        "## 分布",
        "",
        "| path | status | count |",
        "|---|---|---:|",
    ]
    for key in sorted(counts):
        lines.append(f"| {key[0]} | {key[1]} | {counts[key]} |")
    lines.extend(["", "## 更新明细", ""])
    for plan in plans:
        lines.append(
            f"- `{plan['candidate_uid']}` task={plan.get('task_id')} "
            f"attempt={plan['attempt_id']} old_score={plan['old'].get('mutation_score')} "
            f"new_score={plan['new'].get('mutation_score') if 'new' in plan else plan['old'].get('mutation_score')}"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="写回 MT 重跑结果到 attempts 表")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--input", required=True, help="合并后的结果 JSONL，或 restore 使用的 backup JSONL")
    parser.add_argument("--report", required=True, help="中文报告路径")
    parser.add_argument("--backup-output", default=None, help="apply 前输出旧值备份 JSONL")
    parser.add_argument("--apply", action="store_true", help="实际写回；默认只 dry-run")
    parser.add_argument("--restore", action="store_true", help="按 backup JSONL 恢复旧值")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.apply and args.restore:
        raise SystemExit("--apply 和 --restore 不能同时使用")
    config = load_config(args.config)
    conn = connect_raw(config.database, config.database.database)
    report_path = Path(args.report)
    try:
        rows = read_jsonl(Path(args.input))
        if args.restore:
            for row in rows:
                execute_restore(conn, row)
            conn.commit()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(render_apply_report(rows, applied=True, restored=True), encoding="utf-8")
            print(f"恢复完成: {len(rows)} 条")
            print(f"报告已写入: {report_path}")
            return

        plans = [build_update_plan(conn, row) for row in rows]
        if args.backup_output:
            write_jsonl(Path(args.backup_output), plans)
        if args.apply:
            for plan in plans:
                execute_update(conn, plan)
            conn.commit()
        else:
            conn.rollback()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_apply_report(plans, applied=args.apply), encoding="utf-8")
        print(f"{'写回' if args.apply else 'dry-run'} 完成: {len(plans)} 条")
        print(f"报告已写入: {report_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
