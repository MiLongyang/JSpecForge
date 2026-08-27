"""Prepare the schema-clean JSpecForge submission dataset from frozen V4 data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SUCCESS_INPUT = (
    _ROOT
    / "data"
    / "final"
    / "jspecforge_final_success_fused_279_recovered_mtfix_v4_20260714.jsonl"
)
DEFAULT_FAILED_INPUT = (
    _ROOT
    / "data"
    / "final"
    / "jspecforge_final_failed_fused_279_recovered_mtfix_v4_20260714.jsonl"
)
DEFAULT_RAW_INPUT = _ROOT / "data" / "data.jsonl"
DEFAULT_OUTPUT_DIR = _ROOT / "data" / "submission"
DEFAULT_REPORT_DIR = (
    _ROOT / "train" / "reports" / "submission_schema_cleanup_v3_20260719"
)

SUCCESS_OUTPUT_NAME = "jspecforge_submission_success.jsonl"
FAILED_OUTPUT_NAME = "jspecforge_submission_failed.jsonl"
JSON_REPORT_NAME = "schema_cleanup_report.json"
MARKDOWN_REPORT_NAME = "execution_report.md"

SUCCESS_FIELDS = (
    "dataset_id",
    "split",
    "source_dataset_name",
    "source_task_id",
    "source_dataset_index",
    "nl_description",
    "method_code",
    "full_code",
    "input_generator_code",
    "branch_coverage",
    "final_answer",
    "oj_candidates",
    "rp_candidates",
)

FAILED_FIELDS = (
    "dataset_id",
    "split",
    "source_dataset_name",
    "source_task_id",
    "source_dataset_index",
    "nl_description",
    "method_code",
    "full_code",
    "input_generator_code",
    "branch_coverage",
    "termination_reason",
    "failure_info",
)

FAILURE_INFO_FIELDS = (
    "failure_stage",
    "failure_reason",
    "last_error_detail",
)

ANSWER_FIELDS = (
    "full_java_with_jml",
    "mutation_score",
    "mt_detail",
)

MT_DETAIL_FIELDS = (
    "killed",
    "survived",
    "total",
)

EXPECTED_SUCCESS_SHA256 = "ab1ea8deef0e05dbb831be9bf0925d651d7c55ca2737b9dcee784fcef461f392"
EXPECTED_FAILED_SHA256 = "999630626c153ab6f2772113638e1802834602e4ffd107b601fcc2e5fe08ce30"
EXPECTED_SUCCESS_OUTPUT_SHA256 = "384f3ec21c4f194adbfa3d3ab30e9c364a7b06399c3a3fddc4ccd226a2f7c5d2"
EXPECTED_FAILED_OUTPUT_SHA256 = "982a1ad51fe2f02a42bce6b636618c1fdbaccb1acb6e711ec2365cd470016444"
EXPECTED_SUCCESS_OUTPUT_BYTES = 31076694
EXPECTED_FAILED_OUTPUT_BYTES = 4555934
EXPECTED_RECORD_COUNTS = {"success": 2717, "failed": 462, "total": 3179}
EXPECTED_ANSWER_COUNTS = {
    "final_oj": 1679,
    "final_rp": 2076,
    "candidate_oj": 2108,
    "candidate_rp": 5157,
    "all": 11020,
}
EXPECTED_FAILURE_COUNTS = {
    "failed_oj_bug": 306,
    "exhausted_syntax": 97,
    "exhausted_rounds": 23,
    "failed_source_fidelity": 22,
    "failed_pbt_insufficient_input": 8,
    "failed_pbt_run": 6,
}

SOURCE_FIDELITY_STAGE = "failed_source_fidelity"
SOURCE_FIDELITY_REASON_HEAD = (
    "Task A 源码保真检查重试上限耗尽：模型修改了原始 Java 方法源码"
)
SOURCE_FIDELITY_DETAIL = (
    "=== 最后一次源码保真检查错误 ===\n"
    "The previous answer modified the original Java method source. "
    "Only JML annotations may be changed."
)

_SUCCESS_ALLOWED_INPUT_FIELDS = set(SUCCESS_FIELDS) | {"recovery_source"}
_FAILED_ALLOWED_INPUT_FIELDS = set(FAILED_FIELDS) | {
    "recovery_source",
    "final_answer",
    "oj_candidates",
    "rp_candidates",
}
_ANSWER_ALLOWED_INPUT_FIELDS = set(ANSWER_FIELDS) | {
    "a_round",
    "ig_round",
    "selected_candidate_index",
}
_MT_DETAIL_ALLOWED_INPUT_FIELDS = set(MT_DETAIL_FIELDS) | {
    "feedback_text",
    "candidate_uid",
    "patched_from",
}
_NESTED_FORBIDDEN_FIELDS = (
    "a_round",
    "ig_round",
    "selected_candidate_index",
    "feedback_text",
    "candidate_uid",
    "patched_from",
)


class DatasetPreparationError(ValueError):
    """Raised when an input violates the frozen submission contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                raise DatasetPreparationError(f"{path} line {line_number}: blank JSONL row")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetPreparationError(
                    f"{path} line {line_number}: invalid JSON"
                ) from exc
            if not isinstance(row, dict):
                raise DatasetPreparationError(
                    f"{path} line {line_number}: expected JSON object"
                )
            rows.append(row)
    return rows


def _require_nonempty_string(value: Any, context: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DatasetPreparationError(f"{context}: expected non-empty string")


def _require_integer(value: Any, context: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetPreparationError(f"{context}: expected integer")


def _require_number(value: Any, context: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetPreparationError(f"{context}: expected number")


def _validate_task_identity(row: Mapping[str, Any], context: str, split: str) -> None:
    for field in (
        "dataset_id",
        "source_dataset_name",
        "source_task_id",
        "nl_description",
        "method_code",
        "full_code",
    ):
        _require_nonempty_string(row.get(field), f"{context}.{field}")
    _require_integer(row.get("source_dataset_index"), f"{context}.source_dataset_index")
    if row.get("split") != split:
        raise DatasetPreparationError(
            f"{context}.split: expected {split!r}, got {row.get('split')!r}"
        )


def _reject_field_mismatch(
    row: Mapping[str, Any], allowed: set[str], required: Iterable[str], context: str
) -> None:
    unexpected = sorted(set(row) - allowed)
    missing = sorted(set(required) - set(row))
    if unexpected:
        raise DatasetPreparationError(f"{context}: unexpected fields {unexpected}")
    if missing:
        raise DatasetPreparationError(f"{context}: missing fields {missing}")


def clean_answer_payload(answer: Any, context: str) -> dict[str, Any] | None:
    if answer is None:
        return None
    if not isinstance(answer, dict):
        raise DatasetPreparationError(f"{context}: expected object or null")
    _reject_field_mismatch(
        answer, _ANSWER_ALLOWED_INPUT_FIELDS, ANSWER_FIELDS, context
    )
    _require_nonempty_string(
        answer.get("full_java_with_jml"), f"{context}.full_java_with_jml"
    )
    mutation_score = answer.get("mutation_score")
    _require_number(mutation_score, f"{context}.mutation_score")
    if not 0 <= mutation_score <= 1:
        raise DatasetPreparationError(
            f"{context}.mutation_score: expected value in [0, 1]"
        )

    detail = answer.get("mt_detail")
    if not isinstance(detail, dict):
        raise DatasetPreparationError(f"{context}.mt_detail: expected object")
    _reject_field_mismatch(
        detail,
        _MT_DETAIL_ALLOWED_INPUT_FIELDS,
        MT_DETAIL_FIELDS,
        f"{context}.mt_detail",
    )
    for field in MT_DETAIL_FIELDS:
        _require_integer(detail.get(field), f"{context}.mt_detail.{field}")
        if detail[field] < 0:
            raise DatasetPreparationError(
                f"{context}.mt_detail.{field}: expected non-negative integer"
            )
    if detail["killed"] + detail["survived"] != detail["total"]:
        raise DatasetPreparationError(
            f"{context}.mt_detail: killed + survived must equal total"
        )

    return {
        "full_java_with_jml": answer["full_java_with_jml"],
        "mutation_score": mutation_score,
        "mt_detail": {field: detail[field] for field in MT_DETAIL_FIELDS},
    }


def build_raw_input_index(
    raw_rows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int, str], Mapping[str, Any]]:
    index: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for position, row in enumerate(raw_rows, start=1):
        context = f"raw input row {position}"
        _require_nonempty_string(row.get("source_dataset"), f"{context}.source_dataset")
        _require_integer(row.get("source_index"), f"{context}.source_index")
        _require_nonempty_string(row.get("task_id"), f"{context}.task_id")
        key = (row["source_dataset"], row["source_index"], row["task_id"])
        if key in index:
            raise DatasetPreparationError(f"duplicate raw input key: {key!r}")
        index[key] = row
    return index


def clean_success_records(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cleaned: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for position, row in enumerate(rows, start=1):
        context = f"success row {position}"
        _reject_field_mismatch(
            row, _SUCCESS_ALLOWED_INPUT_FIELDS, SUCCESS_FIELDS, context
        )
        _validate_task_identity(row, context, "success")
        _require_nonempty_string(
            row.get("input_generator_code"), f"{context}.input_generator_code"
        )
        _require_number(row.get("branch_coverage"), f"{context}.branch_coverage")

        final_answer = row.get("final_answer")
        if not isinstance(final_answer, dict) or set(final_answer) != {"oj", "rp"}:
            raise DatasetPreparationError(
                f"{context}.final_answer: expected exactly oj and rp keys"
            )
        if not any(isinstance(final_answer.get(mode), dict) for mode in ("oj", "rp")):
            raise DatasetPreparationError(f"{context}.final_answer: both answers are empty")
        for mode in ("oj", "rp"):
            value = final_answer.get(mode)
            if value is not None and not isinstance(value, dict):
                raise DatasetPreparationError(
                    f"{context}.final_answer.{mode}: expected object or null"
                )
        for field in ("oj_candidates", "rp_candidates"):
            if not isinstance(row.get(field), list):
                raise DatasetPreparationError(f"{context}.{field}: expected array")

        clean_final = {
            mode: clean_answer_payload(
                final_answer[mode], f"{context}.final_answer.{mode}"
            )
            for mode in ("oj", "rp")
        }
        clean_candidates: dict[str, list[dict[str, Any]]] = {}
        for mode in ("oj", "rp"):
            field = f"{mode}_candidates"
            candidates: list[dict[str, Any]] = []
            for candidate_index, candidate in enumerate(row[field]):
                cleaned_candidate = clean_answer_payload(
                    candidate, f"{context}.{field}[{candidate_index}]"
                )
                if cleaned_candidate is None:
                    raise DatasetPreparationError(
                        f"{context}.{field}[{candidate_index}]: candidate cannot be null"
                    )
                candidates.append(cleaned_candidate)
            clean_candidates[mode] = candidates

        output_values = {
            **row,
            "final_answer": clean_final,
            "oj_candidates": clean_candidates["oj"],
            "rp_candidates": clean_candidates["rp"],
        }
        cleaned.append({field: output_values[field] for field in SUCCESS_FIELDS})
        if "recovery_source" in row:
            removed.append(
                {
                    "dataset_id": row["dataset_id"],
                    "removed_fields": ["recovery_source"],
                }
            )

    return cleaned, removed


def _raw_key(row: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        row["source_dataset_name"],
        row["source_dataset_index"],
        row["source_task_id"],
    )


def _validate_failure_info(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or tuple(value) != FAILURE_INFO_FIELDS:
        raise DatasetPreparationError(
            f"{context}: expected fields {list(FAILURE_INFO_FIELDS)} in order"
        )
    _require_nonempty_string(value.get("failure_stage"), f"{context}.failure_stage")
    _require_nonempty_string(value.get("failure_reason"), f"{context}.failure_reason")
    detail = value.get("last_error_detail")
    if detail is not None and not isinstance(detail, str):
        raise DatasetPreparationError(
            f"{context}.last_error_detail: expected string or null"
        )
    return value


def normalize_failure_info(
    failure_info: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    normalized = dict(failure_info)
    if normalized["failure_stage"] != SOURCE_FIDELITY_STAGE:
        return normalized, False

    old_reason = normalized["failure_reason"]
    suffix_start = old_reason.find("（")
    suffix = old_reason[suffix_start:] if suffix_start >= 0 else ""
    normalized["failure_reason"] = SOURCE_FIDELITY_REASON_HEAD + suffix

    old_detail = normalized["last_error_detail"]
    if old_detail is None or not old_detail.strip():
        normalized["last_error_detail"] = SOURCE_FIDELITY_DETAIL
    elif SOURCE_FIDELITY_DETAIL not in old_detail:
        normalized["last_error_detail"] = (
            old_detail.rstrip() + "\n\n" + SOURCE_FIDELITY_DETAIL
        )

    return normalized, normalized != failure_info


def _failure_info_normalization_report(
    source_rows: Sequence[Mapping[str, Any]],
    clean_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for source, cleaned in zip(source_rows, clean_rows):
        source_info = source["failure_info"]
        clean_info = cleaned["failure_info"]
        if source_info["failure_stage"] != SOURCE_FIDELITY_STAGE:
            continue
        source_detail = source_info["last_error_detail"]
        records.append(
            {
                "dataset_id": source["dataset_id"],
                "reason_rewritten": (
                    source_info["failure_reason"] != clean_info["failure_reason"]
                ),
                "detail_appended": (
                    source_info["last_error_detail"]
                    != clean_info["last_error_detail"]
                ),
                "null_detail_filled": (
                    source_detail is None
                    or (
                        isinstance(source_detail, str)
                        and not source_detail.strip()
                    )
                ),
            }
        )

    counts = {
        "source_fidelity_records": len(records),
        "source_fidelity_reason_rewritten": sum(
            record["reason_rewritten"] for record in records
        ),
        "source_fidelity_detail_appended": sum(
            record["detail_appended"] for record in records
        ),
        "source_fidelity_null_detail_filled": sum(
            record["null_detail_filled"] for record in records
        ),
    }
    return counts, records


def _validate_complete_failure_details(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    null_or_blank: list[str] = []
    source_fidelity = 0
    source_fidelity_reason_complete = 0
    source_fidelity_detail_complete = 0
    for row in rows:
        failure_info = row["failure_info"]
        detail = failure_info["last_error_detail"]
        if not isinstance(detail, str) or not detail.strip():
            null_or_blank.append(row["dataset_id"])
        if failure_info["failure_stage"] != SOURCE_FIDELITY_STAGE:
            continue
        source_fidelity += 1
        if (
            "未知终止原因" not in failure_info["failure_reason"]
            and failure_info["failure_reason"].startswith(
                SOURCE_FIDELITY_REASON_HEAD
            )
        ):
            source_fidelity_reason_complete += 1
        if isinstance(detail, str) and SOURCE_FIDELITY_DETAIL in detail:
            source_fidelity_detail_complete += 1

    if null_or_blank:
        raise DatasetPreparationError(
            "failed last_error_detail is null or blank: "
            + ", ".join(null_or_blank)
        )
    if source_fidelity_reason_complete != source_fidelity:
        raise DatasetPreparationError(
            "failed_source_fidelity failure_reason normalization incomplete"
        )
    if source_fidelity_detail_complete != source_fidelity:
        raise DatasetPreparationError(
            "failed_source_fidelity last_error_detail normalization incomplete"
        )
    return {
        "nonempty_last_error_detail": len(rows),
        "source_fidelity_records": source_fidelity,
        "source_fidelity_reason_complete": source_fidelity_reason_complete,
        "source_fidelity_detail_complete": source_fidelity_detail_complete,
    }


def clean_failed_records(
    rows: Sequence[Mapping[str, Any]],
    raw_index: Mapping[tuple[str, int, str], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cleaned: list[dict[str, Any]] = []
    supplements: list[dict[str, Any]] = []
    required = set(FAILED_FIELDS) - {
        "input_generator_code",
        "branch_coverage",
        "termination_reason",
    }

    for position, row in enumerate(rows, start=1):
        context = f"failed row {position}"
        _reject_field_mismatch(row, _FAILED_ALLOWED_INPUT_FIELDS, required, context)
        _validate_task_identity(row, context, "failed")
        source_failure_info = _validate_failure_info(
            row.get("failure_info"), f"{context}.failure_info"
        )
        failure_info, _ = normalize_failure_info(source_failure_info)

        key = _raw_key(row)
        raw = raw_index.get(key)
        if raw is None:
            raise DatasetPreparationError(f"{context}: no raw input match for {key!r}")
        raw_input_generator_code = raw.get("input_generator_code")
        if (
            not isinstance(raw_input_generator_code, str)
            or not raw_input_generator_code.strip()
        ):
            raise DatasetPreparationError(
                f"{context}: raw input_generator_code is missing or empty"
            )
        raw_branch_coverage = raw.get("branch_coverage")
        if isinstance(raw_branch_coverage, bool) or not isinstance(
            raw_branch_coverage, (int, float)
        ):
            raise DatasetPreparationError(
                f"{context}: raw branch_coverage is missing or not numeric"
            )

        supplemented_fields: list[str] = []
        input_generator_code = row.get("input_generator_code")
        if not isinstance(input_generator_code, str) or not input_generator_code.strip():
            input_generator_code = raw_input_generator_code
            supplemented_fields.append("input_generator_code")
        elif input_generator_code != raw_input_generator_code:
            raise DatasetPreparationError(
                f"{context}: input_generator_code differs from raw input"
            )

        branch_coverage = row.get("branch_coverage")
        if branch_coverage is None:
            branch_coverage = raw_branch_coverage
            supplemented_fields.append("branch_coverage")
        else:
            _require_number(branch_coverage, f"{context}.branch_coverage")
            if branch_coverage != raw_branch_coverage:
                raise DatasetPreparationError(
                    f"{context}: branch_coverage differs from raw input"
                )

        output_values = {
            **row,
            "input_generator_code": input_generator_code,
            "branch_coverage": branch_coverage,
            "termination_reason": failure_info["failure_stage"],
            "failure_info": failure_info,
        }
        cleaned.append({field: output_values[field] for field in FAILED_FIELDS})
        if supplemented_fields:
            supplements.append(
                {
                    "dataset_id": row["dataset_id"],
                    "supplemented_fields": supplemented_fields,
                }
            )

    return cleaned, supplements


def _answer_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "final_oj": sum(bool(row["final_answer"]["oj"]) for row in rows),
        "final_rp": sum(bool(row["final_answer"]["rp"]) for row in rows),
        "candidate_oj": sum(len(row["oj_candidates"]) for row in rows),
        "candidate_rp": sum(len(row["rp_candidates"]) for row in rows),
    }
    counts["all"] = sum(counts.values())
    return counts


def _iter_answers(
    rows: Sequence[Mapping[str, Any]],
) -> Iterable[Mapping[str, Any]]:
    for row in rows:
        for mode in ("oj", "rp"):
            answer = row["final_answer"][mode]
            if answer is not None:
                yield answer
        yield from row["oj_candidates"]
        yield from row["rp_candidates"]


def _nested_forbidden_counts(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = Counter({field: 0 for field in _NESTED_FORBIDDEN_FIELDS})
    for answer in _iter_answers(rows):
        detail = answer.get("mt_detail")
        for field in _NESTED_FORBIDDEN_FIELDS:
            if field in answer or (isinstance(detail, dict) and field in detail):
                counts[field] += 1
    return {field: counts[field] for field in _NESTED_FORBIDDEN_FIELDS}


def _validate_clean_answer_payloads(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        "answers": 0,
        "strict_answer_schema": 0,
        "strict_mt_detail_schema": 0,
        "valid_mutation_score": 0,
        "valid_mutant_relation": 0,
    }
    for answer in _iter_answers(rows):
        counts["answers"] += 1
        if tuple(answer) == ANSWER_FIELDS:
            counts["strict_answer_schema"] += 1
        detail = answer.get("mt_detail")
        if isinstance(detail, dict) and tuple(detail) == MT_DETAIL_FIELDS:
            counts["strict_mt_detail_schema"] += 1
        score = answer.get("mutation_score")
        if (
            not isinstance(score, bool)
            and isinstance(score, (int, float))
            and 0 <= score <= 1
        ):
            counts["valid_mutation_score"] += 1
        if isinstance(detail, dict):
            values = [detail.get(field) for field in MT_DETAIL_FIELDS]
            if (
                all(isinstance(value, int) and not isinstance(value, bool) for value in values)
                and all(value >= 0 for value in values)
                and detail["killed"] + detail["survived"] == detail["total"]
            ):
                counts["valid_mutant_relation"] += 1

    expected = counts["answers"]
    failed = {
        field: value
        for field, value in counts.items()
        if field != "answers" and value != expected
    }
    forbidden = _nested_forbidden_counts(rows)
    if failed or any(forbidden.values()):
        raise DatasetPreparationError(
            f"nested answer validation failed: checks={failed}, forbidden={forbidden}"
        )
    return counts


def _validate_unique_identity(
    success: Sequence[Mapping[str, Any]], failed: Sequence[Mapping[str, Any]]
) -> dict[str, int]:
    success_ids = [row["dataset_id"] for row in success]
    failed_ids = [row["dataset_id"] for row in failed]
    success_tasks = [row["source_task_id"] for row in success]
    failed_tasks = [row["source_task_id"] for row in failed]

    checks = {
        "success_duplicate_dataset_id": len(success_ids) - len(set(success_ids)),
        "failed_duplicate_dataset_id": len(failed_ids) - len(set(failed_ids)),
        "success_duplicate_source_task_id": len(success_tasks) - len(set(success_tasks)),
        "failed_duplicate_source_task_id": len(failed_tasks) - len(set(failed_tasks)),
        "success_failed_dataset_id_intersection": len(set(success_ids) & set(failed_ids)),
        "success_failed_source_task_id_intersection": len(
            set(success_tasks) & set(failed_tasks)
        ),
    }
    nonzero = {name: value for name, value in checks.items() if value}
    if nonzero:
        raise DatasetPreparationError(f"identity validation failed: {nonzero}")
    return checks


def _validate_known_v4(
    success: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
    success_hash: str,
    failed_hash: str,
) -> None:
    if success_hash != EXPECTED_SUCCESS_SHA256:
        raise DatasetPreparationError(
            f"unexpected V4 success SHA-256: {success_hash}"
        )
    if failed_hash != EXPECTED_FAILED_SHA256:
        raise DatasetPreparationError(f"unexpected V4 failed SHA-256: {failed_hash}")
    actual_counts = {
        "success": len(success),
        "failed": len(failed),
        "total": len(success) + len(failed),
    }
    if actual_counts != EXPECTED_RECORD_COUNTS:
        raise DatasetPreparationError(
            f"unexpected V4 record counts: {actual_counts}"
        )
    answers = _answer_counts(success)
    if answers != EXPECTED_ANSWER_COUNTS:
        raise DatasetPreparationError(f"unexpected V4 answer counts: {answers}")
    reasons = dict(Counter(row["termination_reason"] for row in failed))
    if reasons != EXPECTED_FAILURE_COUNTS:
        raise DatasetPreparationError(f"unexpected failure distribution: {reasons}")


def _validate_content_invariance(
    source_success: Sequence[Mapping[str, Any]],
    clean_success: Sequence[Mapping[str, Any]],
    source_failed: Sequence[Mapping[str, Any]],
    clean_failed: Sequence[Mapping[str, Any]],
    raw_index: Mapping[tuple[str, int, str], Mapping[str, Any]],
) -> None:
    if len(source_success) != len(clean_success):
        raise DatasetPreparationError("success record count changed during cleanup")
    if len(source_failed) != len(clean_failed):
        raise DatasetPreparationError("failed record count changed during cleanup")
    for position, (source, cleaned) in enumerate(
        zip(source_success, clean_success), start=1
    ):
        expected_values = {
            **source,
            "final_answer": {
                mode: clean_answer_payload(
                    source["final_answer"][mode],
                    f"success row {position}.final_answer.{mode}",
                )
                for mode in ("oj", "rp")
            },
            "oj_candidates": [
                clean_answer_payload(
                    answer, f"success row {position}.oj_candidates[{index}]"
                )
                for index, answer in enumerate(source["oj_candidates"])
            ],
            "rp_candidates": [
                clean_answer_payload(
                    answer, f"success row {position}.rp_candidates[{index}]"
                )
                for index, answer in enumerate(source["rp_candidates"])
            ],
        }
        expected = {field: expected_values[field] for field in SUCCESS_FIELDS}
        if cleaned != expected:
            raise DatasetPreparationError(
                f"success row {position}: content changed outside allowed cleanup"
            )

    for position, (source, cleaned) in enumerate(
        zip(source_failed, clean_failed), start=1
    ):
        raw = raw_index[_raw_key(source)]
        expected = {
            field: source[field]
            for field in FAILED_FIELDS
            if field
            not in {"input_generator_code", "branch_coverage", "termination_reason"}
        }
        expected["input_generator_code"] = source.get(
            "input_generator_code", raw["input_generator_code"]
        )
        if not expected["input_generator_code"]:
            expected["input_generator_code"] = raw["input_generator_code"]
        expected["branch_coverage"] = source.get(
            "branch_coverage", raw["branch_coverage"]
        )
        if expected["branch_coverage"] is None:
            expected["branch_coverage"] = raw["branch_coverage"]
        expected["termination_reason"] = source["failure_info"]["failure_stage"]
        expected["failure_info"], _ = normalize_failure_info(
            source["failure_info"]
        )
        expected = {field: expected[field] for field in FAILED_FIELDS}
        if cleaned != expected:
            raise DatasetPreparationError(
                f"failed row {position}: content changed outside allowed cleanup"
            )


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2))
        stream.write("\n")


def _markdown_report(report: Mapping[str, Any]) -> str:
    counts = report["record_counts"]
    removed = report["removed_field_counts"]
    supplemented = report["supplemented_field_counts"]
    answers = report["answer_counts"]
    nested_removed = report["nested_removed_field_counts"]
    nested_checks = report["nested_answer_checks"]
    normalized = report["failure_info_normalization_counts"]
    failure_checks = report["failure_detail_checks"]
    return f"""# JSpecForge正式提交数据集字段清洗执行报告

## 输入与输出

- 输入成功集：`{report['inputs']['success']['path']}`
- 输入失败集：`{report['inputs']['failed']['path']}`
- 原始输入：`{report['inputs']['raw']['path']}`
- 输出成功集：`{report['outputs']['success']['path']}`
- 输出失败集：`{report['outputs']['failed']['path']}`

## 处理结果

- 成功记录：{counts['success']}
- 失败记录：{counts['failed']}
- 总记录：{counts['total']}
- 删除成功集 `recovery_source`：{removed['success_recovery_source']}
- 删除失败集 `recovery_source`：{removed['failed_recovery_source']}
- 删除失败集答案字段：{removed['failed_answer_fields']}
- 补齐 `input_generator_code`：{supplemented['input_generator_code']}
- 补齐 `branch_coverage`：{supplemented['branch_coverage']}
- 规范化源码保真失败原因：{normalized['source_fidelity_reason_rewritten']}
- 追加源码保真失败证据：{normalized['source_fidelity_detail_appended']}
- 回填原为空的失败详情：{normalized['source_fidelity_null_detail_filled']}
- 删除答案字段 `a_round`：{nested_removed['a_round']}
- 删除答案字段 `ig_round`：{nested_removed['ig_round']}
- 删除答案字段 `selected_candidate_index`：{nested_removed['selected_candidate_index']}
- 删除MT字段 `feedback_text`：{nested_removed['feedback_text']}
- 删除MT字段 `candidate_uid`：{nested_removed['candidate_uid']}
- 删除MT字段 `patched_from`：{nested_removed['patched_from']}

## 内容验收

- 最终OJ答案：{answers['final_oj']}
- 最终RP答案：{answers['final_rp']}
- OJ候选答案：{answers['candidate_oj']}
- RP候选答案：{answers['candidate_rp']}
- 全部答案：{answers['all']}
- 严格答案Schema：{nested_checks['strict_answer_schema']}/{nested_checks['answers']}
- 严格MT详情Schema：{nested_checks['strict_mt_detail_schema']}/{nested_checks['answers']}
- 合法MT分数：{nested_checks['valid_mutation_score']}/{nested_checks['answers']}
- 合法变异体数量关系：{nested_checks['valid_mutant_relation']}/{nested_checks['answers']}
- 非空失败详情：{failure_checks['nonempty_last_error_detail']}/{counts['failed']}
- 源码保真失败原因完整：{failure_checks['source_fidelity_reason_complete']}/{failure_checks['source_fidelity_records']}
- 源码保真失败证据完整：{failure_checks['source_fidelity_detail_complete']}/{failure_checks['source_fidelity_records']}
- 成功集严格Schema：通过
- 失败集严格Schema：通过
- 任务身份、任务归属和记录顺序：通过
- 答案内容、候选顺序、MT分数与MT详情不变：通过
- 仅源码保真失败说明按规则回填，其他 `failure_info` 不变：通过
- 原始输入三元组唯一匹配：通过

## 输出哈希

- 成功集SHA-256：`{report['outputs']['success']['sha256']}`
- 失败集SHA-256：`{report['outputs']['failed']['sha256']}`

补齐字段的完整 `dataset_id` 清单记录在同目录的 `schema_cleanup_report.json` 中。
"""


def _publish_outputs(
    success: Sequence[Mapping[str, Any]],
    failed: Sequence[Mapping[str, Any]],
    report: dict[str, Any],
    output_dir: Path,
    report_dir: Path,
    *,
    replace_existing: bool,
    enforce_expected_outputs: bool,
) -> dict[str, Any]:
    success_target = output_dir / SUCCESS_OUTPUT_NAME
    failed_target = output_dir / FAILED_OUTPUT_NAME
    json_report_target = report_dir / JSON_REPORT_NAME
    markdown_report_target = report_dir / MARKDOWN_REPORT_NAME
    data_targets = (success_target, failed_target)
    report_targets = (json_report_target, markdown_report_target)
    existing_data = [str(path) for path in data_targets if path.exists()]
    existing_reports = [str(path) for path in report_targets if path.exists()]
    if existing_data and not replace_existing:
        raise DatasetPreparationError(
            f"refusing to overwrite existing outputs: {existing_data}"
        )
    if existing_reports:
        raise DatasetPreparationError(
            f"refusing to overwrite existing reports: {existing_reports}"
        )

    staging_root = output_dir.parent / f".submission_schema_v3_{uuid.uuid4().hex}"
    staging_data = staging_root / "data"
    staging_reports = staging_root / "reports"
    staging_success = staging_data / SUCCESS_OUTPUT_NAME
    staging_failed = staging_data / FAILED_OUTPUT_NAME
    staging_json_report = staging_reports / JSON_REPORT_NAME
    staging_markdown_report = staging_reports / MARKDOWN_REPORT_NAME
    staging_data.mkdir(parents=True, exist_ok=False)
    staging_reports.mkdir(parents=True, exist_ok=False)

    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        _write_jsonl(staging_success, success)
        _write_jsonl(staging_failed, failed)
        success_output_hash = _sha256(staging_success)
        failed_output_hash = _sha256(staging_failed)
        success_output_bytes = staging_success.stat().st_size
        failed_output_bytes = staging_failed.stat().st_size
        if enforce_expected_outputs:
            differences: dict[str, Any] = {}
            if success_output_hash != EXPECTED_SUCCESS_OUTPUT_SHA256:
                differences["success_sha256"] = {
                    "actual": success_output_hash,
                    "expected": EXPECTED_SUCCESS_OUTPUT_SHA256,
                }
            if failed_output_hash != EXPECTED_FAILED_OUTPUT_SHA256:
                differences["failed_sha256"] = {
                    "actual": failed_output_hash,
                    "expected": EXPECTED_FAILED_OUTPUT_SHA256,
                }
            if success_output_bytes != EXPECTED_SUCCESS_OUTPUT_BYTES:
                differences["success_bytes"] = {
                    "actual": success_output_bytes,
                    "expected": EXPECTED_SUCCESS_OUTPUT_BYTES,
                }
            if failed_output_bytes != EXPECTED_FAILED_OUTPUT_BYTES:
                differences["failed_bytes"] = {
                    "actual": failed_output_bytes,
                    "expected": EXPECTED_FAILED_OUTPUT_BYTES,
                }
            if differences:
                raise DatasetPreparationError(
                    f"generated output differs from frozen expectation: {differences}"
                )

        report["outputs"] = {
            "success": {
                "path": str(success_target),
                "sha256": success_output_hash,
                "bytes": success_output_bytes,
            },
            "failed": {
                "path": str(failed_target),
                "sha256": failed_output_hash,
                "bytes": failed_output_bytes,
            },
        }
        _write_json(staging_json_report, report)
        with staging_markdown_report.open(
            "w", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(_markdown_report(report))

        output_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        for target in data_targets:
            if target.exists():
                backup = staging_root / f"backup_{target.name}"
                os.replace(target, backup)
                backups[target] = backup

        publications = (
            (staging_success, success_target),
            (staging_failed, failed_target),
            (staging_json_report, json_report_target),
            (staging_markdown_report, markdown_report_target),
        )
        for source, target in publications:
            os.replace(source, target)
            published.append(target)

        for backup in backups.values():
            backup.unlink()
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        for target, backup in backups.items():
            if backup.exists():
                os.replace(backup, target)
        raise
    finally:
        for path in (
            staging_success,
            staging_failed,
            staging_json_report,
            staging_markdown_report,
        ):
            path.unlink(missing_ok=True)
        for directory in (staging_data, staging_reports, staging_root):
            try:
                directory.rmdir()
            except OSError:
                pass
    return report


def prepare_submission_dataset(
    *,
    success_input: Path,
    failed_input: Path,
    raw_input: Path,
    output_dir: Path,
    report_dir: Path,
    enforce_v4_expectations: bool = True,
    replace_existing: bool = False,
) -> dict[str, Any]:
    success_hash = _sha256(success_input)
    failed_hash = _sha256(failed_input)
    raw_hash = _sha256(raw_input)
    source_success = _load_jsonl(success_input)
    source_failed = _load_jsonl(failed_input)
    raw_rows = _load_jsonl(raw_input)
    raw_index = build_raw_input_index(raw_rows)
    source_nested_forbidden = _nested_forbidden_counts(source_success)

    clean_success, success_removed = clean_success_records(source_success)
    clean_failed, supplements = clean_failed_records(source_failed, raw_index)
    identity_checks = _validate_unique_identity(clean_success, clean_failed)
    nested_answer_checks = _validate_clean_answer_payloads(clean_success)
    failure_detail_checks = _validate_complete_failure_details(clean_failed)
    _validate_content_invariance(
        source_success, clean_success, source_failed, clean_failed, raw_index
    )
    failure_info_normalization_counts, failure_info_normalized_records = (
        _failure_info_normalization_report(source_failed, clean_failed)
    )

    if enforce_v4_expectations:
        _validate_known_v4(
            clean_success, clean_failed, success_hash, failed_hash
        )

    answer_counts = _answer_counts(clean_success)
    failure_counts = dict(Counter(row["termination_reason"] for row in clean_failed))
    supplemented_counts = Counter(
        field for item in supplements for field in item["supplemented_fields"]
    )
    failed_answer_fields = sum(
        field in row
        for row in source_failed
        for field in ("final_answer", "oj_candidates", "rp_candidates")
    )

    report: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "success": {"path": str(success_input), "sha256": success_hash},
            "failed": {"path": str(failed_input), "sha256": failed_hash},
            "raw": {"path": str(raw_input), "sha256": raw_hash},
        },
        "record_counts": {
            "success": len(clean_success),
            "failed": len(clean_failed),
            "total": len(clean_success) + len(clean_failed),
        },
        "removed_field_counts": {
            "success_recovery_source": len(success_removed),
            "failed_recovery_source": sum(
                "recovery_source" in row for row in source_failed
            ),
            "failed_answer_fields": failed_answer_fields,
        },
        "supplemented_field_counts": {
            "input_generator_code": supplemented_counts["input_generator_code"],
            "branch_coverage": supplemented_counts["branch_coverage"],
        },
        "nested_removed_field_counts": source_nested_forbidden,
        "nested_answer_checks": nested_answer_checks,
        "success_removed_records": success_removed,
        "failed_supplemented_records": supplements,
        "failure_info_normalization_counts": failure_info_normalization_counts,
        "failure_info_normalized_records": failure_info_normalized_records,
        "answer_counts": answer_counts,
        "termination_reason_distribution": failure_counts,
        "failure_detail_checks": failure_detail_checks,
        "failure_last_error_detail_null": sum(
            row["failure_info"]["last_error_detail"] is None for row in clean_failed
        ),
        "identity_checks": identity_checks,
        "schema": {
            "success_fields": list(SUCCESS_FIELDS),
            "failed_fields": list(FAILED_FIELDS),
        },
        "acceptance": {
            "strict_success_schema": all(
                tuple(row) == SUCCESS_FIELDS for row in clean_success
            ),
            "strict_failed_schema": all(
                tuple(row) == FAILED_FIELDS for row in clean_failed
            ),
            "content_invariance": True,
            "strict_nested_answer_schema": True,
            "complete_failure_details": True,
            "raw_input_unique_match": True,
            "record_order_preserved": True,
        },
    }
    return _publish_outputs(
        clean_success,
        clean_failed,
        report,
        output_dir,
        report_dir,
        replace_existing=replace_existing,
        enforce_expected_outputs=enforce_v4_expectations,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare schema-clean JSpecForge submission JSONL files."
    )
    parser.add_argument("--success-input", type=Path, default=DEFAULT_SUCCESS_INPUT)
    parser.add_argument("--failed-input", type=Path, default=DEFAULT_FAILED_INPUT)
    parser.add_argument("--raw-input", type=Path, default=DEFAULT_RAW_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument(
        "--skip-v4-expectations",
        action="store_true",
        help="skip frozen V4 hashes and expected aggregate counts",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace the two existing submission JSONL files after validation",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = prepare_submission_dataset(
        success_input=args.success_input,
        failed_input=args.failed_input,
        raw_input=args.raw_input,
        output_dir=args.output_dir,
        report_dir=args.report_dir,
        enforce_v4_expectations=not args.skip_v4_expectations,
        replace_existing=args.replace_existing,
    )
    counts = report["record_counts"]
    print(
        f"submission dataset prepared: success={counts['success']} "
        f"failed={counts['failed']} total={counts['total']}"
    )
    print(report["outputs"]["success"]["path"])
    print(report["outputs"]["failed"]["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
