"""MT 主编排器：读 jsonl → Major 生成变异体 → 按路径分流验证 → 算分 → 回写输出。

核心流程（design §5）：

1. 读取 ``success.jsonl``
2. 对每条记录：
   - path=openjml → OJ 路径 MT（design §3.1）
   - path=rp → RP 路径 MT（design §3.2），对每个 is_pass=true 的 pbt_attempt 分别跑
3. 回填 ``final_answer.mutation_score`` / ``mt_detail`` / ``pbt_attempts[].mutation_score``
4. path=rp 时选最高分候选更新 ``final_answer`` 的 full_java_with_jml / a_round /
   selected_candidate_index / mutation_score / mt_detail
5. 输出 ``success_with_mt.jsonl``
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.mt.mutant_loader import (
    MajorMutant,
    extract_class_name,
    load_mutants,
)
from jspecforge.mt.oj_path_runner import OJPathRunner
from jspecforge.mt.rp_path_runner import RPPathRunner
from jspecforge.mt.score_calculator import (
    INVALID,
    KILLED,
    SURVIVED,
    MutantVerdict,
    build_final_answer_update,
    build_mt_detail,
    compute_mutation_score,
)
from jspecforge.tools.major_runner import MajorRunner
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("mt.orchestrator")


# ---------------------------------------------------------------------------
# jsonl I/O
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读取 jsonl 文件，每行解析为一个 JSON 对象。

    Args:
        path: jsonl 文件路径。

    Returns:
        JSON 对象列表。

    Raises:
        FileNotFoundError: 文件不存在。
    """
    if not path.is_file():
        raise FileNotFoundError(f"jsonl 文件不存在: {path}")

    records: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                _logger.warning("第 %d 行 JSON 解析失败: %s", line_no, exc)
    _logger.info("读取 %d 条记录: %s", len(records), path)
    return records


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    """写入 jsonl 文件。

    Args:
        path: 输出路径。
        records: JSON 对象列表。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    _logger.info("写入 %d 条记录: %s", len(records), path)


# ---------------------------------------------------------------------------
# 单条记录处理
# ---------------------------------------------------------------------------


def _get_method_code(record: Dict[str, Any]) -> str:
    """从记录中提取方法签名文本（用于 TestDriver 构建）。

    优先使用 method_code（方法体），其次 full_code。
    """
    return record.get("method_code") or record.get("full_code") or ""


def _get_method_signature_text(record: Dict[str, Any]) -> str:
    """提取方法签名文本。

    method_code 本身就是方法声明+方法体，test_driver_builder.parse_method_signature
    能从中提取签名。
    """
    return _get_method_code(record)


def _get_pass_candidates(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """获取 pbt_attempts 中 is_pass=true 的候选列表。"""
    pbt_attempts = record.get("pbt_attempts") or []
    return [p for p in pbt_attempts if p.get("is_pass")]


class MTOrchestrator:
    """MT 主编排器。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._major = MajorRunner(config)
        self._oj_runner = OJPathRunner(config)
        self._rp_runner = RPPathRunner(config)

    def process_record(
        self,
        record: Dict[str, Any],
        *,
        work_dir: Path,
        max_mutants: Optional[int] = None,
        workers: Optional[int] = None,
    ) -> Dict[str, Any]:
        """对单条 success.jsonl 记录执行 MT 后处理。

        Args:
            record: success.jsonl 中的一条记录。
            work_dir: MT 工作临时目录。
            max_mutants: 覆盖 config 中的 max_mutants_per_method。
            workers: 覆盖 config 中的 mt.workers。

        Returns:
            回填了 mutation_score / mt_detail 的记录（原地修改并返回）。
        """
        path = record.get("path")
        final_answer = record.get("final_answer")
        if not final_answer or path not in ("openjml", "rp"):
            _logger.warning("记录 id=%s path=%s 跳过 MT", record.get("id"), path)
            return record

        record_id = record.get("id", "?")
        full_java = final_answer.get("full_java_with_jml", "")
        if not full_java:
            _logger.warning("记录 id=%s 缺少 full_java_with_jml, 跳过", record_id)
            return record

        class_name = extract_class_name(full_java)
        actual_max = max_mutants if max_mutants is not None else self._config.mt.max_mutants_per_method

        # Step 1: 写变异体源文件 + Major 生成变异体
        task_work = work_dir / f"task_{record_id}"
        java_src_dir = task_work / "java_src"
        java_src_dir.mkdir(parents=True, exist_ok=True)

        source_file = java_src_dir / f"{class_name}.java"
        with source_file.open("w", encoding="utf-8", newline="\n") as f:
            f.write(full_java)

        major_output = task_work / "major_out"
        # Major plugin 不会自动创建父目录,必须先确保 major_out/ 存在
        # （否则 mutants.log 无法写入,exit_code=4 + FileNotFoundException）
        major_output.mkdir(parents=True, exist_ok=True)
        major_result = self._major.run(
            source_files=[str(source_file)],
            output_dir=str(major_output),
        )

        if not major_result.success:
            _logger.warning(
                "记录 id=%s Major 生成变异体失败 (exit=%d), mutation_score=-1",
                record_id,
                major_result.tool_result.exit_code,
            )
            final_answer["mutation_score"] = -1.0
            final_answer["mt_detail"] = build_mt_detail(
                mutants_used=0,
                killed=0,
                survived=0,
                invalid=0,
                test_path=path,
                total_elapsed_seconds=major_result.tool_result.elapsed,
                mutants_generated=0,
            )
            final_answer["mt_valid_input_ratio"] = None
            return record

        # Step 2: 加载变异体
        mutants, budget = load_mutants(major_output, class_name, actual_max)
        if not mutants:
            _logger.warning("记录 id=%s Major 输出 0 个变异体, mutation_score=1.0 (无可变异点)", record_id)
            final_answer["mutation_score"] = 1.0
            final_answer["mt_detail"] = build_mt_detail(
                mutants_used=0,
                killed=0,
                survived=0,
                invalid=0,
                test_path=path,
                total_elapsed_seconds=0.0,
                mutants_generated=0,
            )
            final_answer["mt_valid_input_ratio"] = None
            return record

        # Step 3: 按路径分流验证
        if path == "openjml":
            self._process_oj_path(
                record, mutants, budget, task_work, workers=workers,
            )
        else:  # path == "rp"
            self._process_rp_path(
                record, mutants, budget, task_work, workers=workers,
            )

        return record

    def _process_oj_path(
        self,
        record: Dict[str, Any],
        mutants: List[MajorMutant],
        budget: Dict[str, int],
        task_work: Path,
        *,
        workers: Optional[int] = None,
    ) -> None:
        """OJ 路径 MT 处理。"""
        start = time.perf_counter()
        result = self._oj_runner.verify_all(mutants, workers=workers)
        total_elapsed = time.perf_counter() - start

        score, killed, survived, invalid = compute_mutation_score(result.verdicts)

        final_answer = record["final_answer"]
        final_answer["mutation_score"] = score
        final_answer["mt_detail"] = build_mt_detail(
            mutants_used=budget["mutants_used"],
            killed=killed,
            survived=survived,
            invalid=invalid,
            test_path="openjml",
            total_elapsed_seconds=total_elapsed,
            mutants_generated=budget["mutants_total"],
        )
        # OJ 路径 MT 不跑 TestDriver, 没有 valid_input 信号
        final_answer["mt_valid_input_ratio"] = None

        _logger.info(
            "OJ MT id=%s: score=%.4f, killed=%d, survived=%d, invalid=%d, elapsed=%.2fs",
            record.get("id"),
            score,
            killed,
            survived,
            invalid,
            total_elapsed,
        )

    def _process_rp_path(
        self,
        record: Dict[str, Any],
        mutants: List[MajorMutant],
        budget: Dict[str, int],
        task_work: Path,
        *,
        workers: Optional[int] = None,
    ) -> None:
        """RP 路径 MT 处理：对每个 is_pass=true 候选分别跑 MT，选最高分。"""
        candidates = _get_pass_candidates(record)
        if not candidates:
            _logger.warning("记录 id=%s path=rp 但无 is_pass=true 候选", record.get("id"))
            final_answer = record["final_answer"]
            final_answer["mutation_score"] = -1.0
            final_answer["mt_detail"] = build_mt_detail(
                mutants_used=budget["mutants_used"],
                killed=0,
                survived=0,
                invalid=0,
                test_path="rp",
                total_elapsed_seconds=0.0,
                mutants_generated=budget["mutants_total"],
            )
            final_answer["mt_valid_input_ratio"] = None
            return

        method_sig = _get_method_signature_text(record)
        target_class_name = extract_class_name(
            record["final_answer"].get("full_java_with_jml", "")
        )

        # Phase 4 (2026-05-13): 输入生成器已上移到 task 级。优先用 record 顶层
        # input_generator_code; pbt_attempts[].input_generator_code 已成为同
        # 一字符串的副本(向后兼容老导出文件,缺失时再退回 candidate 字段)。
        task_level_ig = record.get("input_generator_code")

        best_score = -2.0  # 任何合法 score >= -1.0 都会覆盖
        best_index = 0
        best_detail = None
        best_update = None
        best_valid_input_ratio: Optional[float] = None
        total_elapsed = 0.0

        for cand_idx, candidate in enumerate(candidates):
            ig_code = task_level_ig or candidate.get("input_generator_code") or ""
            cand_work = task_work / f"candidate_{cand_idx}"

            start = time.perf_counter()
            rp_result = self._rp_runner.verify_all(
                mutants,
                target_class_name=target_class_name,
                method_signature_text=method_sig,
                input_generator_code=ig_code,
                work_dir=cand_work,
                candidate_index=cand_idx,
                workers=workers,
            )
            cand_elapsed = time.perf_counter() - start
            total_elapsed += cand_elapsed

            score, killed, survived, invalid = compute_mutation_score(rp_result.verdicts)

            # 回填单个候选的 mutation_score
            candidate["mutation_score"] = score
            # Phase 4 (2026-05-13): per-candidate mt_valid_input_ratio 也落到
            # pbt_attempts[] 上, 便于人工对照 PBT 主流水线的 valid_input_ratio
            candidate["mt_valid_input_ratio"] = rp_result.valid_input_ratio

            cand_detail = build_mt_detail(
                mutants_used=budget["mutants_used"],
                killed=killed,
                survived=survived,
                invalid=invalid,
                test_path="rp",
                total_elapsed_seconds=cand_elapsed,
                mutants_generated=budget["mutants_total"],
            )

            _logger.info(
                "RP MT id=%s candidate[%d]: score=%.4f, killed=%d, survived=%d, valid_ratio=%s",
                record.get("id"),
                cand_idx,
                score,
                killed,
                survived,
                f"{rp_result.valid_input_ratio:.4f}" if rp_result.valid_input_ratio is not None else "n/a",
            )

            if score > best_score:
                best_score = score
                best_index = cand_idx
                best_detail = cand_detail
                best_valid_input_ratio = rp_result.valid_input_ratio
                best_update = build_final_answer_update(
                    mutation_score=score,
                    mt_detail=cand_detail,
                    full_java_with_jml=candidate.get("full_java_with_jml"),
                    a_round=candidate.get("a_round"),
                    selected_candidate_index=cand_idx,
                )

        # 用最高分候选更新 final_answer
        final_answer = record["final_answer"]
        if best_update:
            final_answer.update(best_update)
            final_answer["mt_valid_input_ratio"] = best_valid_input_ratio
        else:
            final_answer["mutation_score"] = -1.0
            final_answer["mt_detail"] = build_mt_detail(
                mutants_used=budget["mutants_used"],
                killed=0,
                survived=0,
                invalid=0,
                test_path="rp",
                total_elapsed_seconds=total_elapsed,
                mutants_generated=budget["mutants_total"],
            )
            final_answer["mt_valid_input_ratio"] = None

        _logger.info(
            "RP MT id=%s: best_score=%.4f (candidate[%d]), total_elapsed=%.2fs",
            record.get("id"),
            best_score,
            best_index,
            total_elapsed,
        )

    def run(
        self,
        input_path: Path,
        output_path: Path,
        *,
        work_dir: Optional[Path] = None,
        max_mutants: Optional[int] = None,
        workers: Optional[int] = None,
    ) -> int:
        """执行完整 MT 后处理流水线。

        Args:
            input_path: 输入 success.jsonl 路径。
            output_path: 输出 success_with_mt.jsonl 路径。
            work_dir: 工作临时目录，缺省取 ``{config.paths.tmp_dir}/mt_work``。
            max_mutants: 覆盖 config 中的每方法最大变异体数。
            workers: 覆盖 config 中的并发线程数。

        Returns:
            处理的记录总数。
        """
        if work_dir is not None:
            actual_work = work_dir
        else:
            actual_work = Path(self._config.paths.tmp_dir) / "mt_work"
        actual_work.mkdir(parents=True, exist_ok=True)

        records = read_jsonl(input_path)
        total = len(records)
        _logger.info("MT 开始: %d 条记录, work_dir=%s", total, actual_work)

        overall_start = time.perf_counter()
        for i, record in enumerate(records, 1):
            _logger.info("处理 %d/%d: id=%s, path=%s", i, total, record.get("id"), record.get("path"))
            try:
                self.process_record(
                    record,
                    work_dir=actual_work,
                    max_mutants=max_mutants,
                    workers=workers,
                )
            except Exception as exc:
                _logger.error(
                    "记录 id=%s MT 处理异常, 跳过: %s", record.get("id"), exc
                )
                # 异常记录设 mutation_score = -1, mt_valid_input_ratio = None
                fa = record.get("final_answer")
                if fa:
                    fa["mutation_score"] = -1.0
                    fa.setdefault("mt_valid_input_ratio", None)

        overall_elapsed = time.perf_counter() - overall_start
        _logger.info(
            "MT 完成: %d 条记录, 总耗时=%.2fs", total, overall_elapsed
        )

        write_jsonl(output_path, records)
        return total
