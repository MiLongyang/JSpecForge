#!/usr/bin/env python
"""Read-only audit for MT scores in OJ/RP pass candidates.

The script reports current MySQL state without modifying any table. It focuses
on success tasks and separates task-level path counts from candidate-level MT
score counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.db.connection import connect_raw  # noqa: E402


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def bucket_score(value: Any) -> str:
    """Classify a mutation score into reporting buckets."""
    score = _as_float(value)
    if score is None:
        return "null"
    if score == -1:
        return "-1"
    if score == 0:
        return "0"
    if score == 1:
        return "1"
    if 0 < score < 1:
        return "(0,1)"
    return "other"


def summarize_zero_consistency(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count whether score=0 rows match killed=0, survived>0, total=survived."""
    zero_total = 0
    true_zero = 0
    for row in rows:
        if bucket_score(row.get("mutation_score")) != "0":
            continue
        zero_total += 1
        killed = _as_int(row.get("mt_killed"))
        survived = _as_int(row.get("mt_survived"))
        total = _as_int(row.get("mt_total"))
        if killed == 0 and survived is not None and survived > 0 and total == survived:
            true_zero += 1
    return {
        "zero_total": zero_total,
        "zero_true_k0_survived_positive": true_zero,
        "zero_suspicious": zero_total - true_zero,
    }


def summarize_anomaly_targets(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count rows that should enter the first rerun target set."""
    counts = Counter(bucket_score(row.get("mutation_score")) for row in rows)
    score_null = int(counts["null"])
    score_neg1 = int(counts["-1"])
    return {
        "rerun_target_total": score_null + score_neg1,
        "score_null": score_null,
        "score_neg1": score_neg1,
    }


def summarize_score_buckets(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(bucket_score(row.get("mutation_score")) for row in rows)
    return {key: int(counts[key]) for key in ["-1", "(0,1)", "0", "1", "null", "other"]}


def summarize_detail_presence(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out = {
        "null_with_mt_total": 0,
        "null_without_mt_total": 0,
        "neg1_with_mt_total": 0,
        "neg1_without_mt_total": 0,
    }
    for row in rows:
        bucket = bucket_score(row.get("mutation_score"))
        has_total = row.get("mt_total") is not None
        if bucket == "null":
            out["null_with_mt_total" if has_total else "null_without_mt_total"] += 1
        elif bucket == "-1":
            out["neg1_with_mt_total" if has_total else "neg1_without_mt_total"] += 1
    return out


def _query_all(conn: Any, sql: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def collect_audit(conn: Any) -> dict[str, Any]:
    task_status_path = _query_all(
        conn,
        "SELECT status, path, COUNT(*) AS cnt "
        "FROM tasks GROUP BY status, path ORDER BY status, path",
    )
    oj_rows = _query_all(
        conn,
        "SELECT o.id, o.task_id, o.a_round, o.mutation_score, "
        "o.mt_killed, o.mt_survived, o.mt_total "
        "FROM oj_attempts o JOIN tasks t ON t.id = o.task_id "
        "WHERE t.status = 'success' AND o.status = 'pass'",
    )
    rp_rows = _query_all(
        conn,
        "SELECT p.id, p.task_id, p.a_round, p.ig_round, p.mutation_score, "
        "p.mt_killed, p.mt_survived, p.mt_total "
        "FROM pbt_attempts p JOIN tasks t ON t.id = p.task_id "
        "WHERE t.status = 'success' AND p.is_pass = 1",
    )
    pool_presence = _query_all(
        conn,
        "SELECT "
        "SUM(has_oj=1 AND has_rp=1) AS both_pools, "
        "SUM(has_oj=1 AND has_rp=0) AS oj_only, "
        "SUM(has_oj=0 AND has_rp=1) AS rp_only, "
        "SUM(has_oj=0 AND has_rp=0) AS neither, "
        "COUNT(*) AS total "
        "FROM ("
        "  SELECT t.id, "
        "  EXISTS(SELECT 1 FROM oj_attempts o WHERE o.task_id=t.id AND o.status='pass') AS has_oj, "
        "  EXISTS(SELECT 1 FROM pbt_attempts p WHERE p.task_id=t.id AND p.is_pass=1) AS has_rp "
        "  FROM tasks t WHERE t.status='success'"
        ") x",
    )[0]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "task_status_path": _jsonable(task_status_path),
        "success_task_pool_presence": _jsonable(pool_presence),
        "oj": {
            "candidate_total": len(oj_rows),
            "score_buckets": summarize_score_buckets(oj_rows),
            "zero_consistency": summarize_zero_consistency(oj_rows),
            "rerun_targets": summarize_anomaly_targets(oj_rows),
            "detail_presence": summarize_detail_presence(oj_rows),
        },
        "rp": {
            "candidate_total": len(rp_rows),
            "score_buckets": summarize_score_buckets(rp_rows),
            "zero_consistency": summarize_zero_consistency(rp_rows),
            "rerun_targets": summarize_anomaly_targets(rp_rows),
            "detail_presence": summarize_detail_presence(rp_rows),
        },
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def render_markdown(audit: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# MT Score Audit Report")
    lines.append("")
    lines.append(f"- Generated at: `{audit['generated_at']}`")
    lines.append("- Scope: success tasks only; OJ/RP pass candidates only")
    lines.append("- Rerun target policy: `mutation_score IS NULL` or `mutation_score = -1`")
    lines.append("- Score zero policy: audit only, not included in default rerun target")
    lines.append("")
    lines.append("## Task Status / Path")
    lines.append("")
    lines.append("| status | path | count |")
    lines.append("|---|---|---:|")
    for row in audit["task_status_path"]:
        lines.append(f"| {row.get('status')} | {row.get('path')} | {row.get('cnt')} |")
    lines.append("")
    lines.append("## Success Task Candidate Pools")
    lines.append("")
    p = audit["success_task_pool_presence"]
    lines.append(f"- both OJ/RP pools: {p['both_pools']}")
    lines.append(f"- OJ only: {p['oj_only']}")
    lines.append(f"- RP only: {p['rp_only']}")
    lines.append(f"- neither: {p['neither']}")
    lines.append(f"- total success tasks: {p['total']}")
    for path in ("oj", "rp"):
        section = audit[path]
        lines.append("")
        lines.append(f"## {path.upper()} Pass Candidates")
        lines.append("")
        lines.append(f"- candidate_total: {section['candidate_total']}")
        lines.append("")
        lines.append("| score bucket | count |")
        lines.append("|---|---:|")
        for key, count in section["score_buckets"].items():
            lines.append(f"| {key} | {count} |")
        lines.append("")
        z = section["zero_consistency"]
        lines.append(
            f"- score=0 true zero: {z['zero_true_k0_survived_positive']} / {z['zero_total']}"
        )
        lines.append(f"- score=0 suspicious: {z['zero_suspicious']}")
        r = section["rerun_targets"]
        lines.append(
            f"- rerun targets: {r['rerun_target_total']} "
            f"(null={r['score_null']}, -1={r['score_neg1']})"
        )
        d = section["detail_presence"]
        lines.append(
            "- detail presence: "
            f"null_with_total={d['null_with_mt_total']}, "
            f"null_without_total={d['null_without_mt_total']}, "
            f"neg1_with_total={d['neg1_with_mt_total']}, "
            f"neg1_without_total={d['neg1_without_mt_total']}"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit MT score anomalies in MySQL")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for audit_report.md and audit_report.json",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    out_dir = (
        Path(args.output_dir)
        if args.output_dir
        else Path(config.paths.output_dir)
        / f"mt_rerun_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_raw(config.database, config.database.database)
    try:
        audit = collect_audit(conn)
    finally:
        conn.close()

    (out_dir / "audit_report.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "audit_report.md").write_text(render_markdown(audit), encoding="utf-8")
    print(f"MT audit written to: {out_dir}")


if __name__ == "__main__":
    main()
