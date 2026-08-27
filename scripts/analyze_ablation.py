#!/usr/bin/env python
"""单组(消融实验某一种模式)统计报告 —— 直观版。

输入: 该组合并后的 success/failed JSONL(由 export_dataset.py 导出)。
输出: 一份中文 Markdown 报告。重点是开头把「带 OJ 最终答案的任务数」等
可比指标单独列张表, 方便与其它组(norp / oj)一字排开对比。

用法::
    python scripts/analyze_ablation.py \
        --jsonl-dir "a/实验结果/norp/merged/jsonl/20260827_094506" \
        --output "a/实验结果/norp/统计报告.md" \
        --title "norp(关RP)"
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import List


def load_jsonl(path: Path) -> List[dict]:
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def find_file(d: Path, prefix: str) -> Path:
    for c in sorted(d.glob(f"{prefix}_*.jsonl")):
        if c.name.startswith(prefix):
            return c
    raise FileNotFoundError(f"{d} 下没有 {prefix}_*.jsonl")


MT_BUCKETS = ["[0,0.1)", "[0.1,0.2)", "[0.2,0.3)", "[0.3,0.4)", "[0.4,0.5)",
              "[0.5,0.6)", "[0.6,0.7)", "[0.7,0.8)", "[0.8,0.9)", "[0.9,1.0]"]


def mt_bucket(score: float) -> str:
    b = min(int(score * 10), 9)
    return MT_BUCKETS[b]


def bucket_table(scores: List[float]) -> str:
    if not scores:
        return "n=0"
    c: Counter = Counter()
    for s in scores:
        c[mt_bucket(s)] += 1
    rows = [f"| {b} | {c.get(b, 0)} |" for b in MT_BUCKETS]
    return "| 区间 | 数量 |\n|---|---|\n" + "\n".join(rows)


def mt_line(seq: List[float]) -> str:
    if not seq:
        return "无"
    return f"均值={statistics.mean(seq):.3f} 中位={statistics.median(seq):.3f} (n={len(seq)})"


def round_dist(vals: List[int]) -> str:
    if not vals:
        return "n=0"
    return (f"n={len(vals)} 均值={statistics.mean(vals):.2f} 中位={statistics.median(vals):.1f} | "
            f"分布(轮次:个数) {dict(sorted(Counter(vals).items()))}")


def render(title: str, success: List[dict], failed: List[dict]) -> str:
    L: List[str] = []
    A = L.append
    total = len(success) + len(failed)
    fa = lambda r: r.get("final_answer") or {}

    final_oj = [r for r in success if r.get("path") == "openjml"]
    final_rp = [r for r in success if r.get("path") == "rp"]
    oj_pass = [r for r in success if fa(r).get("oj")]
    rp_pass = [r for r in success if fa(r).get("rp")]
    both = [r for r in success if fa(r).get("oj") and fa(r).get("rp")]
    only_oj = [r for r in success if fa(r).get("oj") and not fa(r).get("rp")]
    only_rp = [r for r in success if fa(r).get("rp") and not fa(r).get("oj")]

    oj_final_mt = [float(fa(r)["oj"]["mutation_score"]) for r in final_oj
                   if fa(r)["oj"].get("mutation_score") is not None]
    rp_final_mt = [float(fa(r)["rp"]["mutation_score"]) for r in final_rp
                   if fa(r)["rp"].get("mutation_score") is not None]
    oj_all_mt: List[float] = []
    rp_all_mt: List[float] = []
    for r in success:
        for c in (r.get("oj_candidates") or []):
            if c.get("mutation_score") is not None:
                oj_all_mt.append(float(c["mutation_score"]))
        for c in (r.get("rp_candidates") or []):
            if c.get("mutation_score") is not None:
                rp_all_mt.append(float(c["mutation_score"]))

    first_pass: List[int] = []
    final_round: List[int] = []
    for r in success:
        ars = [int(c["a_round"]) for c in (r.get("oj_candidates") or [])]
        ars += [int(c["a_round"]) for c in (r.get("rp_candidates") or [])]
        if ars:
            first_pass.append(min(ars))
        p = r.get("path")
        if p == "openjml" and fa(r).get("oj"):
            final_round.append(int(fa(r)["oj"]["a_round"]))
        elif p == "rp" and fa(r).get("rp"):
            final_round.append(int(fa(r)["rp"]["a_round"]))

    ti = to = calls = 0
    for r in success + failed:
        for key in ("task_a", "input_gen"):
            for m in ((r.get("llm_history") or {}).get(key) or []):
                cm = m.get("call_meta")
                if isinstance(cm, dict) and cm.get("input_tokens") is not None:
                    ti += int(cm.get("input_tokens") or 0)
                    to += int(cm.get("output_tokens") or 0)
                    calls += 1

    fc = Counter((r.get("failure_info") or {}).get("failure_stage", "unknown") for r in failed)

    A(f"# {title} 组 统计报告\n")
    A("> 数据来源: 合并后的 success/failed JSONL。a_round 为 0-based(0=第1轮)。\n")

    A("## 核心结论\n")
    A(f"- 共 **{total}** 个任务: 成功 **{len(success)}**({len(success)/total*100:.1f}%) , "
      f"失败 **{len(failed)}**({len(failed)/total*100:.1f}%)。")
    A(f"- **带 OJ 最终答案的任务数 = {len(final_oj)} 个({len(final_oj)/total*100:.1f}%)**; "
      f"带 RP 最终答案的任务数 = {len(final_rp)} 个({len(final_rp)/total*100:.1f}%)。")
    A(f"- 成功任务构成: 仅 OJ 通过 {len(only_oj)} / 仅 RP 通过 {len(only_rp)} / 两者都通过 {len(both)}。\n")

    A("## 一、任务结果\n")
    A("| 总任务 | 成功 | 失败 | 成功率 |")
    A("|---|---|---|---|")
    A(f"| {total} | {len(success)} | {len(failed)} | {len(success)/total*100:.1f}% |\n")

    A("## 二、最终答案通道(与其它组对比的核心口径)\n")
    A("| 指标 | 数量 | 占全体任务 |")
    A("|---|---|---|")
    A(f"| **带 OJ 最终答案的任务数** | **{len(final_oj)}** | **{len(final_oj)/total*100:.1f}%** |")
    A(f"| 带 RP 最终答案的任务数 | {len(final_rp)} | {len(final_rp)/total*100:.1f}% |")
    A("")
    A("> 口径: 「带 OJ 最终答案」= 该任务最终选中的答案走 openjml 通道(即 path=openjml)。"
      "norp / oj 组无 RP, 此数与 success 数一致; full 组则可能小于 success 数。\n")

    A("## 三、通过通道(成功任务内)\n")
    A("| OJ 通过 | RP 通过 | 仅 OJ | 仅 RP | 两者都过 |")
    A("|---|---|---|---|---|")
    A(f"| {len(oj_pass)} | {len(rp_pass)} | {len(only_oj)} | {len(only_rp)} | {len(both)} |\n")

    A("## 四、答案质量 MT(变异分数, 0~1)\n")
    A("### OJ 最终答案 MT\n")
    A(f"{mt_line(oj_final_mt)}\n")
    A(bucket_table(oj_final_mt))
    A("")
    if rp_final_mt:
        A("### RP 最终答案 MT\n")
        A(f"{mt_line(rp_final_mt)}\n")
        A(bucket_table(rp_final_mt))
        A("")
    else:
        A("### RP 最终答案 MT\n")
        A("本组无 RP 答案。\n")
    A("### 全部通过答案(候选)MT\n")
    A(f"- OJ 候选: {mt_line(oj_all_mt)}\n")
    A(f"- RP 候选: {mt_line(rp_all_mt)}\n")

    A("## 五、轮次(成功任务)\n")
    A(f"- 首次出现通过答案的轮次: {round_dist(first_pass)}")
    A(f"- 最终答案出现的轮次: {round_dist(final_round)}")
    A("")

    A("## 六、Token 开销\n")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| input tokens | {ti} |")
    A(f"| output tokens | {to} |")
    A(f"| 合计 | {ti+to} |")
    A(f"| LLM 调用数(有 call_meta) | {calls} |")
    A(f"| 平均 tokens/任务 | {(ti+to)/total:.0f} |")
    A("")

    A("## 七、失败原因分布\n")
    A(f"`{dict(fc)}`\n")

    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--title", type=str, default="")
    args = ap.parse_args()

    success = load_jsonl(find_file(args.jsonl_dir, "success"))
    failed = load_jsonl(find_file(args.jsonl_dir, "failed"))
    md = render(args.title or args.jsonl_dir.parent.parent.parent.name, success, failed)
    args.output.write_text(md, encoding="utf-8")
    print(f"[report] 已写入 {args.output} ({len(md)} 字符)")
    print(f"[report] success={len(success)} failed={len(failed)} total={len(success)+len(failed)}")
    return 0


if __name__ == "__main__":
    main()