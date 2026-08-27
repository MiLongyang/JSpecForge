"""Prepare the deterministic JSpecForge manual-audit sample package."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUCCESS_INPUT = (
    ROOT / "data" / "submission" / "jspecforge_submission_success.jsonl"
)
DEFAULT_FAILED_INPUT = (
    ROOT / "data" / "submission" / "jspecforge_submission_failed.jsonl"
)
DEFAULT_OUTPUT_DIR = (
    ROOT
    / "docs"
    / "JSpecForge数据集论文材料包"
    / "人工抽查"
    / "01"
)
DEFAULT_SEED = "20260719"
DEFAULT_DATABASE_NAME = "jspecforge_manual_audit_01"
DEFAULT_REVIEWER_CODE = "01"

SUCCESS_SAMPLE_NAME = "jspecforge_manual_audit_success_180.jsonl"
FAILED_SAMPLE_NAME = "jspecforge_manual_audit_failed_100.jsonl"
RISK_BUCKETS = (
    ("coverage_below_1", lambda item: item["branch_coverage"] < 1),
    ("answer_count_10_plus", lambda item: item["answer_count"] >= 10),
    ("minimum_mt_score_eq_0", lambda item: item["minimum_mt_score"] == 0),
    ("mt_score_range_ge_0.5", lambda item: item["mt_score_range"] >= 0.5),
    ("single_answer_task", lambda item: item["answer_count"] == 1),
)
RATING_VALUES = ("PASS", "MINOR", "MAJOR", "INVALID", "UNJUDGEABLE")


class ManualAuditPackageError(RuntimeError):
    """Raised when deterministic package preparation cannot complete."""


def load_jsonl_with_lines(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            raw_line = line.rstrip("\r\n")
            if not raw_line.strip():
                continue
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                raise ManualAuditPackageError(
                    f"{path}:{line_number}: expected JSON object"
                )
            items.append(
                {
                    "row": row,
                    "raw_line": raw_line,
                    "line_number": line_number,
                    "record_sha256": hashlib.sha256(
                        raw_line.encode("utf-8")
                    ).hexdigest(),
                    "original_file": str(path),
                }
            )
    return items


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def deterministic_rank(seed: str, namespace: str, dataset_id: str) -> str:
    value = f"{seed}|{namespace}|{dataset_id}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def allocate_with_minimum(
    sizes: Mapping[str, int],
    *,
    total: int,
    minimum: int,
) -> dict[str, int]:
    positive = {key: int(size) for key, size in sizes.items() if size > 0}
    if total < 0 or total > sum(positive.values()):
        raise ManualAuditPackageError(
            f"invalid allocation total={total}, population={sum(positive.values())}"
        )
    base = {key: min(minimum, size) for key, size in positive.items()}
    if sum(base.values()) > total:
        base = {key: 0 for key in positive}
    remaining = total - sum(base.values())
    capacity = {key: positive[key] - base[key] for key in positive}
    while remaining > 0:
        capacity_total = sum(capacity.values())
        if capacity_total <= 0:
            raise ManualAuditPackageError("allocation capacity exhausted")
        ideals = {
            key: remaining * capacity[key] / capacity_total
            for key in positive
        }
        floors = {
            key: min(capacity[key], int(ideals[key]))
            for key in positive
        }
        floor_total = sum(floors.values())
        if floor_total:
            for key, value in floors.items():
                base[key] += value
                capacity[key] -= value
            remaining -= floor_total
            continue
        ranked = sorted(
            positive,
            key=lambda key: (
                -(ideals[key] - int(ideals[key])),
                -capacity[key],
                key,
            ),
        )
        for key in ranked:
            if remaining == 0:
                break
            if capacity[key] <= 0:
                continue
            base[key] += 1
            capacity[key] -= 1
            remaining -= 1
    return {key: base.get(key, 0) for key in sizes}


def _answer_entries(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    answer_order = 0
    for mode in ("oj", "rp"):
        answer = row["final_answer"][mode]
        if answer is None:
            continue
        answer_order += 1
        entries.append(
            {
                "answer_order": answer_order,
                "answer_key": f"final_{mode}",
                "source_mode": mode.upper(),
                "answer_role": "FINAL",
                "candidate_index": None,
                "answer": answer,
            }
        )
    for mode in ("oj", "rp"):
        for index, answer in enumerate(row[f"{mode}_candidates"], start=1):
            answer_order += 1
            entries.append(
                {
                    "answer_order": answer_order,
                    "answer_key": f"candidate_{mode}_{index:02d}",
                    "source_mode": mode.upper(),
                    "answer_role": "CANDIDATE",
                    "candidate_index": index,
                    "answer": answer,
                }
            )
    return entries


def _score_band(score: float) -> str:
    if score == 0:
        return "score_eq_0"
    if score < 0.25:
        return "score_0_0.25"
    if score < 0.5:
        return "score_0.25_0.5"
    if score < 0.75:
        return "score_0.5_0.75"
    if score < 1:
        return "score_0.75_1"
    return "score_eq_1"


def _score_band_counts(scores: Sequence[float]) -> dict[str, int]:
    counter = Counter(_score_band(score) for score in scores)
    return {
        "score_eq_0": counter["score_eq_0"],
        "score_0_0.25": counter["score_0_0.25"],
        "score_0.25_0.5": counter["score_0.25_0.5"],
        "score_0.5_0.75": counter["score_0.5_0.75"],
        "score_0.75_1": counter["score_0.75_1"],
        "score_eq_1": counter["score_eq_1"],
    }


def _simple_numeric_summary(values: Sequence[float | int]) -> dict[str, Any]:
    numeric = [float(value) for value in values]
    if not numeric:
        return {
            "count": 0,
            "min": None,
            "median": None,
            "mean": None,
            "max": None,
        }
    return {
        "count": len(numeric),
        "min": round(min(numeric), 6),
        "median": round(float(median(numeric)), 6),
        "mean": round(float(mean(numeric)), 6),
        "max": round(max(numeric), 6),
    }


def _success_feature(item: Mapping[str, Any]) -> dict[str, Any]:
    row = item["row"]
    answers = _answer_entries(row)
    scores = [float(entry["answer"]["mutation_score"]) for entry in answers]
    has_oj = row["final_answer"]["oj"] is not None
    has_rp = row["final_answer"]["rp"] is not None
    final_mode = (
        "both"
        if has_oj and has_rp
        else "oj_only"
        if has_oj
        else "rp_only"
    )
    result = dict(item)
    result.update(
        {
            "dataset_id": row["dataset_id"],
            "split": "success",
            "source_dataset_name": row["source_dataset_name"],
            "source_task_id": row["source_task_id"],
            "source_dataset_index": row["source_dataset_index"],
            "branch_coverage": float(row["branch_coverage"]),
            "final_mode": final_mode,
            "oj_candidate_count": len(row["oj_candidates"]),
            "rp_candidate_count": len(row["rp_candidates"]),
            "candidate_count": len(row["oj_candidates"])
            + len(row["rp_candidates"]),
            "answer_count": len(answers),
            "minimum_mt_score": min(scores),
            "mean_mt_score": float(mean(scores)),
            "maximum_mt_score": max(scores),
            "mt_score_range": max(scores) - min(scores),
            "minimum_mt_score_band": _score_band(min(scores)),
            "answer_entries": answers,
            "termination_reason": None,
        }
    )
    return result


def _failed_feature(item: Mapping[str, Any]) -> dict[str, Any]:
    row = item["row"]
    result = dict(item)
    result.update(
        {
            "dataset_id": row["dataset_id"],
            "split": "failed",
            "source_dataset_name": row["source_dataset_name"],
            "source_task_id": row["source_task_id"],
            "source_dataset_index": row["source_dataset_index"],
            "branch_coverage": float(row["branch_coverage"]),
            "final_mode": None,
            "oj_candidate_count": 0,
            "rp_candidate_count": 0,
            "candidate_count": 0,
            "answer_count": 0,
            "minimum_mt_score": None,
            "mean_mt_score": None,
            "maximum_mt_score": None,
            "mt_score_range": None,
            "minimum_mt_score_band": None,
            "answer_entries": [],
            "termination_reason": row["termination_reason"],
        }
    )
    return result


def _select_ranked(
    items: Sequence[Mapping[str, Any]],
    *,
    count: int,
    seed: str,
    namespace: str,
) -> list[dict[str, Any]]:
    ranked = sorted(
        items,
        key=lambda item: deterministic_rank(
            seed, namespace, str(item["dataset_id"])
        ),
    )
    if len(ranked) < count:
        raise ManualAuditPackageError(
            f"{namespace}: need {count}, only {len(ranked)} available"
        )
    selected: list[dict[str, Any]] = []
    for item in ranked[:count]:
        copied = dict(item)
        copied["random_rank"] = deterministic_rank(
            seed, namespace, str(item["dataset_id"])
        )
        selected.append(copied)
    return selected


def _select_success(
    success_items: Sequence[Mapping[str, Any]],
    *,
    seed: str,
) -> list[dict[str, Any]]:
    features = [_success_feature(item) for item in success_items]
    primary_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in features:
        primary = f"{item['source_dataset_name']}|{item['final_mode']}"
        primary_groups[primary].append(item)
    primary_sizes = {
        key: len(values) for key, values in primary_groups.items()
    }
    primary_quotas = allocate_with_minimum(
        primary_sizes, total=150, minimum=1
    )

    representative: list[dict[str, Any]] = []
    for primary in sorted(primary_groups):
        quota = primary_quotas[primary]
        secondary_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in primary_groups[primary]:
            secondary_groups[item["minimum_mt_score_band"]].append(item)
        secondary_sizes = {
            key: len(values) for key, values in secondary_groups.items()
        }
        minimum = 1 if quota >= len(secondary_sizes) else 0
        secondary_quotas = allocate_with_minimum(
            secondary_sizes, total=quota, minimum=minimum
        )
        for secondary in sorted(secondary_groups):
            secondary_quota = secondary_quotas[secondary]
            chosen = _select_ranked(
                secondary_groups[secondary],
                count=secondary_quota,
                seed=seed,
                namespace=f"representative|{primary}|{secondary}",
            )
            for item in chosen:
                item.update(
                    {
                        "sample_group": "success_representative",
                        "selection_reason": "stratified_random",
                        "primary_stratum": primary,
                        "secondary_stratum": secondary,
                        "population_stratum_size": secondary_sizes[secondary],
                        "sample_stratum_size": secondary_quota,
                        "sampling_weight": round(
                            secondary_sizes[secondary] / secondary_quota, 8
                        ),
                    }
                )
                representative.append(item)
    if len(representative) != 150:
        raise ManualAuditPackageError(
            f"representative selection produced {len(representative)}"
        )
    representative.sort(
        key=lambda item: (
            item["primary_stratum"],
            item["secondary_stratum"],
            item["random_rank"],
        )
    )
    excluded = {item["dataset_id"] for item in representative}

    risk: list[dict[str, Any]] = []
    for reason, predicate in RISK_BUCKETS:
        candidates = [
            item
            for item in features
            if item["dataset_id"] not in excluded and predicate(item)
        ]
        chosen = _select_ranked(
            candidates,
            count=6,
            seed=seed,
            namespace=f"risk|{reason}",
        )
        for item in chosen:
            item.update(
                {
                    "sample_group": "success_risk",
                    "selection_reason": reason,
                    "primary_stratum": reason,
                    "secondary_stratum": item["minimum_mt_score_band"],
                    "population_stratum_size": len(candidates),
                    "sample_stratum_size": 6,
                    "sampling_weight": None,
                }
            )
            risk.append(item)
            excluded.add(item["dataset_id"])

    representative_and_risk = representative + risk
    for order, item in enumerate(representative_and_risk, start=1):
        item["sample_order"] = order
    return representative_and_risk


def _select_failed(
    failed_items: Sequence[Mapping[str, Any]],
    *,
    seed: str,
) -> list[dict[str, Any]]:
    features = [_failed_feature(item) for item in failed_items]
    stage_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in features:
        stage_groups[item["termination_reason"]].append(item)
    stage_sizes = {key: len(values) for key, values in stage_groups.items()}
    stage_quotas = allocate_with_minimum(
        stage_sizes, total=100, minimum=3
    )
    selected: list[dict[str, Any]] = []
    for stage in sorted(stage_groups):
        quota = stage_quotas[stage]
        source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in stage_groups[stage]:
            source_groups[item["source_dataset_name"]].append(item)
        source_sizes = {
            key: len(values) for key, values in source_groups.items()
        }
        minimum = 1 if quota >= len(source_sizes) else 0
        source_quotas = allocate_with_minimum(
            source_sizes, total=quota, minimum=minimum
        )
        for source in sorted(source_groups):
            source_quota = source_quotas[source]
            chosen = _select_ranked(
                source_groups[source],
                count=source_quota,
                seed=seed,
                namespace=f"failed|{stage}|{source}",
            )
            for item in chosen:
                item.update(
                    {
                        "sample_group": "failed_stratified",
                        "selection_reason": stage,
                        "primary_stratum": stage,
                        "secondary_stratum": source,
                        "population_stratum_size": source_sizes[source],
                        "sample_stratum_size": source_quota,
                        "sampling_weight": round(
                            source_sizes[source] / source_quota, 8
                        ),
                    }
                )
                selected.append(item)
    selected.sort(
        key=lambda item: (
            item["primary_stratum"],
            item["secondary_stratum"],
            item["random_rank"],
        )
    )
    for order, item in enumerate(selected, start=1):
        item["sample_order"] = order
    if len(selected) != 100:
        raise ManualAuditPackageError(
            f"failed selection produced {len(selected)}"
        )
    return selected


def select_audit_samples(
    success_items: Sequence[Mapping[str, Any]],
    failed_items: Sequence[Mapping[str, Any]],
    *,
    seed: str,
) -> dict[str, list[dict[str, Any]]]:
    success = _select_success(success_items, seed=seed)
    failed = _select_failed(failed_items, seed=seed)
    if len({item["dataset_id"] for item in success}) != 180:
        raise ManualAuditPackageError("duplicate successful sample task")
    if len({item["dataset_id"] for item in failed}) != 100:
        raise ManualAuditPackageError("duplicate failed sample task")
    return {"success": success, "failed": failed}


def _write_jsonl(path: Path, items: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for item in items:
            stream.write(str(item["raw_line"]))
            stream.write("\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _manifest_fields() -> list[str]:
    return [
        "sample_group",
        "sample_order",
        "dataset_id",
        "split",
        "source_dataset_name",
        "source_task_id",
        "source_dataset_index",
        "original_file",
        "original_line_number",
        "sample_file",
        "sample_line_number",
        "selection_reason",
        "primary_stratum",
        "secondary_stratum",
        "population_stratum_size",
        "sample_stratum_size",
        "sampling_weight",
        "random_rank",
        "record_sha256",
        "branch_coverage",
        "final_mode",
        "answer_count",
        "oj_candidate_count",
        "rp_candidate_count",
        "minimum_mt_score",
        "mean_mt_score",
        "maximum_mt_score",
        "termination_reason",
    ]


def _manifest_row(
    item: Mapping[str, Any],
    *,
    sample_file: str,
    sample_line_number: int,
) -> dict[str, Any]:
    return {
        "sample_group": item["sample_group"],
        "sample_order": item["sample_order"],
        "dataset_id": item["dataset_id"],
        "split": item["split"],
        "source_dataset_name": item["source_dataset_name"],
        "source_task_id": item["source_task_id"],
        "source_dataset_index": item["source_dataset_index"],
        "original_file": item["original_file"],
        "original_line_number": item["line_number"],
        "sample_file": sample_file,
        "sample_line_number": sample_line_number,
        "selection_reason": item["selection_reason"],
        "primary_stratum": item["primary_stratum"],
        "secondary_stratum": item["secondary_stratum"],
        "population_stratum_size": item["population_stratum_size"],
        "sample_stratum_size": item["sample_stratum_size"],
        "sampling_weight": item["sampling_weight"],
        "random_rank": item["random_rank"],
        "record_sha256": item["record_sha256"],
        "branch_coverage": item["branch_coverage"],
        "final_mode": item["final_mode"],
        "answer_count": item["answer_count"],
        "oj_candidate_count": item["oj_candidate_count"],
        "rp_candidate_count": item["rp_candidate_count"],
        "minimum_mt_score": item["minimum_mt_score"],
        "mean_mt_score": item["mean_mt_score"],
        "maximum_mt_score": item["maximum_mt_score"],
        "termination_reason": item["termination_reason"],
    }


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields))
        writer.writeheader()
        writer.writerows(rows)


def _answer_rows(
    success: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for task in success:
        for entry in task["answer_entries"]:
            answer = entry["answer"]
            detail = answer["mt_detail"]
            rows.append(
                {
                    "dataset_id": task["dataset_id"],
                    "sample_order": task["sample_order"],
                    "answer_order": entry["answer_order"],
                    "answer_key": entry["answer_key"],
                    "source_mode": entry["source_mode"],
                    "answer_role": entry["answer_role"],
                    "candidate_index": entry["candidate_index"],
                    "full_java_with_jml": answer["full_java_with_jml"],
                    "mutation_score": answer["mutation_score"],
                    "mt_killed": detail["killed"],
                    "mt_survived": detail["survived"],
                    "mt_total": detail["total"],
                    "answer_sha256": hashlib.sha256(
                        answer["full_java_with_jml"].encode("utf-8")
                    ).hexdigest(),
                }
            )
    return rows


def _sql_text(value: Any) -> str:
    if value is None:
        return "NULL"
    encoded = str(value).encode("utf-8").hex()
    if not encoded:
        return "''"
    return f"CONVERT(0x{encoded} USING utf8mb4)"


def _sql_number(value: Any) -> str:
    if value is None:
        return "NULL"
    return str(value)


def render_schema_sql(database_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise ManualAuditPackageError("unsafe database name")
    ratings = ",".join(f"'{value}'" for value in RATING_VALUES)
    return f"""CREATE DATABASE IF NOT EXISTS {database_name}
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE {database_name};

CREATE TABLE IF NOT EXISTS audit_tasks (
  dataset_id VARCHAR(64) NOT NULL,
  reviewer_code VARCHAR(8) NOT NULL DEFAULT '01',
  sample_group ENUM('success_representative','success_risk','failed_stratified') NOT NULL,
  sample_order INT NOT NULL,
  split ENUM('success','failed') NOT NULL,
  source_dataset_name VARCHAR(255) NOT NULL,
  source_task_id VARCHAR(512) NOT NULL,
  source_dataset_index INT NOT NULL,
  original_file VARCHAR(1024) NOT NULL,
  original_line_number INT NOT NULL,
  sample_file VARCHAR(1024) NOT NULL,
  sample_line_number INT NOT NULL,
  selection_reason VARCHAR(128) NOT NULL,
  primary_stratum VARCHAR(512) NOT NULL,
  secondary_stratum VARCHAR(255) NOT NULL,
  population_stratum_size INT NOT NULL,
  sample_stratum_size INT NOT NULL,
  sampling_weight DECIMAL(18,8) NULL,
  random_rank CHAR(64) NOT NULL,
  record_sha256 CHAR(64) NOT NULL,
  branch_coverage DECIMAL(10,6) NOT NULL,
  final_mode VARCHAR(32) NULL,
  answer_count INT NOT NULL,
  oj_candidate_count INT NOT NULL,
  rp_candidate_count INT NOT NULL,
  termination_reason VARCHAR(128) NULL,
  nl_description LONGTEXT NOT NULL,
  method_code LONGTEXT NOT NULL,
  full_code LONGTEXT NOT NULL,
  input_generator_code LONGTEXT NOT NULL,
  failure_info_json LONGTEXT NULL,
  review_status ENUM('NOT_STARTED','IN_PROGRESS','COMPLETED','NEEDS_ADJUDICATION') NOT NULL DEFAULT 'NOT_STARTED',
  evidence_comment LONGTEXT NULL,
  task_rating ENUM({ratings}) NULL,
  issue_codes_json LONGTEXT NULL,
  review_comment LONGTEXT NULL,
  confidence ENUM('HIGH','MEDIUM','LOW') NULL,
  PRIMARY KEY (dataset_id),
  UNIQUE KEY uq_task_sample_order (sample_group, sample_order),
  KEY idx_task_review_status (review_status),
  KEY idx_task_rating (task_rating),
  KEY idx_task_source (source_dataset_name),
  KEY idx_task_split (split)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS audit_answers (
  answer_id BIGINT NOT NULL AUTO_INCREMENT,
  dataset_id VARCHAR(64) NOT NULL,
  answer_order INT NOT NULL,
  answer_key VARCHAR(64) NOT NULL,
  source_mode ENUM('OJ','RP') NOT NULL,
  answer_role ENUM('FINAL','CANDIDATE') NOT NULL,
  candidate_index INT NULL,
  full_java_with_jml LONGTEXT NOT NULL,
  mutation_score DECIMAL(12,8) NOT NULL,
  mt_killed INT NOT NULL,
  mt_survived INT NOT NULL,
  mt_total INT NOT NULL,
  answer_sha256 CHAR(64) NOT NULL,
  review_status ENUM('NOT_STARTED','IN_PROGRESS','COMPLETED','NEEDS_ADJUDICATION') NOT NULL DEFAULT 'NOT_STARTED',
  evidence_comment LONGTEXT NULL,
  jml_syntax_status ENUM('PASS','FAIL','UNJUDGEABLE') NULL,
  source_fidelity_status ENUM('PASS','FAIL','UNJUDGEABLE') NULL,
  semantic_status ENUM({ratings}) NULL,
  overall_rating ENUM({ratings}) NULL,
  issue_codes_json LONGTEXT NULL,
  review_comment LONGTEXT NULL,
  confidence ENUM('HIGH','MEDIUM','LOW') NULL,
  PRIMARY KEY (answer_id),
  UNIQUE KEY uq_task_answer_key (dataset_id, answer_key),
  KEY idx_answer_review_status (review_status),
  KEY idx_answer_rating (overall_rating),
  CONSTRAINT fk_answer_task FOREIGN KEY (dataset_id)
    REFERENCES audit_tasks(dataset_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def render_seed_sql(
    tasks: Sequence[Mapping[str, Any]],
    answers: Sequence[Mapping[str, Any]],
    *,
    database_name: str,
    reviewer_code: str,
    success_sample_file: str,
    failed_sample_file: str,
) -> str:
    lines = [f"USE {database_name};", "SET NAMES utf8mb4;", ""]
    task_columns = (
        "dataset_id,reviewer_code,sample_group,sample_order,split,"
        "source_dataset_name,source_task_id,source_dataset_index,"
        "original_file,original_line_number,sample_file,sample_line_number,"
        "selection_reason,primary_stratum,secondary_stratum,"
        "population_stratum_size,sample_stratum_size,sampling_weight,"
        "random_rank,record_sha256,branch_coverage,final_mode,answer_count,"
        "oj_candidate_count,rp_candidate_count,termination_reason,"
        "nl_description,method_code,full_code,input_generator_code,"
        "failure_info_json"
    )
    for task in tasks:
        row = task["row"]
        sample_file = (
            success_sample_file
            if task["split"] == "success"
            else failed_sample_file
        )
        failure_json = (
            json.dumps(row["failure_info"], ensure_ascii=False, separators=(",", ":"))
            if task["split"] == "failed"
            else None
        )
        values = [
            _sql_text(task["dataset_id"]),
            _sql_text(reviewer_code),
            _sql_text(task["sample_group"]),
            _sql_number(task["sample_order"]),
            _sql_text(task["split"]),
            _sql_text(task["source_dataset_name"]),
            _sql_text(task["source_task_id"]),
            _sql_number(task["source_dataset_index"]),
            _sql_text(task["original_file"]),
            _sql_number(task["line_number"]),
            _sql_text(sample_file),
            _sql_number(task["sample_order"]),
            _sql_text(task["selection_reason"]),
            _sql_text(task["primary_stratum"]),
            _sql_text(task["secondary_stratum"]),
            _sql_number(task["population_stratum_size"]),
            _sql_number(task["sample_stratum_size"]),
            _sql_number(task["sampling_weight"]),
            _sql_text(task["random_rank"]),
            _sql_text(task["record_sha256"]),
            _sql_number(task["branch_coverage"]),
            _sql_text(task["final_mode"]),
            _sql_number(task["answer_count"]),
            _sql_number(task["oj_candidate_count"]),
            _sql_number(task["rp_candidate_count"]),
            _sql_text(task["termination_reason"]),
            _sql_text(row["nl_description"]),
            _sql_text(row["method_code"]),
            _sql_text(row["full_code"]),
            _sql_text(row["input_generator_code"]),
            _sql_text(failure_json),
        ]
        lines.append(
            f"INSERT IGNORE INTO audit_tasks ({task_columns}) VALUES "
            f"({','.join(values)});"
        )
    lines.append("")
    answer_columns = (
        "dataset_id,answer_order,answer_key,source_mode,answer_role,"
        "candidate_index,full_java_with_jml,mutation_score,mt_killed,"
        "mt_survived,mt_total,answer_sha256"
    )
    for answer in answers:
        values = [
            _sql_text(answer["dataset_id"]),
            _sql_number(answer["answer_order"]),
            _sql_text(answer["answer_key"]),
            _sql_text(answer["source_mode"]),
            _sql_text(answer["answer_role"]),
            _sql_number(answer["candidate_index"]),
            _sql_text(answer["full_java_with_jml"]),
            _sql_number(answer["mutation_score"]),
            _sql_number(answer["mt_killed"]),
            _sql_number(answer["mt_survived"]),
            _sql_number(answer["mt_total"]),
            _sql_text(answer["answer_sha256"]),
        ]
        lines.append(
            f"INSERT IGNORE INTO audit_answers ({answer_columns}) VALUES "
            f"({','.join(values)});"
        )
    lines.append("")
    return "\n".join(lines)


def render_progress_and_export_queries(database_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise ManualAuditPackageError("unsafe database name")
    return f"""USE {database_name};

-- 任务审查进度
SELECT review_status, COUNT(*) AS task_count
FROM audit_tasks
GROUP BY review_status
ORDER BY review_status;

-- 答案审查进度
SELECT review_status, COUNT(*) AS answer_count
FROM audit_answers
GROUP BY review_status
ORDER BY review_status;

-- 任务评级分布
SELECT COALESCE(task_rating, 'UNRATED') AS task_rating, COUNT(*) AS task_count
FROM audit_tasks
GROUP BY COALESCE(task_rating, 'UNRATED')
ORDER BY task_rating;

-- 答案评级分布
SELECT COALESCE(overall_rating, 'UNRATED') AS overall_rating,
       COUNT(*) AS answer_count
FROM audit_answers
GROUP BY COALESCE(overall_rating, 'UNRATED')
ORDER BY overall_rating;

-- 导出任务评价
SELECT dataset_id, reviewer_code, sample_group, sample_order, split,
       source_dataset_name, source_task_id, selection_reason,
       primary_stratum, secondary_stratum, sampling_weight,
       review_status, evidence_comment, task_rating,
       issue_codes_json, review_comment, confidence
FROM audit_tasks
ORDER BY split, sample_group, sample_order;

-- 导出答案评价
SELECT dataset_id, answer_order, answer_key, source_mode, answer_role,
       candidate_index, mutation_score, mt_killed, mt_survived, mt_total,
       answer_sha256, review_status, evidence_comment,
       jml_syntax_status, source_fidelity_status, semantic_status,
       overall_rating, issue_codes_json, review_comment, confidence
FROM audit_answers
ORDER BY dataset_id, answer_order;

-- 找出需要复核的任务
SELECT dataset_id, split, sample_group, sample_order,
       task_rating, review_status, review_comment
FROM audit_tasks
WHERE review_status = 'NEEDS_ADJUDICATION'
   OR task_rating = 'UNJUDGEABLE'
ORDER BY split, sample_order;
"""


def render_review_guide(
    *,
    database_name: str,
    reviewer_code: str,
    answer_count: int,
) -> str:
    return f"""# JSpecForge人工抽查任务说明与统一评价标准

## 1. 本审查包的任务

本包包含180条成功任务和100条失败任务，共280条任务。成功任务被抽中后，必须检查其中所有实际存在的final与candidate答案；当前包共包含{answer_count}个待审答案对象。

本阶段只建立抽样和记录环境。任何审查者都不得修改样本JSONL、抽样清单或MT分数。

## 2. 文件

- data/{SUCCESS_SAMPLE_NAME}：180条成功任务。
- data/{FAILED_SAMPLE_NAME}：100条失败任务。
- data/sample_manifest.csv：抽样来源、分层、权重和原始行号。
- csv/audit_tasks_{reviewer_code}.csv：任务级空白评价表。
- csv/audit_answers_{reviewer_code}.csv：答案级空白评价表。
- sql/00_create_schema_{reviewer_code}.sql：数据库与两张表的DDL。
- sql/01_seed_sample_{reviewer_code}.sql：280条任务及全部答案的种子数据。
- reports/抽样结果统计报告.md：样本构成。

MySQL数据库固定为 {database_name}。其他审查者复制本包时，将后缀和reviewer_code改为02、03、04，禁止覆盖01。

## 2.1 数据库初始化与核验

依次执行 sql/00_create_schema_{reviewer_code}.sql 和 sql/01_seed_sample_{reviewer_code}.sql。DDL只使用CREATE IF NOT EXISTS，种子数据使用INSERT IGNORE，不会覆盖已经填写的评价。

初始化后应核验：

    SELECT COUNT(*) FROM audit_tasks;
    SELECT COUNT(*) FROM audit_answers;
    SELECT sample_group, COUNT(*) FROM audit_tasks GROUP BY sample_group;
    SELECT review_status, COUNT(*) FROM audit_tasks GROUP BY review_status;
    SELECT review_status, COUNT(*) FROM audit_answers GROUP BY review_status;

01号包的预期值是audit_tasks=280、audit_answers={answer_count}，所有初始review_status均为NOT_STARTED。

## 3. 审查顺序

1. 先阅读自然语言描述、原始Java方法和完整代码。
2. 阅读当前答案中的JML，不先依据MT分数判断好坏。
3. 先在 evidence_comment 中写具体证据，再填写评级。
4. 每审完一个答案立即保存 audit_answers。
5. 一个成功任务的全部答案完成后，立即填写 audit_tasks。
6. 失败任务没有答案行，直接核对失败阶段、原因和错误证据后填写任务评价。

## 4. 答案检查维度

- JML语法与结构：注解是否是可识别的JML，是否存在明显语法或作用域问题。
- 源码保真：除JML外，Java方法源码是否保持不变。
- 语义正确性：requires、ensures、signals、assignable等是否与代码和题意一致。
- 前置条件合理性：不能通过过度限制输入来回避正常合法输入。
- 后置条件有效性：不能与代码矛盾、引用错误变量、越界或产生明显空洞结论。
- 可判定性：只有确实缺少必要信息时才能使用UNJUDGEABLE，不能因为规约复杂而使用。

## 5. 统一评级

### PASS

没有发现实质问题。JML与原Java代码、题意和正常输入范围一致。

### MINOR

存在局部、非关键问题，但不改变规约的主要语义。例如文字性冗余、轻微不完整或边缘输入描述不够精确。必须指出具体位置。

### MAJOR

存在会影响重要输入或核心行为的实质问题，但答案仍能辨认出合理意图。例如错误或过严的requires、关键ensures错误、重要分支遗漏、明显的运行时不安全表达式。

### INVALID

不能作为有效JML答案使用。例如根本性JML语法错误、修改Java方法源码、答案与任务无关、规约整体矛盾或伪造代码。

### UNJUDGEABLE

现有任务材料不足以作出可靠判断。必须写明缺少什么证据。不得把UNJUDGEABLE当作低置信度的普通评分。

## 6. 任务级评级

- 成功任务：原则上采用已审答案中的最严重等级，并在task_comment概括问题分布。
- 失败任务：评价失败分类和证据是否一致、完整、足以解释为什么没有答案。
- 如果同一任务内评价冲突或无法聚合，review_status填写NEEDS_ADJUDICATION。

严重程度顺序：INVALID高于MAJOR，MAJOR高于MINOR，MINOR高于PASS。UNJUDGEABLE单独处理。

## 7. 问题代码建议

- JML_SYNTAX
- SEMANTIC_CONTRADICTION
- OVER_RESTRICTIVE_PRECONDITION
- UNDER_SPECIFIED_POSTCONDITION
- SOURCE_FIDELITY
- JAVA_JML_MISMATCH
- VACUOUS_SPECIFICATION
- FAILURE_CLASSIFICATION
- MISSING_EVIDENCE
- OTHER

issue_codes_json填写JSON数组，例如 ["OVER_RESTRICTIVE_PRECONDITION","SEMANTIC_CONTRADICTION"]。

## 8. 评价偏差控制

- MT分数只能作为数据集内部指标，不是人工正确性的答案。
- 不因答案更长、注释更多或表达更自信而给更高评级。
- 不因OJ或RP来源而预设质量差异。
- 证据必须先于评级填写。
- 审查者不确定时写明冲突点，进入后续复核，不得猜测。

## 9. 数据保存规则

- 不设置或填写完成时间字段。
- 不删除种子行。
- 每次只更新当前答案或当前任务的评价字段。
- 数据库是01号审查者的权威记录；CSV用于人工查看、交换和必要时的离线填写。
"""


def _sample_statistics(
    success: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
    answers: Sequence[Mapping[str, Any]],
    *,
    seed: str,
) -> dict[str, Any]:
    scores = [float(answer["mutation_score"]) for answer in answers]
    answer_counts = [int(item["answer_count"]) for item in success]
    candidate_counts = [int(item["candidate_count"]) for item in success]
    return {
        "seed": seed,
        "sample_counts": {
            "success_representative": sum(
                item["sample_group"] == "success_representative"
                for item in success
            ),
            "success_risk": sum(
                item["sample_group"] == "success_risk" for item in success
            ),
            "success_total": len(success),
            "failed_total": len(failed),
            "all_tasks": len(success) + len(failed),
        },
        "answer_rows": len(answers),
        "answers_per_success_task": _simple_numeric_summary(answer_counts),
        "candidates_per_success_task": _simple_numeric_summary(candidate_counts),
        "mutation_score_summary": _simple_numeric_summary(scores),
        "mutation_score_bands": _score_band_counts(scores),
        "success_source_distribution": dict(
            Counter(item["source_dataset_name"] for item in success)
        ),
        "success_final_mode_distribution": dict(
            Counter(item["final_mode"] for item in success)
        ),
        "success_selection_reason_distribution": dict(
            Counter(item["selection_reason"] for item in success)
        ),
        "failed_stage_distribution": dict(
            Counter(item["termination_reason"] for item in failed)
        ),
        "failed_source_distribution": dict(
            Counter(item["source_dataset_name"] for item in failed)
        ),
        "answer_location_distribution": dict(
            Counter(
                f"{answer['answer_role'].lower()}_{answer['source_mode'].lower()}"
                for answer in answers
            )
        ),
    }


def render_sample_report(report: Mapping[str, Any]) -> str:
    counts = report["sample_counts"]
    locations = report["answer_location_distribution"]
    score_bands = report["mutation_score_bands"]
    answer_summary = report["answers_per_success_task"]
    candidate_summary = report["candidates_per_success_task"]
    score_summary = report["mutation_score_summary"]
    return f"""# JSpecForge人工抽查样本统计报告

生成时间：{report['generated_at_utc']}

## 1. 样本规模

- 成功代表性样本：{counts['success_representative']}
- 成功风险定向样本：{counts['success_risk']}
- 成功任务合计：{counts['success_total']}
- 失败任务：{counts['failed_total']}
- 全部任务：{counts['all_tasks']}
- 成功任务中的答案对象：{report['answer_rows']}
- 固定抽样种子：{report['seed']}

## 2. 成功任务构成

来源分布：{json.dumps(report['success_source_distribution'], ensure_ascii=False)}

最终答案模式：{json.dumps(report['success_final_mode_distribution'], ensure_ascii=False)}

抽样原因：{json.dumps(report['success_selection_reason_distribution'], ensure_ascii=False)}

### 2.1 答案位置

- final OJ：{locations.get('final_oj', 0)}
- final RP：{locations.get('final_rp', 0)}
- candidate OJ：{locations.get('candidate_oj', 0)}
- candidate RP：{locations.get('candidate_rp', 0)}
- 全部答案：{report['answer_rows']}

### 2.2 每任务答案与候选数量

- 答案数：最小{answer_summary['min']}，中位数{answer_summary['median']}，均值{answer_summary['mean']}，最大{answer_summary['max']}
- 候选数：最小{candidate_summary['min']}，中位数{candidate_summary['median']}，均值{candidate_summary['mean']}，最大{candidate_summary['max']}

### 2.3 MT分数

- 最小值：{score_summary['min']}
- 中位数：{score_summary['median']}
- 均值：{score_summary['mean']}
- 最大值：{score_summary['max']}
- 分数等于0：{score_bands['score_eq_0']}
- 分数大于0且小于0.25：{score_bands['score_0_0.25']}
- 分数大于等于0.25且小于0.5：{score_bands['score_0.25_0.5']}
- 分数大于等于0.5且小于0.75：{score_bands['score_0.5_0.75']}
- 分数大于等于0.75且小于1：{score_bands['score_0.75_1']}
- 分数等于1：{score_bands['score_eq_1']}

## 3. 失败任务构成

失败阶段：{json.dumps(report['failed_stage_distribution'], ensure_ascii=False)}

来源分布：{json.dumps(report['failed_source_distribution'], ensure_ascii=False)}

## 4. 复现与使用边界

- 代表性样本使用分层伪随机选择，并记录抽样权重。
- 风险样本是定向覆盖，不应用抽样权重估计总体比例。
- 两个JSONL保持正式数据记录内容不变，只改变行的集合和顺序。
- 本包尚未执行任何答案或任务评价。
"""


def prepare_package(
    *,
    success_input: Path,
    failed_input: Path,
    output_dir: Path,
    seed: str,
    database_name: str,
    reviewer_code: str,
) -> dict[str, Any]:
    if output_dir.exists():
        raise ManualAuditPackageError(f"output directory already exists: {output_dir}")
    success_items = load_jsonl_with_lines(success_input)
    failed_items = load_jsonl_with_lines(failed_input)
    selection = select_audit_samples(success_items, failed_items, seed=seed)
    success = selection["success"]
    failed = selection["failed"]
    answers = _answer_rows(success)

    data_dir = output_dir / "data"
    reports_dir = output_dir / "reports"
    sql_dir = output_dir / "sql"
    csv_dir = output_dir / "csv"
    success_path = data_dir / SUCCESS_SAMPLE_NAME
    failed_path = data_dir / FAILED_SAMPLE_NAME
    _write_jsonl(success_path, success)
    _write_jsonl(failed_path, failed)

    manifest_rows = [
        _manifest_row(
            item,
            sample_file=f"data/{SUCCESS_SAMPLE_NAME}",
            sample_line_number=index,
        )
        for index, item in enumerate(success, start=1)
    ] + [
        _manifest_row(
            item,
            sample_file=f"data/{FAILED_SAMPLE_NAME}",
            sample_line_number=index,
        )
        for index, item in enumerate(failed, start=1)
    ]
    _write_csv(data_dir / "sample_manifest.csv", manifest_rows, _manifest_fields())

    task_csv_fields = _manifest_fields() + [
        "review_status",
        "evidence_comment",
        "task_rating",
        "issue_codes_json",
        "review_comment",
        "confidence",
    ]
    task_csv_rows = [
        {
            **row,
            "review_status": "NOT_STARTED",
            "evidence_comment": "",
            "task_rating": "",
            "issue_codes_json": "",
            "review_comment": "",
            "confidence": "",
        }
        for row in manifest_rows
    ]
    _write_csv(
        csv_dir / f"audit_tasks_{reviewer_code}.csv",
        task_csv_rows,
        task_csv_fields,
    )

    answer_csv_fields = [
        "dataset_id",
        "sample_order",
        "answer_order",
        "answer_key",
        "source_mode",
        "answer_role",
        "candidate_index",
        "mutation_score",
        "mt_killed",
        "mt_survived",
        "mt_total",
        "answer_sha256",
        "review_status",
        "evidence_comment",
        "jml_syntax_status",
        "source_fidelity_status",
        "semantic_status",
        "overall_rating",
        "issue_codes_json",
        "review_comment",
        "confidence",
    ]
    answer_csv_rows = [
        {
            **{field: answer.get(field) for field in answer_csv_fields},
            "review_status": "NOT_STARTED",
            "evidence_comment": "",
            "jml_syntax_status": "",
            "source_fidelity_status": "",
            "semantic_status": "",
            "overall_rating": "",
            "issue_codes_json": "",
            "review_comment": "",
            "confidence": "",
        }
        for answer in answers
    ]
    _write_csv(
        csv_dir / f"audit_answers_{reviewer_code}.csv",
        answer_csv_rows,
        answer_csv_fields,
    )

    _write_text(
        sql_dir / f"00_create_schema_{reviewer_code}.sql",
        render_schema_sql(database_name),
    )
    _write_text(
        sql_dir / f"01_seed_sample_{reviewer_code}.sql",
        render_seed_sql(
            success + failed,
            answers,
            database_name=database_name,
            reviewer_code=reviewer_code,
            success_sample_file=f"data/{SUCCESS_SAMPLE_NAME}",
            failed_sample_file=f"data/{FAILED_SAMPLE_NAME}",
        ),
    )
    _write_text(
        sql_dir
        / f"02_review_progress_and_export_queries_{reviewer_code}.sql",
        render_progress_and_export_queries(database_name),
    )
    _write_text(
        output_dir / "人工抽查任务说明与统一评价标准.md",
        render_review_guide(
            database_name=database_name,
            reviewer_code=reviewer_code,
            answer_count=len(answers),
        ),
    )

    report = _sample_statistics(success, failed, answers, seed=seed)
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["input_files"] = {
        "success": {
            "path": str(success_input),
            "sha256": file_sha256(success_input),
            "records": len(success_items),
        },
        "failed": {
            "path": str(failed_input),
            "sha256": file_sha256(failed_input),
            "records": len(failed_items),
        },
    }
    report["output_files"] = {
        "success_sample": {
            "path": str(success_path),
            "sha256": file_sha256(success_path),
        },
        "failed_sample": {
            "path": str(failed_path),
            "sha256": file_sha256(failed_path),
        },
    }
    _write_json(reports_dir / "sample_statistics.json", report)
    _write_text(reports_dir / "抽样结果统计报告.md", render_sample_report(report))
    return report


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare the JSpecForge manual-audit sample package."
    )
    parser.add_argument("--success-input", type=Path, default=DEFAULT_SUCCESS_INPUT)
    parser.add_argument("--failed-input", type=Path, default=DEFAULT_FAILED_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--database-name", default=DEFAULT_DATABASE_NAME)
    parser.add_argument("--reviewer-code", default=DEFAULT_REVIEWER_CODE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = prepare_package(
        success_input=args.success_input,
        failed_input=args.failed_input,
        output_dir=args.output_dir,
        seed=args.seed,
        database_name=args.database_name,
        reviewer_code=args.reviewer_code,
    )
    print(
        "manual audit package prepared: "
        f"success={report['sample_counts']['success_total']} "
        f"failed={report['sample_counts']['failed_total']} "
        f"answers={report['answer_rows']}"
    )
    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
