"""变异分数计算与 mt_detail 构建。

公式（design §4.1）：``mutation_score = killed / (killed + survived)``

- 精度 4 位小数
- ``invalid``（如基础设施错误、RAC 编译失败但非变异导致的）不计入分母
- 分母 = 0 时 score = -1（design §4.2：Major 生成 0 个变异体或 MT 执行异常）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from jspecforge.utils.log_utils import get_logger

_logger = get_logger("mt.score_calculator")

# MT 验证结果枚举
KILLED = "killed"
SURVIVED = "survived"
INVALID = "invalid"
TIMEOUT = "timeout"  # OJ 超时 → survived（保守处理，design §3.1）


@dataclass(frozen=True)
class MutantVerdict:
    """单个变异体的 MT 验证裁决。

    Attributes:
        major_id: Major 变异体 ID。
        operator: 变异算子。
        line: 变异行号。
        verdict: KILLED / SURVIVED / INVALID / TIMEOUT。
        elapsed_seconds: 验证耗时（秒）。
        valid_inputs: TestDriver JSON 输出的 valid_inputs (Phase 4, 仅 RP 路径
            且 TestDriver 成功跑完时填; OJ 路径或 TestDriver 没跑成时为 None)。
        total_runs: 同上, TestDriver JSON 输出的 total_runs。
    """

    major_id: int
    operator: str
    line: int
    verdict: str
    elapsed_seconds: float
    valid_inputs: int | None = None
    total_runs: int | None = None


def compute_mutation_score(verdicts: list[MutantVerdict]) -> tuple[float, int, int, int]:
    """从裁决列表计算变异分数。

    Args:
        verdicts: 所有变异体的裁决结果。

    Returns:
        (mutation_score, killed, survived, invalid)。
        ``mutation_score`` 精度 4 位小数，分母为 0 时返回 -1.0。
    """
    killed = sum(1 for v in verdicts if v.verdict == KILLED)
    survived = sum(1 for v in verdicts if v.verdict == SURVIVED)
    # OJ 超时按 survived 处理（保守），design §3.1
    timeout_count = sum(1 for v in verdicts if v.verdict == TIMEOUT)
    survived += timeout_count
    invalid = sum(1 for v in verdicts if v.verdict == INVALID)

    denominator = killed + survived
    if denominator == 0:
        _logger.warning("变异分数分母为 0 (killed=%d, survived=%d), 返回 -1", killed, survived)
        return -1.0, killed, survived, invalid

    score = round(killed / denominator, 4)
    return score, killed, survived, invalid


def verdict_from_pbt_stats(
    post_fail_count: int,
    other_exception_count: int,
    total_runs: int,
) -> str:
    """从 PBT 跑出的统计结果判定单个变异体的裁决。

    **此函数是 RP 路径 MT 判定逻辑的唯一入口**。后续如果想换计算公式（如
    "按通过率加权"、"NPE 部分扣分"等），**只改本函数即可**，不需要动
    rp_path_runner / orchestrator。

    判定规则（design §3.2，简化为 3 行）：

    1. ``post_fail_count > 0`` → KILLED
       规约抓到 postcondition 违反 = 变异被规约杀死
    2. ``post_fail_count == 0 AND other_exception_count == total_runs`` → INVALID
       100% 非 JML 异常（NPE 等）= 变异让方法在 JVM 层崩溃,
       JML postcondition 来不及检查 = 这种变异不能用来评判规约,
       从分母剔除
    3. 其他情况 → SURVIVED
       要么全部通过(规约太弱), 要么部分 NPE 部分通过
       (有通过的部分说明规约确实没杀掉, 仍可评判)

    边界情况：
    - ``total_runs == 0`` → INVALID（PBT 一次都没跑，无法判断）
    - ``valid_inputs == 0``（全部 generator 错或 precondition 拒绝）→
      调用方自行判 INVALID（这种情况不该走到本函数）

    Args:
        post_fail_count: TestDriver 输出的 postcondition 失败次数。
        other_exception_count: TestDriver 输出的非 JML 异常次数（NPE 等）。
            **必须**用 MTTestDriverTemplate.java（含此字段），不能用主流水线
            模板（主流水线把 NPE 算 valid_inputs，不输出此字段）。
        total_runs: TestDriver 输出的总运行次数（= ``config.pbt.test_runs``）。

    Returns:
        KILLED / SURVIVED / INVALID 三选一。

    Examples:
        >>> verdict_from_pbt_stats(post_fail_count=5, other_exception_count=0, total_runs=1000)
        'killed'
        >>> verdict_from_pbt_stats(post_fail_count=0, other_exception_count=1000, total_runs=1000)
        'invalid'
        >>> verdict_from_pbt_stats(post_fail_count=0, other_exception_count=200, total_runs=1000)
        'survived'
        >>> verdict_from_pbt_stats(post_fail_count=0, other_exception_count=0, total_runs=1000)
        'survived'
    """
    if post_fail_count > 0:
        return KILLED
    if total_runs > 0 and other_exception_count == total_runs:
        return INVALID
    if total_runs == 0:
        return INVALID
    return SURVIVED


def build_mt_detail(
    mutants_used: int,
    killed: int,
    survived: int,
    invalid: int,
    test_path: str,
    total_elapsed_seconds: float,
    mutants_generated: int = 0,
) -> Dict:
    """构建 ``final_answer.mt_detail`` 对象（design §10）。

    口径修正（2026-08-13）：``mutants_total`` 重定义为**实际参与统计的变异体总数**，
    恒等于 ``killed + survived + invalid``，使数据库中
    ``mutants_total = mutants_killed + mutants_survived + mutants_invalid``
    恒成立，invalid 数量可直接读取、可对账。原"Major 截断前生成数"移入新字段
    ``mutants_generated`` 保留。

    Args:
        mutants_used: 实际使用数（截断后）。
        killed: 被"杀死"的变异体数。
        survived: 存活的变异体数。
        invalid: 无效变异体数（不计入 mutation_score 分母，但计入 mutants_total）。
        test_path: ``"openjml"`` 或 ``"rp"``。
        total_elapsed_seconds: MT 执行总耗时（秒）。
        mutants_generated: Major 生成的变异体总数（截断前）。

    Returns:
        与 design §10 字段对齐的字典，``mutants_total`` 为 killed+survived+invalid。
    """
    return {
        "mutants_total": killed + survived + invalid,
        "mutants_generated": mutants_generated,
        "mutants_used": mutants_used,
        "mutants_killed": killed,
        "mutants_survived": survived,
        "mutants_invalid": invalid,
        "test_path": test_path,
        "total_elapsed_seconds": round(total_elapsed_seconds, 2),
    }


def build_final_answer_update(
    mutation_score: float,
    mt_detail: Dict,
    *,
    full_java_with_jml: str | None = None,
    a_round: int | None = None,
    selected_candidate_index: int | None = None,
) -> Dict:
    """构建 ``final_answer`` 需要回写的字段集合。

    对于 path=openjml 只回填 mutation_score + mt_detail。
    对于 path=rp 还需回填 full_java_with_jml / a_round / selected_candidate_index
    （来自最高分候选）。

    Args:
        mutation_score: 变异分数。
        mt_detail: mt_detail 对象。
        full_java_with_jml: 仅 path=rp 时，最高分候选的 Java 源码。
        a_round: 仅 path=rp 时，最高分候选的 a_round。
        selected_candidate_index: 仅 path=rp 时，最高分候选在 pbt_attempts 中的索引。

    Returns:
        需要合并进 ``final_answer`` 的字典。
    """
    update = {
        "mutation_score": mutation_score,
        "mt_detail": mt_detail,
    }
    if full_java_with_jml is not None:
        update["full_java_with_jml"] = full_java_with_jml
    if a_round is not None:
        update["a_round"] = a_round
    if selected_candidate_index is not None:
        update["selected_candidate_index"] = selected_candidate_index
    return update
