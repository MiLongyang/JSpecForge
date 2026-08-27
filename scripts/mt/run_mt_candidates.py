#!/usr/bin/env python
"""Run MT for candidate-level rerun JSONL records.

This script does not read or write MySQL. It consumes the JSONL produced by
``extract_mt_rerun_candidates.py`` and writes one result row per input row.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Mapping

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (_ROOT, _SRC):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from jspecforge.config.loader import load_config  # noqa: E402
from jspecforge.config.settings import AppConfig  # noqa: E402
from jspecforge.mt.mutant_loader import (  # noqa: E402
    MajorMutant,
    extract_class_name,
    load_mutants,
)
from jspecforge.mt.oj_path_runner import OJPathRunner  # noqa: E402
from jspecforge.mt.rp_path_runner import RPPathRunner  # noqa: E402
from jspecforge.mt.score_calculator import (  # noqa: E402
    SURVIVED,
    MutantVerdict,
    compute_mutation_score,
)
from jspecforge.tools.major_runner import MajorRunner  # noqa: E402


def _identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_uid": candidate.get("candidate_uid"),
        "path": candidate.get("path"),
        "task_id": candidate.get("task_id"),
        "attempt_id": candidate.get("attempt_id"),
        "a_round": candidate.get("a_round"),
        "ig_round": candidate.get("ig_round"),
    }


def build_failure_result(
    candidate: Mapping[str, Any],
    error_type: str,
    error_message: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    result = _identity(candidate)
    result.update(
        {
            "status": "failed",
            "mutation_score": -1.0,
            "mt_killed": None,
            "mt_survived": None,
            "mt_total": None,
            "mt_feedback_text": None,
            "mt_valid_input_ratio": None,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "error_type": error_type,
            "error_message": error_message,
        }
    )
    return result


def build_success_result(
    candidate: Mapping[str, Any],
    *,
    mutation_score: float,
    killed: int,
    survived: int,
    invalid: int,
    feedback_text: str,
    elapsed_seconds: float,
    valid_input_ratio: float | None = None,
) -> dict[str, Any]:
    result = _identity(candidate)
    result.update(
        {
            "status": "ok",
            "mutation_score": mutation_score,
            "mt_killed": killed,
            "mt_survived": survived,
            "mt_total": killed + survived,
            "mt_invalid": invalid,
            "mt_feedback_text": feedback_text,
            "mt_valid_input_ratio": valid_input_ratio,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "error_type": None,
            "error_message": None,
        }
    )
    return result


def build_survived_summary(
    mutants: Iterable[MajorMutant],
    verdicts: Iterable[MutantVerdict],
    *,
    max_survived: int,
) -> str:
    mutant_map = {m.major_id: m for m in mutants}
    lines: list[str] = []
    for verdict in verdicts:
        if verdict.verdict != SURVIVED:
            continue
        mutant = mutant_map.get(verdict.major_id)
        if mutant is None:
            continue
        obj = {
            "id": mutant.major_id,
            "operator": f"{mutant.operator} ({mutant.from_code}->{mutant.to_code})",
            "line": mutant.line,
            "original": mutant.from_code,
            "mutant": mutant.to_code,
        }
        lines.append(json.dumps(obj, ensure_ascii=False))
        if len(lines) >= max_survived:
            break
    return "\n".join(lines)


def _safe_dir_name(candidate_uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_uid)


def normalize_work_root(path: Path) -> Path:
    """Return an absolute work directory path for tool calls.

    OpenJML on Windows is invoked through WSL. Relative Windows paths are not
    convertible to ``/mnt/<drive>/...`` and can be mangled by the shell/tool
    boundary, so all generated Java paths must be rooted at an absolute path.
    """
    return path.resolve()


def remove_external_jml_annotation_imports(source: str) -> str:
    """Remove imports for annotation packages unavailable to Major.

    JML contracts in these candidates are comments, so the import is not needed
    for mutation generation. Keeping it makes javac/Major fail when the external
    annotation package is absent on the cloud runner.
    """
    return re.sub(
        r"(?m)^\s*import\s+org\.jmlspecs\.annotation\.\*\s*;\s*(?:\r?\n)?",
        "",
        source,
    )


def normalize_java_source_for_major(full_java: str) -> str:
    """Restore critical line breaks before writing Java source for Major.

    Some old candidates store the generated wrapper as one physical line, for
    example ``import ...; // wrapper comment public class Solution { ... }``.
    In Java, ``//`` comments consume the rest of the physical line, so the class
    declaration or JML/method body can be commented out before Major sees it.
    Multi-line sources are left unchanged after unsupported imports are removed.
    """
    full_java = remove_external_jml_annotation_imports(full_java)
    if "\n" in full_java:
        return full_java

    source = full_java.replace("\r", "")
    if not source.strip():
        return source

    modifier_group = r"(?:(?:public|private|protected|final|abstract|static|strictfp)\s+)*"
    class_decl = modifier_group + r"class\s+[A-Za-z_$][\w$]*\b"
    member_start = (
        r"(?:/\*@|//@|public\b|private\b|protected\b|static\b|final\b|abstract\b|"
        r"strictfp\b|void\b|boolean\b|byte\b|short\b|int\b|long\b|char\b|float\b|"
        r"double\b|String\b|[A-Za-z_$][\w$<>\[\], ?]*\s+[A-Za-z_$][\w$]*\s*\()"
    )
    after_line_comment_start = (
        r"(?:/\*@|//@|public\b|private\b|protected\b|static\b|final\b|abstract\b|"
        r"strictfp\b)"
    )

    source = re.sub(r";\s+(?=import\s+)", ";\n", source)
    source = re.sub(r"\s+(?=//\s*===)", "\n", source)
    source = re.sub(r"(//\s*===\s*[^=\n]+?\s*===)\s+(?=//\s*===)", r"\1\n", source)
    source = re.sub(r"(//\s*===\s*[^=\n]+?\s*===)\s+(?=" + class_decl + r")", r"\1\n", source)
    source = re.sub(r"\s+(?=" + class_decl + r"\s*\{)", "\n", source, count=1)
    source = re.sub(r"(\{\s+)(?=" + member_start + r")", "{\n", source, count=1)

    # A compressed method wrapper can contain ``// comment /*@ ...`` or
    # ``// comment public int ...``. Put the following declaration back on a new
    # physical line so the line comment does not consume it.
    source = re.sub(r"(//[^\n]*?)\s+(?=" + after_line_comment_start + r")", r"\1\n", source)
    source = re.sub(r"(\*/)\s+(?=" + member_start + r")", r"\1\n", source)

    if not source.endswith("\n"):
        source += "\n"
    return source


def resolve_parallelism(
    *,
    jobs: int | None,
    workers_per_candidate: int | None,
    legacy_workers: int | None,
    config_workers: int,
) -> tuple[int, int]:
    """Resolve candidate-level and mutant-level parallelism.

    ``jobs`` controls how many candidates run at the same time.
    ``workers_per_candidate`` controls how many mutant verifications are run
    inside one candidate. ``legacy_workers`` is the old ``--workers`` option and
    remains a compatibility alias for ``workers_per_candidate``.
    """
    actual_jobs = jobs if jobs is not None else 1
    actual_workers = (
        workers_per_candidate
        if workers_per_candidate is not None
        else legacy_workers
        if legacy_workers is not None
        else config_workers
    )
    if actual_jobs < 1:
        raise ValueError("--jobs must be >= 1")
    if actual_workers < 1:
        raise ValueError("--workers-per-candidate must be >= 1")
    return actual_jobs, actual_workers


def apply_timeout_overrides(
    config: AppConfig,
    *,
    timeout_per_mutant: int | None,
) -> AppConfig:
    """Apply timeout overrides to all tool configs used by candidate MT."""
    if timeout_per_mutant is None:
        return config
    if timeout_per_mutant < 1:
        raise ValueError("--timeout-per-mutant must be >= 1")
    return dataclasses.replace(
        config,
        openjml=dataclasses.replace(
            config.openjml,
            timeout=timeout_per_mutant,
            rac_timeout=timeout_per_mutant,
        ),
        java=dataclasses.replace(
            config.java,
            javac_timeout=timeout_per_mutant,
            test_driver_timeout=timeout_per_mutant,
        ),
    )


def process_candidate(
    candidate: Mapping[str, Any],
    *,
    config: AppConfig,
    work_root: Path,
    max_mutants: int | None,
    workers_per_candidate: int | None,
    major_timeout: int | None = None,
) -> dict[str, Any]:
    start = time.perf_counter()
    candidate_uid = str(candidate.get("candidate_uid") or "unknown")
    path = candidate.get("path")
    if path not in ("oj", "rp"):
        return build_failure_result(candidate, "bad_input", f"unknown path={path!r}", 0.0)

    full_java = candidate.get("full_java_with_jml") or ""
    if not full_java:
        return build_failure_result(candidate, "bad_input", "missing full_java_with_jml", 0.0)

    try:
        class_name = extract_class_name(full_java)
    except Exception as exc:  # noqa: BLE001
        return build_failure_result(candidate, "class_name_failed", str(exc), 0.0)

    work_dir = work_root / _safe_dir_name(candidate_uid)
    java_src_dir = work_dir / "java_src"
    major_output = work_dir / "major_out"
    java_src_dir.mkdir(parents=True, exist_ok=True)
    major_output.mkdir(parents=True, exist_ok=True)

    source_file = java_src_dir / f"{class_name}.java"
    with source_file.open("w", encoding="utf-8", newline="\n") as f:
        f.write(normalize_java_source_for_major(full_java))

    try:
        major = MajorRunner(config)
        major_result = major.run(
            [str(source_file)],
            str(major_output),
            timeout=major_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        return build_failure_result(candidate, "major_exception", str(exc), elapsed)

    if not major_result.success:
        elapsed = time.perf_counter() - start
        message = (
            f"Major failed exit={major_result.tool_result.exit_code}; "
            f"stderr={major_result.tool_result.stderr[:1000]}"
        )
        return build_failure_result(candidate, "major_failed", message, elapsed)

    actual_max = max_mutants if max_mutants is not None else config.mt.max_mutants_per_method
    try:
        mutants, _budget = load_mutants(major_output, class_name, actual_max)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        return build_failure_result(candidate, "load_mutants_failed", str(exc), elapsed)

    if not mutants:
        elapsed = time.perf_counter() - start
        return build_success_result(
            candidate,
            mutation_score=1.0,
            killed=0,
            survived=0,
            invalid=0,
            feedback_text="Major 输出 0 个变异体，按无可变异点处理为 1.0",
            elapsed_seconds=elapsed,
        )

    try:
        if path == "oj":
            oj_result = OJPathRunner(config).verify_all(
                mutants,
                workers=workers_per_candidate,
            )
            verdicts = oj_result.verdicts
            valid_input_ratio = None
        else:
            method_code = candidate.get("method_code") or ""
            input_generator_code = candidate.get("input_generator_code") or ""
            if not method_code or not input_generator_code:
                elapsed = time.perf_counter() - start
                return build_failure_result(
                    candidate,
                    "bad_input",
                    "missing method_code or input_generator_code for RP",
                    elapsed,
                )
            rp_result = RPPathRunner(config).verify_all(
                mutants,
                target_class_name=class_name,
                method_signature_text=method_code,
                input_generator_code=input_generator_code,
                work_dir=work_dir / "rp_verify",
                candidate_index=int(candidate.get("ig_round") or 0),
                workers=workers_per_candidate,
            )
            verdicts = rp_result.verdicts
            valid_input_ratio = rp_result.valid_input_ratio
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - start
        return build_failure_result(candidate, "verify_exception", str(exc), elapsed)

    score, killed, survived, invalid = compute_mutation_score(verdicts)
    feedback_text = build_survived_summary(
        mutants,
        verdicts,
        max_survived=config.mt.feedback_max_survived,
    )
    elapsed = time.perf_counter() - start
    return build_success_result(
        candidate,
        mutation_score=score,
        killed=killed,
        survived=survived,
        invalid=invalid,
        feedback_text=feedback_text,
        elapsed_seconds=elapsed,
        valid_input_ratio=valid_input_ratio,
    )


def read_jsonl(path: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(json.loads(line))
            if limit is not None and len(rows) >= limit:
                break
    return rows


def read_completed_candidate_uids(path: Path) -> set[str]:
    """Read candidate_uid values already present in an output JSONL file.

    A cloud VM may be stopped while a line is being written. In that case the
    partial line is ignored and the corresponding candidate will be rerun.
    """
    completed: set[str] = set()
    if not path.exists():
        return completed
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                print(f"[resume] ignore malformed output line {line_no}: {path}")
                continue
            uid = row.get("candidate_uid")
            if uid:
                completed.add(str(uid))
    return completed


def filter_pending_candidates(
    candidates: list[dict[str, Any]],
    completed_uids: set[str],
) -> list[dict[str, Any]]:
    """Return candidates whose candidate_uid is not already completed."""
    if not completed_uids:
        return candidates
    return [
        candidate
        for candidate in candidates
        if str(candidate.get("candidate_uid")) not in completed_uids
    ]


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="候选级 MT 重跑脚本")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--max-mutants", type=int, default=None)
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="候选级并发数：同时处理多少条候选。默认 1。",
    )
    parser.add_argument(
        "--workers-per-candidate",
        type=int,
        default=None,
        help="单条候选内部的变异体验证并发数。默认读取 config.mt.workers。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="兼容旧参数，等价于 --workers-per-candidate。",
    )
    parser.add_argument(
        "--major-timeout",
        type=int,
        default=None,
        help="Major 生成变异体超时秒数。默认使用 MajorRunner 默认值。",
    )
    parser.add_argument(
        "--timeout-per-mutant",
        type=int,
        default=None,
        help="单个变异体验证相关工具超时秒数，覆盖 OJ/RAC/javac/TestDriver。",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="删除已存在的 output 后重新写入",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="断点续跑：读取已存在 output，跳过已完成 candidate_uid，并继续追加写入",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.overwrite and args.resume:
        raise SystemExit("--overwrite 和 --resume 不能同时使用")
    config = load_config(args.config)
    config = apply_timeout_overrides(
        config,
        timeout_per_mutant=args.timeout_per_mutant,
    )
    jobs, workers_per_candidate = resolve_parallelism(
        jobs=args.jobs,
        workers_per_candidate=args.workers_per_candidate,
        legacy_workers=args.workers,
        config_workers=config.mt.workers,
    )
    input_path = Path(args.input)
    output_path = Path(args.output)
    if output_path.exists() and args.overwrite:
        output_path.unlink()
    elif output_path.exists() and not args.resume:
        raise SystemExit(f"输出文件已存在，请使用 --overwrite: {output_path}")

    work_root = normalize_work_root(
        Path(args.work_dir)
        if args.work_dir
        else Path(config.paths.tmp_dir) / "mt_candidate_rerun"
    )
    work_root.mkdir(parents=True, exist_ok=True)

    candidates = read_jsonl(input_path, limit=args.limit)
    completed_uids = read_completed_candidate_uids(output_path) if args.resume else set()
    if completed_uids:
        before_count = len(candidates)
        candidates = filter_pending_candidates(candidates, completed_uids)
        print(
            f"[resume] output={output_path} completed={len(completed_uids)} "
            f"pending={len(candidates)} skipped={before_count - len(candidates)}"
        )
    elif args.resume:
        print(f"[resume] output={output_path} completed=0 pending={len(candidates)}")

    def _run_one(index_and_candidate: tuple[int, Mapping[str, Any]]) -> dict[str, Any]:
        idx, candidate = index_and_candidate
        print(f"[{idx}/{len(candidates)}] rerun {candidate.get('candidate_uid')}")
        result = process_candidate(
            candidate,
            config=config,
            work_root=work_root,
            max_mutants=args.max_mutants,
            workers_per_candidate=workers_per_candidate,
            major_timeout=args.major_timeout,
        )
        print(
            f"  -> {result['status']} score={result['mutation_score']} "
            f"error={result['error_type']}"
        )
        return result

    indexed_candidates = list(enumerate(candidates, 1))
    if jobs == 1:
        for item in indexed_candidates:
            append_jsonl(output_path, _run_one(item))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            future_map = {executor.submit(_run_one, item): item for item in indexed_candidates}
            for future in as_completed(future_map):
                append_jsonl(output_path, future.result())


if __name__ == "__main__":
    main()
