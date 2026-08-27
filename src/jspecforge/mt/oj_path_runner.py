"""OJ 路径 MT 验证：对每个变异体重跑 OpenJML ESC。

验证逻辑（design §3.1）：

- ESC 通过（SUCCESS）→ survived
- ESC 失败（非 SUCCESS / 非 infra）→ killed
- ESC 超时 → survived（保守处理，design §3.1）
- 基础设施错误 → invalid（不计入分母）

并发执行，由 ``ThreadPoolExecutor`` 调度（参考 MT_Demo plan_c 实测 14 并发）。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from jspecforge.config.settings import AppConfig
from jspecforge.core.shutdown import get_shutdown_event
from jspecforge.mt.mutant_loader import MajorMutant
from jspecforge.mt.score_calculator import INVALID, KILLED, MutantVerdict, SURVIVED, TIMEOUT
from jspecforge.tools.oj_classifier import classify_oj_result
from jspecforge.tools.openjml_tool import OpenJMLRunner
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("mt.oj_path_runner")

# OJ label → MT 裁决映射
# SUCCESS = 规约仍然通过 = 变异体存活
# LOGIC_ERROR / SYNTAX_ERROR / UNSUPPORTED_JML = 规约检测到变异 = killed
# OPENJML_TIMEOUT / HARNESS_TIMEOUT = 超时 = survived（保守）
# INFRA_ERROR / UNKNOWN = 基础设施问题 = invalid
_OJ_LABEL_TO_VERDICT: dict[str, str] = {
    "SUCCESS": SURVIVED,
    "LOGIC_ERROR": KILLED,
    "SYNTAX_ERROR": KILLED,
    "UNSUPPORTED_JML": KILLED,
    "OPENJML_TIMEOUT": TIMEOUT,
    "HARNESS_TIMEOUT": TIMEOUT,
    "INFRA_ERROR": INVALID,
    "UNKNOWN": INVALID,
}

# 基础设施错误关键词检测（参考 MT_Demo _is_infra_error）
_INFRA_KEYWORDS: tuple[str, ...] = (
    "e_accessdenied",
    "wsl/service",
    "access is denied",
    "拒绝访问",
)


def _is_infra_error(returncode: int, output: str) -> bool:
    """判断是否为基础设施错误（MT_Demo 实测经验）。

    退出码 -1（Python 层超时）或大负数通常为 WSL/权限问题。
    """
    if returncode in {-1, -1 & 0xFFFFFFFF}:  # -1 或 4294967295
        return True
    lowered = output.lower()
    return any(kw in lowered for kw in _INFRA_KEYWORDS)


@dataclass(frozen=True)
class OJPathResult:
    """OJ 路径 MT 单方法验证结果。

    Attributes:
        verdicts: 各变异体裁决列表。
        total_elapsed_seconds: 总耗时（秒）。
    """

    verdicts: List[MutantVerdict]
    total_elapsed_seconds: float


class OJPathRunner:
    """OJ 路径 MT 验证器（并发执行 ESC 重验）。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._openjml = OpenJMLRunner(config)

    def verify_single_mutant(
        self,
        mutant: MajorMutant,
        *,
        timeout: Optional[int] = None,
    ) -> MutantVerdict:
        """对单个变异体重跑 ESC 并判定 killed / survived / invalid。

        Args:
            mutant: 变异体元数据（含 .java 文件路径）。
            timeout: ESC 超时秒数，缺省取 ``config.openjml.timeout``。

        Returns:
            :class:`MutantVerdict`。
        """
        java_path = str(mutant.source_file)
        actual_timeout = timeout if timeout is not None else self._config.openjml.timeout
        start = time.perf_counter()

        try:
            tool_result = self._openjml.run_esc(java_path, timeout=actual_timeout)
            elapsed = time.perf_counter() - start

            label, _ = classify_oj_result(
                tool_result.exit_code,
                tool_result.stdout,
                tool_result.stderr,
                tool_result.elapsed,
                self._config.openjml.timeout_threshold_seconds,
            )

            # 基础设施错误的额外检测（WSL/权限等 exit_code=2 之外的情况）
            combined_output = f"{tool_result.stdout}\n{tool_result.stderr}"
            if _is_infra_error(tool_result.exit_code, combined_output):
                label = "INFRA_ERROR"

            verdict = _OJ_LABEL_TO_VERDICT.get(label, INVALID)

        except FileNotFoundError:
            elapsed = time.perf_counter() - start
            _logger.error("变异体 .java 文件不存在: %s", java_path)
            verdict = INVALID

        except Exception as exc:
            elapsed = time.perf_counter() - start
            _logger.error("OJ 路径变异体 %d 验证异常: %s", mutant.major_id, exc)
            verdict = INVALID

        _logger.debug(
            "OJ MT mutant %d: verdict=%s, elapsed=%.2fs",
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
        )

    def verify_all(
        self,
        mutants: List[MajorMutant],
        *,
        timeout: Optional[int] = None,
        workers: Optional[int] = None,
    ) -> OJPathResult:
        """对一组变异体并发执行 OJ 路径 MT 验证。

        Args:
            mutants: 变异体列表。
            timeout: 单个变异体 ESC 超时秒数。
            workers: 并发线程数，缺省取 ``config.mt.workers``。

        Returns:
            :class:`OJPathResult`。
        """
        n_workers = workers if workers is not None else self._config.mt.workers
        start = time.perf_counter()
        verdicts: List[MutantVerdict] = []
        shutdown_event = get_shutdown_event()

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            future_map = {
                executor.submit(self.verify_single_mutant, m, timeout=timeout): m
                for m in mutants
            }
            for future in as_completed(future_map):
                if shutdown_event.is_set():
                    _logger.info(
                        "OJ MT 检测到 shutdown_event,取消剩余 future 并退出"
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
                    _logger.error("OJ MT 并发异常 (mutant %d): %s", mutant.major_id, exc)
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
        total_elapsed = time.perf_counter() - start
        _logger.info(
            "OJ MT 完成: %d 变异体, elapsed=%.2fs",
            len(verdicts),
            total_elapsed,
        )
        return OJPathResult(verdicts=verdicts, total_elapsed_seconds=total_elapsed)
