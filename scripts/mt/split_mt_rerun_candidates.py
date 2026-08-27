#!/usr/bin/env python
"""Split MT rerun candidate JSONL into deterministic shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("candidate_uid"):
                raise ValueError(f"{path}:{line_no} 缺少 candidate_uid")
            rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def split_rows(rows: list[dict[str, Any]], shard_count: int) -> list[list[dict[str, Any]]]:
    if shard_count < 1:
        raise ValueError("shard_count must be >= 1")
    seen: set[str] = set()
    shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    for idx, row in enumerate(sorted(rows, key=lambda r: str(r["candidate_uid"]))):
        uid = str(row["candidate_uid"])
        if uid in seen:
            raise ValueError(f"重复 candidate_uid: {uid}")
        seen.add(uid)
        shards[idx % shard_count].append(row)
    return shards


def count_distribution(rows: Iterable[Mapping[str, Any]]) -> Counter[tuple[str, str]]:
    counts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        counts[(str(row.get("path")), str(row.get("rerun_reason")))] += 1
    return counts


def render_report(
    *,
    input_path: Path,
    output_dir: Path,
    shard_paths: list[Path],
    all_rows: list[dict[str, Any]],
    shards: list[list[dict[str, Any]]],
) -> str:
    total_counts = count_distribution(all_rows)
    lines = [
        "# MT 重跑候选分片报告",
        "",
        f"- 生成时间: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- 输入文件: `{input_path}`",
        f"- 输出目录: `{output_dir}`",
        f"- 候选总数: {len(all_rows)}",
        f"- shard 数量: {len(shards)}",
        "",
        "## 总体分布",
        "",
        "| path | reason | count |",
        "|---|---|---:|",
    ]
    for key in sorted(total_counts):
        lines.append(f"| {key[0]} | {key[1]} | {total_counts[key]} |")

    lines.extend(
        [
            "",
            "## 分片明细",
            "",
            "| shard | count | sha256 | oj_null | oj_neg1 | rp_null | rp_neg1 |",
            "|---|---:|---|---:|---:|---:|---:|",
        ]
    )
    for path, rows in zip(shard_paths, shards, strict=True):
        counts = count_distribution(rows)
        lines.append(
            f"| `{path.name}` | {len(rows)} | `{sha256_file(path)}` | "
            f"{counts[('oj', 'score_null')]} | "
            f"{counts[('oj', 'score_neg1')]} | "
            f"{counts[('rp', 'score_null')]} | "
            f"{counts[('rp', 'score_neg1')]} |"
        )
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 MT 重跑候选 JSONL 切分为多个 shard")
    parser.add_argument("--input", required=True, help="候选 JSONL")
    parser.add_argument("--output-dir", required=True, help="分片输出目录")
    parser.add_argument("--shards", type=int, default=5, help="分片数量，默认 5")
    parser.add_argument("--prefix", default="candidates_shard", help="分片文件名前缀")
    parser.add_argument("--report", default=None, help="报告路径，默认 output-dir/shard_report.md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    report_path = Path(args.report) if args.report else output_dir / "shard_report.md"

    rows = read_jsonl(input_path)
    shards = split_rows(rows, args.shards)
    shard_paths: list[Path] = []
    for idx, shard_rows in enumerate(shards, 1):
        path = output_dir / f"{args.prefix}_{idx:02d}.jsonl"
        write_jsonl(path, shard_rows)
        shard_paths.append(path)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render_report(
            input_path=input_path,
            output_dir=output_dir,
            shard_paths=shard_paths,
            all_rows=rows,
            shards=shards,
        ),
        encoding="utf-8",
    )
    print(f"已生成 {len(shard_paths)} 个分片到: {output_dir}")
    print(f"分片报告已写入: {report_path}")


if __name__ == "__main__":
    main()
