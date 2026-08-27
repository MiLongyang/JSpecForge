"""RP 路径 MT 验证：对每个变异体重跑 RAC 编译 + TestDriver 动态测试。

验证逻辑（design §3.2）：

1. RAC 编译变异体 → 编译失败 = killed
2. 编译成功 → 用 **MT 专用 TestDriver** + 新随机种子运行动态测试
3. 判定（统一委派给 :func:`mt.score_calculator.verdict_from_pbt_stats`）：

   - ``post_fail_count > 0`` → killed（规约抓到 postcondition 违反）
   - ``post_fail_count == 0 AND other_exception_count == total_runs`` → invalid
     （100% NPE 类异常，方法在 JVM 层崩溃，规约来不及检查）
   - 其他（含部分 NPE 部分通过、全部通过）→ survived

4. RAC 编译失败但为基础设施错误 → invalid（不计入分母）

**为什么用 MT 专用 TestDriver 模板**（``templates/MTTestDriverTemplate.java``）：

主流水线 ``TestDriverTemplate.java`` 把 NPE 等非 JML 异常算 ``valid_inputs``（吞掉），
这是为了主流水线判定"输入生成器是否合理"的需求——避免 generator bug 误算为
规约失败。但 MT 评判的是"JML 规约对变异体的识别力"，当变异让方法在 JVM 层
崩溃时，JML postcondition 来不及检查，这种变异不能用来评判规约强弱，应标
invalid 不计入分母。所以 MT 用独立模板，区分 ``other_exception_count``。
"""

from __future__ import annotations

import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from jspecforge.config.settings import AppConfig
from jspecforge.core.shutdown import get_shutdown_event
from jspecforge.mt.mutant_loader import MajorMutant
from jspecforge.mt.score_calculator import (
    INVALID,
    KILLED,
    MutantVerdict,
    SURVIVED,
    verdict_from_pbt_stats,
)
from jspecforge.tools.pbt_runner import PBTRunner
from jspecforge.tools.rac_runner import RACRunner
# 复用主流水线的纯字符串处理函数（与模板无关，无副作用）：
#   - parse_method_signature: 从 method_code 提取方法名/返回类型/参数
#   - build_method_call_code: 拼装 PLACEHOLDER_METHOD_CALL 单行调用
from jspecforge.tools.test_driver_builder import (
    build_method_call_code,
    parse_method_signature,
)
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("mt.rp_path_runner")

# 基础设施错误关键词（同 oj_path_runner，参考 MT_Demo）
_INFRA_KEYWORDS: tuple[str, ...] = (
    "e_accessdenied",
    "wsl/service",
    "access is denied",
    "拒绝访问",
)


def _is_infra_error(returncode: int, output: str) -> bool:
    if returncode in {-1, -1 & 0xFFFFFFFF}:
        return True
    lowered = output.lower()
    return any(kw in lowered for kw in _INFRA_KEYWORDS)


def _extract_trailing_json(stdout: str) -> Optional[Dict]:
    """从 stdout 末尾兜底抽取最后一个 ``{...}`` 行作为 JSON。

    场景：RAC 在变异体上常常把诊断信息（"Runtime exception while evaluating
    postconditions"等）写到 stdout（不是 stderr），导致 stdout 不是单行 JSON。
    TestDriver 的 JSON 是最后一行，从尾部往前扫，找第一个 ``{`` 起始的行尝试
    json.loads。

    Args:
        stdout: TestDriver 的完整 stdout。

    Returns:
        解析成功返回 dict；失败（无 ``{`` 行 / 不是 JSON 对象）返回 ``None``。
    """
    import json as _json

    if not stdout:
        return None
    # 从最后一行往前扫，找 ``{`` 起始的行
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = _json.loads(s)
                if isinstance(obj, dict):
                    return obj
            except _json.JSONDecodeError:
                continue
    return None


# MTTestDriverTemplate.java 占位符（同主模板,但模板本身不同）
_PH_CLASS_NAME = "__CLASS_NAME__"
_PH_INPUT_GENERATOR = "PLACEHOLDER_INPUT_GENERATOR"
_PH_METHOD_CALL = "PLACEHOLDER_METHOD_CALL"


@dataclass(frozen=True)
class _MTBuildResult:
    """MT TestDriver 注入结果。"""

    java_path: Path
    class_name: str


def _build_mt_test_driver(
    *,
    mutant_id: int,
    candidate_index: int,
    target_class_name: str,
    method_signature_text: str,
    input_generator_code: str,
    template_path: Path,
    output_dir: Path,
) -> _MTBuildResult:
    """生成 MT 专用 TestDriver .java 文件。

    与 :func:`tools.test_driver_builder.build_test_driver` 的差异：

    - 类名前缀用 ``MTTestDriver_`` (主流水线用 ``TestDriver_``),便于 debug 区分
    - 模板路径必须指向 ``MTTestDriverTemplate.java``（含 other_exception_count 字段）
    - 不依赖 ``task_id`` / ``a_round``——MT 用 mutant_id + candidate_index 命名

    Args:
        mutant_id: Major 变异体 ID。
        candidate_index: 候选索引（path=rp 时区分多个 pbt_pass 候选；path=openjml 时为 0）。
        target_class_name: 被变异的目标 Java 类名（如 ``"Solution"``）。
        method_signature_text: 方法签名文本（用于 parse_method_signature）。
        input_generator_code: 主流水线产出的 generateValidInput 方法代码。
        template_path: ``templates/MTTestDriverTemplate.java`` 路径。
        output_dir: .java 输出目录（自动创建）。

    Returns:
        :class:`_MTBuildResult`。

    Raises:
        FileNotFoundError: 模板文件不存在。
        ValueError: 方法签名无法解析。
    """
    if not template_path.is_file():
        raise FileNotFoundError(
            f"MTTestDriverTemplate.java 不存在: {template_path}"
        )

    sig = parse_method_signature(method_signature_text)
    if sig is None:
        raise ValueError(
            f"无法从方法签名文本中解析: {method_signature_text[:100]!r}"
        )

    method_call_code = build_method_call_code(target_class_name, sig)
    class_name = f"MTTestDriver_{mutant_id:03d}_{candidate_index}"

    template_text = template_path.read_text(encoding="utf-8")
    rendered = (
        template_text
        .replace(_PH_CLASS_NAME, class_name)
        .replace(_PH_INPUT_GENERATOR, input_generator_code.strip())
        .replace(_PH_METHOD_CALL, method_call_code)
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    java_path = output_dir / f"{class_name}.java"
    with java_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(rendered)

    _logger.debug(
        "_build_mt_test_driver: mutant=%d cand=%d → %s",
        mutant_id,
        candidate_index,
        java_path,
    )
    return _MTBuildResult(java_path=java_path, class_name=class_name)


@dataclass(frozen=True)
class RPPathResult:
    """RP 路径 MT 单方法验证结果。

    Attributes:
        verdicts: 各变异体裁决列表。
        total_elapsed_seconds: 总耗时（秒）。
        valid_input_ratio: MT 跑期间 (sum(valid_inputs) / sum(total_runs)) 的
            汇总值; ``None`` 表示无可解析样本 (全部 RAC 编译失败 / TestDriver
            构建失败 / TestDriver 异常退出)。Phase 4 (2026-05-13) 新增,
            写入 final_answer.mt_valid_input_ratio。
    """

    verdicts: List[MutantVerdict]
    total_elapsed_seconds: float
    valid_input_ratio: Optional[float] = None


class RPPathRunner:
    """RP 路径 MT 验证器（串行执行 RAC + TestDriver）。

    RP 路径比 OJ 路径耗时短（MT_Demo 实测 1.7s vs 4.1s / 变异体），
    且 RAC 编译产物不能跨变异体共享，所以串行即可。
    如需并发可在 orchestrator 层按方法级并发。
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._rac = RACRunner(config)
        self._pbt = PBTRunner(config)

    def verify_single_mutant(
        self,
        mutant: MajorMutant,
        *,
        target_class_name: str,
        method_signature_text: str,
        input_generator_code: str,
        work_dir: Path,
        candidate_index: int = 0,
        seed: Optional[int] = None,
    ) -> MutantVerdict:
        """对单个变异体执行 RP 路径 MT 验证。

        流程：
        1. RAC 编译变异体 .java
        2. 编译失败 → killed（非 infra）或 invalid（infra）
        3. 编译成功 → 构建 TestDriver → javac 编译 → java 运行
        4. post_fail_count > 0 → killed，全过 → survived

        Args:
            mutant: 变异体元数据。
            target_class_name: 目标 Java 类名。
            method_signature_text: 目标方法签名文本。
            input_generator_code: 输入生成器 Java 代码。
            work_dir: MT 工作目录（存放 rac_out / pbt_bin 等临时文件）。
            candidate_index: 候选索引（用于 TestDriver 类名区分）。
            seed: 随机种子，None 时自动生成。

        Returns:
            :class:`MutantVerdict`。
        """
        if seed is None:
            seed = random.randint(0, 2**63 - 1)

        mutant_id_tag = f"mt_{mutant.major_id:03d}"
        rac_out = work_dir / "rac_out" / mutant_id_tag
        pbt_bin = work_dir / "pbt_bin" / mutant_id_tag
        java_src = work_dir / "java_src" / mutant_id_tag
        start = time.perf_counter()

        verdict = INVALID
        valid_inputs: Optional[int] = None
        total_runs: Optional[int] = None
        try:
            verdict, valid_inputs, total_runs = self._verify_inner(
                mutant=mutant,
                target_class_name=target_class_name,
                method_signature_text=method_signature_text,
                input_generator_code=input_generator_code,
                rac_out=rac_out,
                pbt_bin=pbt_bin,
                java_src=java_src,
                candidate_index=candidate_index,
                seed=seed,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start
            _logger.error("RP MT mutant %d 异常: %s", mutant.major_id, exc)
            verdict = INVALID

        elapsed = time.perf_counter() - start
        _logger.debug(
            "RP MT mutant %d: verdict=%s, elapsed=%.2fs",
            mutant.major_id,
            verdict,
            elapsed,
        )
        return MutantVerdict(
            major_id=mutant.major_id,
            operator=mutant.operator,
            line=mutant.line,
            verdict=verdict,
            elapsed_seconds=round(elapsed, 3),
            valid_inputs=valid_inputs,
            total_runs=total_runs,
        )

    def _verify_inner(
        self,
        *,
        mutant: MajorMutant,
        target_class_name: str,
        method_signature_text: str,
        input_generator_code: str,
        rac_out: Path,
        pbt_bin: Path,
        java_src: Path,
        candidate_index: int,
        seed: int,
    ) -> tuple[str, Optional[int], Optional[int]]:
        """内部验证逻辑, 返回 (verdict, valid_inputs, total_runs)。

        valid_inputs / total_runs 仅在 TestDriver 成功跑完且 JSON 解析成功
        时填充, 其它失败路径返回 None (含 RAC 编译失败 / TestDriver 构建失败 /
        TestDriver 异常退出 / 输出 JSON 解析失败)。
        """
        # Step 1: RAC 编译变异体
        rac_result = self._rac.run(
            java_file_path=str(mutant.source_file),
            output_dir=str(rac_out),
            class_name=target_class_name,
        )

        if not rac_result.success:
            combined = f"{rac_result.tool_result.stdout}\n{rac_result.tool_result.stderr}"
            if _is_infra_error(rac_result.tool_result.exit_code, combined):
                _logger.warning("RP MT mutant %d RAC infra error", mutant.major_id)
                return INVALID, None, None
            _logger.debug("RP MT mutant %d RAC 编译失败 → killed", mutant.major_id)
            return KILLED, None, None

        # Step 2: 构建 MT 专用 TestDriver
        template_path = Path(self._config.paths.template_dir) / "MTTestDriverTemplate.java"
        java_src.mkdir(parents=True, exist_ok=True)

        try:
            build_result = _build_mt_test_driver(
                mutant_id=mutant.major_id,
                candidate_index=candidate_index,
                target_class_name=target_class_name,
                method_signature_text=method_signature_text,
                input_generator_code=input_generator_code,
                template_path=template_path,
                output_dir=java_src,
            )
        except (FileNotFoundError, ValueError) as exc:
            _logger.warning(
                "RP MT mutant %d TestDriver 构建失败: %s → killed",
                mutant.major_id,
                exc,
            )
            return KILLED, None, None

        # Step 3: javac 编译 TestDriver
        pbt_bin.mkdir(parents=True, exist_ok=True)
        compile_result = self._pbt.compile_test_driver(
            test_driver_java=str(build_result.java_path),
            rac_output_dir=str(rac_out),
            pbt_bin_dir=str(pbt_bin),
        )
        if not compile_result.success:
            _logger.debug(
                "RP MT mutant %d TestDriver 编译失败 → killed", mutant.major_id
            )
            return KILLED, None, None

        # Step 4: java 运行 TestDriver
        test_runs = self._config.pbt.test_runs
        early_stop = str(self._config.pbt.early_stop_on_fail).lower()
        early_stop_max = str(self._config.pbt.early_stop_max_fails)
        sample_max = str(self._config.pbt.failure_sample_max)

        program_args: Sequence[str] = [
            str(test_runs),
            early_stop,
            early_stop_max,
            sample_max,
            str(seed),
        ]

        run_result = self._pbt.run_test_driver(
            test_driver_class=build_result.class_name,
            pbt_bin_dir=str(pbt_bin),
            rac_output_dir=str(rac_out),
            program_args=program_args,
        )

        # Step 5: 判定结果（统一委派给 verdict_from_pbt_stats，便于后续修改公式）

        # OpenJML RAC 在变异体上常常会把"Runtime exception while evaluating
        # postconditions - postconditions are undefined in JML"这类诊断信息
        # 写到 stdout（不是 stderr），导致 stdout 顶部不是 JSON。
        # PBTRunner.run_test_driver 默认按整行 stripped 解析 JSON,所以会失败。
        # 这里从 stdout 末尾兜底抽取最后一个 JSON 对象行。
        parsed = run_result.parsed_json
        if parsed is None and run_result.tool_result and run_result.tool_result.exit_code == 0:
            parsed = _extract_trailing_json(run_result.tool_result.stdout or "")
            if parsed is not None:
                _logger.debug(
                    "RP MT mutant %d stdout 含 RAC 噪音, 兜底解析末尾 JSON 成功",
                    mutant.major_id,
                )

        if parsed is None:
            # TestDriver 非零退出或 stdout 解析失败 → 检查是否 infra
            combined = ""
            if run_result.tool_result:
                combined = f"{run_result.tool_result.stdout}\n{run_result.tool_result.stderr}"
            if _is_infra_error(run_result.tool_result.exit_code, combined):
                return INVALID, None, None
            # 非 infra 的非零退出码 → killed（如未捕获的 JmlAssertionError 直接抛出导致 JVM 异常退出）
            _logger.debug(
                "RP MT mutant %d TestDriver 非零退出或无可解析输出 → killed", mutant.major_id
            )
            return KILLED, None, None

        post_fail = int(parsed.get("post_fail_count", 0))
        other_exception = int(parsed.get("other_exception_count", 0))
        total_runs_v = int(parsed.get("total_runs", 0))
        valid_inputs_v = int(parsed.get("valid_inputs", 0))

        verdict = verdict_from_pbt_stats(
            post_fail_count=post_fail,
            other_exception_count=other_exception,
            total_runs=total_runs_v,
        )
        _logger.debug(
            "RP MT mutant %d verdict=%s (post_fail=%d, other_exc=%d, total=%d, valid=%d)",
            mutant.major_id,
            verdict,
            post_fail,
            other_exception,
            total_runs_v,
            valid_inputs_v,
        )
        return verdict, valid_inputs_v, total_runs_v

    def verify_all(
        self,
        mutants: List[MajorMutant],
        *,
        target_class_name: str,
        method_signature_text: str,
        input_generator_code: str,
        work_dir: Path,
        candidate_index: int = 0,
        workers: Optional[int] = None,
    ) -> RPPathResult:
        """对一组变异体并发执行 RP 路径 MT 验证。

        Args:
            mutants: 变异体列表。
            target_class_name: 目标 Java 类名。
            method_signature_text: 目标方法签名文本。
            input_generator_code: 输入生成器 Java 代码。
            work_dir: MT 工作目录。
            candidate_index: 候选索引。
            workers: 并发线程数，缺省取 ``config.mt.workers``。

        Returns:
            :class:`RPPathResult`。
        """
        n_workers = workers if workers is not None else self._config.mt.workers
        start = time.perf_counter()
        verdicts: List[MutantVerdict] = []
        shutdown_event = get_shutdown_event()

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_map = {
                executor.submit(
                    self.verify_single_mutant,
                    mutant,
                    target_class_name=target_class_name,
                    method_signature_text=method_signature_text,
                    input_generator_code=input_generator_code,
                    work_dir=work_dir,
                    candidate_index=candidate_index,
                ): mutant
                for mutant in mutants
            }
            for future in as_completed(future_map):
                if shutdown_event.is_set():
                    _logger.info(
                        "RP MT 检测到 shutdown_event,取消剩余 future 并退出"
                    )
                    for f in future_map:
                        if not f.done():
                            f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    verdicts.append(future.result())
                except Exception as exc:
                    mutant = future_map[future]
                    _logger.error(
                        "RP MT 并发异常 (mutant %d): %s", mutant.major_id, exc
                    )
                    verdicts.append(
                        MutantVerdict(
                            major_id=mutant.major_id,
                            operator=mutant.operator,
                            line=mutant.line,
                            verdict=INVALID,
                            elapsed_seconds=0.0,
                        )
                    )

        verdicts.sort(key=lambda v: v.major_id)

        sum_valid = 0
        sum_total = 0
        for v in verdicts:
            if v.valid_inputs is not None and v.total_runs is not None:
                sum_valid += v.valid_inputs
                sum_total += v.total_runs
        if sum_total > 0:
            valid_input_ratio: Optional[float] = round(sum_valid / sum_total, 4)
        else:
            valid_input_ratio = None

        total_elapsed = time.perf_counter() - start
        _logger.info(
            "RP MT 完成: %d 变异体, elapsed=%.2fs, valid_input_ratio=%s",
            len(verdicts),
            total_elapsed,
            f"{valid_input_ratio:.4f}" if valid_input_ratio is not None else "n/a",
        )
        return RPPathResult(
            verdicts=verdicts,
            total_elapsed_seconds=total_elapsed,
            valid_input_ratio=valid_input_ratio,
        )
