#!/usr/bin/env python
"""Merge and validate MT rerun result JSONL files."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


REQUIRED_IDENTITY_FIELDS = ("candidate_uid", "path", "task_id", "attempt_id", "status")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_source_file"] = str(path)
            row["_source_line"] = line_no
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            f.write(json.dumps(clean, ensure_ascii=False) + "\n")


def validate_result_row(row: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_IDENTITY_FIELDS:
        if row.get(field) in (None, ""):
            errors.append(f"missing {field}")
    if row.get("path") not in ("oj", "rp"):
        errors.append(f"invalid path={row.get('path')!r}")
    if row.get("status") not in ("ok", "failed"):
        errors.append(f"invalid status={row.get('status')!r}")
    if row.get("status") == "ok":
        score = row.get("mutation_score")
        if not isinstance(score, (int, float)):
            errors.append("ok result missing numeric mutation_score")
        elif score < -1 or score > 1:
            # score=-1 是设计内的合法值：“0 个可算变异体”/“分母为 0”
            errors.append(f"ok result mutation_score out of range: {score}")
        for field in ("mt_killed", "mt_survived", "mt_total"):
            if not isinstance(row.get(field), int):
                errors.append(f"ok result missing integer {field}")
    else:
        if row.get("error_type") in (None, ""):
            errors.append("failed result missing error_type")
        if row.get("error_message") in (None, ""):
            errors.append("failed result missing error_message")
    return errors


def merge_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    errors: dict[str, list[str]] = {}
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = str(row.get("candidate_uid") or f"<missing>@{row.get('_source_file')}:{row.get('_source_line')}")
        row_errors = validate_result_row(row)
        if uid in seen:
            first = seen[uid]
            row_errors.append(
                "duplicate candidate_uid; first="
                f"{first.get('_source_file')}:{first.get('_source_line')}, "
                f"current={row.get('_source_file')}:{row.get('_source_line')}"
            )
        if row_errors:
            errors.setdefault(uid, []).extend(row_errors)
        else:
            seen[uid] = row
    merged = [seen[uid] for uid in sorted(seen)]
    return merged, errors


def summarize(rows: Iterable[Mapping[str, Any]]) -> Counter[tuple[str, str, str]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        counts[(str(row.get("path")), str(row.get("status")), str(row.get("error_type") or ""))] += 1
    return counts


def render_report(
    *,
    input_paths: list[Path],
    merged_rows: list[dict[str, Any]],
    errors: dict[str, list[str]],
) -> str:
    counts = summarize(merged_rows)
    lines = [
        "# MT 重跑结果合并报告",
        "",
        f"- 生成时间: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- 输入文件数: {len(input_paths)}",
        f"- 合并成功记录数: {len(merged_rows)}",
        f"- 错误 candidate_uid 数: {len(errors)}",
        "",
        "## 输入文件",
        "",
    ]
    for path in input_paths:
        lines.append(f"- `{path}`")
    lines.extend(
        [
            "",
            "## 结果分布",
            "",
            "| path | status | error_type | count |",
            "|---|---|---|---:|",
        ]
    )
    for key in sorted(counts):
        lines.append(f"| {key[0]} | {key[1]} | {key[2] or '-'} | {counts[key]} |")
    if errors:
        lines.extend(["", "## 错误", ""])
        for uid, uid_errors in sorted(errors.items()):
            lines.append(f"- `{uid}`: {'; '.join(uid_errors)}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="合并 MT 重跑结果 JSONL")
    parser.add_argument("--inputs", nargs="+", required=True, help="一个或多个结果 JSONL")
    parser.add_argument("--output", required=True, help="合并后的 JSONL")
    parser.add_argument("--report", default=None, help="报告路径，默认与 output 同名 .md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_paths = [Path(p) for p in args.inputs]
    rows: list[dict[str, Any]] = []
    for path in input_paths:
        rows.extend(read_jsonl(path))
    merged, errors = merge_rows(rows)

    output_path = Path(args.output)
    report_path = Path(args.report) if args.report else output_path.with_suffix(".md")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(input_paths=input_paths, merged_rows=merged, errors=errors),
        encoding="utf-8",
    )
    if errors:
        raise SystemExit(f"合并结果存在错误，已拒绝输出。请查看报告: {report_path}")
    write_jsonl(output_path, merged)
    print(f"合并结果已写入: {output_path}")
    print(f"合并报告已写入: {report_path}")


if __name__ == "__main__":
    main()
