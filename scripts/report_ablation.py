#!/usr/bin/env python
"""消融实验多组对比报告(直观版)。

读入各组合并后的 success/failed JSONL, 输出一份中文对比报告。核心:
- 顶部一段"核心结论"(三句话讲清);
- 一张核心指标对比表(重点突出"带 OJ 最终答案的任务数");
- 同一批任务在不同组下的"去向"逐条对比(RP 兜底救回 / 反而更差 / 促进 OJ)。

用法::
    python scripts/report_ablation.py \
        --group "full:C:/.../full/merged/jsonl/20260826_112555" \
        --group "norp:C:/.../norp/merged/jsonl/20260827_094506" \
        --output "a/实验结果/对比报告_full_vs_norp.md"
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple


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


class Group:
    def __init__(self, label: str, jsonl_dir: Path):
        self.label = label
        self.success = load_jsonl(find_file(jsonl_dir, "success"))
        self.failed = load_jsonl(find_file(jsonl_dir, "failed"))
        self.all = self.success + self.failed
        self.map: Dict[Tuple, dict] = {}
        self.status: Dict[Tuple, str] = {}
        for r in self.success:
            k = (r["source_dataset_name"], r["source_dataset_index"])
            self.map[k] = r
            self.status[k] = "success"
        for r in self.failed:
            k = (r["source_dataset_name"], r["source_dataset_index"])
            self.map[k] = r
            self.status[k] = "failed"

    @property
    def total(self) -> int:
        return len(self.all)

    @property
    def n_success(self) -> int:
        return len(self.success)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    def final_oj(self) -> int:
        return sum(1 for r in self.success if r.get("path") == "openjml")

    def final_rp(self) -> int:
        return sum(1 for r in self.success if r.get("path") == "rp")

    def oj_pass(self) -> int:
        return sum(1 for r in self.success if (r.get("final_answer") or {}).get("oj"))

    def rp_pass(self) -> int:
        return sum(1 for r in self.success if (r.get("final_answer") or {}).get("rp"))

    def both(self) -> int:
        return sum(1 for r in self.success
                   if (r.get("final_answer") or {}).get("oj") and (r.get("final_answer") or {}).get("rp"))

    def only_oj(self) -> int:
        return sum(1 for r in self.success
                   if (r.get("final_answer") or {}).get("oj") and not (r.get("final_answer") or {}).get("rp"))

    def only_rp(self) -> int:
        return sum(1 for r in self.success
                   if (r.get("final_answer") or {}).get("rp") and not (r.get("final_answer") or {}).get("oj"))

    def oj_final_mt(self) -> List[float]:
        out = []
        for r in self.success:
            if r.get("path") != "openjml":
                continue
            o = (r.get("final_answer") or {}).get("oj")
            if o and o.get("mutation_score") is not None:
                out.append(float(o["mutation_score"]))
        return out

    def rp_final_mt(self) -> List[float]:
        out = []
        for r in self.success:
            if r.get("path") != "rp":
                continue
            p = (r.get("final_answer") or {}).get("rp")
            if p and p.get("mutation_score") is not None:
                out.append(float(p["mutation_score"]))
        return out

    def final_rounds(self) -> List[int]:
        out = []
        for r in self.success:
            p = r.get("path")
            fa = r.get("final_answer") or {}
            if p == "openjml" and fa.get("oj"):
                out.append(int(fa["oj"]["a_round"]))
            elif p == "rp" and fa.get("rp"):
                out.append(int(fa["rp"]["a_round"]))
        return out

    def first_pass_rounds(self) -> List[int]:
        out = []
        for r in self.success:
            ars = [int(c["a_round"]) for c in (r.get("oj_candidates") or [])]
            ars += [int(c["a_round"]) for c in (r.get("rp_candidates") or [])]
            if ars:
                out.append(min(ars))
        return out

    def tokens(self) -> Tuple[int, int, int]:
        ti = to = calls = 0
        for r in self.all:
            for key in ("task_a", "input_gen"):
                for m in ((r.get("llm_history") or {}).get(key) or []):
                    cm = m.get("call_meta")
                    if isinstance(cm, dict) and cm.get("input_tokens") is not None:
                        ti += int(cm.get("input_tokens") or 0)
                        to += int(cm.get("output_tokens") or 0)
                        calls += 1
        return ti, to, calls

    def failure_stages(self) -> Counter:
        return Counter((r.get("failure_info") or {}).get("failure_stage", "unknown") for r in self.failed)


def join_two(base: Group, other: Group) -> Dict[str, int]:
    """按同源任务 join, 统计两组成功与否的交/并/差。"""
    both_ok = base_only = other_only = both_fail = 0
    base_only_oj = base_only_rp = 0
    for k in set(base.map) | set(other.map):
        b_ok = base.status.get(k) == "success"
        o_ok = other.status.get(k) == "success"
        if b_ok and o_ok:
            both_ok += 1
        elif b_ok and not o_ok:
            base_only += 1
            if (base.map.get(k) or {}).get("path") == "openjml":
                base_only_oj += 1
            else:
                base_only_rp += 1
        elif not b_ok and o_ok:
            other_only += 1
        else:
            both_fail += 1
    return dict(both_ok=both_ok, base_only=base_only, other_only=other_only,
                both_fail=both_fail, base_only_oj=base_only_oj, base_only_rp=base_only_rp)


def mt_summary(scores: List[float]) -> str:
    if not scores:
        return "n=0"
    return (f"n={len(scores)} 均值={statistics.mean(scores):.3f} 中位={statistics.median(scores):.3f} "
            f"≥0.6={sum(1 for s in scores if s >= 0.6)/len(scores)*100:.0f}% "
            f"≥0.8={sum(1 for s in scores if s >= 0.8)/len(scores)*100:.0f}%")


def round_summary(vals: List[int]) -> str:
    if not vals:
        return "n=0"
    return f"n={len(vals)} 均值={statistics.mean(vals):.2f} 中位={statistics.median(vals):.1f}"

def round_full(vals: List[int]) -> str:
    if not vals:
        return "n=0"
    return f"均值={statistics.mean(vals):.2f} 中位={statistics.median(vals):.1f} 分布{dict(sorted(Counter(vals).items()))}"


def render(groups: List[Group]) -> str:
    L: List[str] = []
    A = L.append
    g0, g1 = groups[0], groups[1]
    j = join_two(g0, g1)

    A("# 消融实验对比报告\n")
    A(f"> 对比组: **{g0.label}** vs **{g1.label}** | 同源任务 {g0.total} 个\n")

    # ===== 核心结论 =====
    A("## 核心结论\n")
    A(f"1. 同样 {g0.total} 个任务: **{g0.label}** 成功 {g0.n_success}({g0.n_success/g0.total*100:.0f}%), "
      f"**{g1.label}** 成功 {g1.n_success}({g1.n_success/g1.total*100:.0f}%)。")
    A(f"2. **带 OJ 最终答案的任务数**: {g0.label} = **{g0.final_oj()}** 个({g0.final_oj()/g0.total*100:.1f}%), "
      f"{g1.label} = **{g1.final_oj()}** 个({g1.final_oj()/g1.total*100:.1f}%)——这是两组可直接对比的口径(norp 无 RP)。")
    A(f"3. RP 兜底净贡献: 救回 {j['base_only']} 个(其中 {j['base_only_rp']} 个最终走 RP、{j['base_only_oj']} 个最终走 OJ, "
      f"后者即 RP 促进 OJ 通过), 但同时有 {j['other_only']} 个任务在 {g1.label} 下能过 OJ、在 {g0.label} 下反而失败; "
      f"净贡献 {j['base_only']} − {j['other_only']} = **{j['base_only'] - j['other_only']}** 个。\n")

    # ===== 核心指标表 =====
    A("## 一、核心指标对比\n")
    A("| 指标 | " + " | ".join(g.label for g in groups) + " |")
    A("|---|" + "---|" * len(groups))
    A(f"| 总任务数 | " + " | ".join(str(g.total) for g in groups) + " |")
    A(f"| 成功任务数 | " + " | ".join(str(g.n_success) for g in groups) + " |")
    A(f"| 失败任务数 | " + " | ".join(str(g.n_failed) for g in groups) + " |")
    A(f"| 成功率 | " + " | ".join(f"{g.n_success/g.total*100:.1f}%" for g in groups) + " |")
    A(f"| **带 OJ 最终答案的任务数** | " + " | ".join(str(g.final_oj()) for g in groups) + " |")
    A(f"| 带 RP 最终答案的任务数 | " + " | ".join(str(g.final_rp()) for g in groups) + " |")
    A(f"| OJ 通过的任务数 | " + " | ".join(str(g.oj_pass()) for g in groups) + " |")
    A(f"| RP 通过的任务数 | " + " | ".join(str(g.rp_pass()) for g in groups) + " |")
    A(f"| 仅 OJ 通过 | " + " | ".join(str(g.only_oj()) for g in groups) + " |")
    A(f"| 仅 RP 通过 | " + " | ".join(str(g.only_rp()) for g in groups) + " |")
    A(f"| 两者都通过 | " + " | ".join(str(g.both()) for g in groups) + " |")
    A("")

    A("**OJ 最终答案质量(MT)**")
    for g in groups:
        A(f"- {g.label}: {mt_summary(g.oj_final_mt())}")
    A("")
    A("**RP 最终答案质量(MT)**")
    for g in groups:
        A(f"- {g.label}: {mt_summary(g.rp_final_mt())}")
    A("")
    A("> 口径提醒: (1) full 的最终答案是按 MT 择优选出(两者都过时选 MT 更高者), 所以 full 的 OJ 最终答案 MT"
      "天然偏高, 与 norp 直接比 MT 时请注意这一选择效应; (2) 极少数在最后一轮(a_round=8)才通过、且未跑 MT 的"
      "最终答案(mt_detail=None)不计入 MT 统计。\n")

    # ===== 逐任务去向 =====
    A("## 二、同一批任务在两组下的去向(逐条对比)\n")
    A("| 分类 | 任务数 | 含义 |")
    A("|---|---|---|")
    A(f"| 两组都成功 | {j['both_ok']} | 有没有 RP 兜底都能过 |")
    A(f"| 仅 {g0.label} 成功 | {j['base_only']} | {g0.label} 能过、{g1.label} 过不了(含 RP 兜底救回) |")
    A(f"| 仅 {g1.label} 成功 | {j['other_only']} | {g1.label} 能过、{g0.label} 过不了(RP 的潜在反作用) |")
    A(f"| 两组都失败 | {j['both_fail']} | 都过不了 |")
    A("")
    A(f"- 其中「仅 {g0.label} 成功」的 {j['base_only']} 个里: 最终走 OJ 的 **{j['base_only_oj']}** 个, 最终走 RP 的 **{j['base_only_rp']}** 个。\n")

    # ===== 组内结构 =====
    A("## 三、各组任务结构\n")
    for g in groups:
        A(f"**{g.label}**")
        A(f"- 成功 {g.n_success} 个 = 仅 OJ {g.only_oj()} + 仅 RP {g.only_rp()} + 两者都过 {g.both()}")
        A(f"- 最终答案: OJ {g.final_oj()} 个, RP {g.final_rp()} 个")
        A("")

    # ===== Token =====
    A("## 四、Token 开销\n")
    A("| 组 | input | output | 合计 | 调用数 | 平均/任务 |")
    A("|---|---|---|---|---|---|")
    for g in groups:
        ti, to, calls = g.tokens()
        A(f"| {g.label} | {ti} | {to} | {ti+to} | {calls} | {(ti+to)/g.total:.0f} |")
    A("")

    # ===== 失败原因 =====
    A("## 五、失败原因分布\n")
    for g in groups:
        A(f"- {g.label}: {dict(g.failure_stages())}")
    A("")

    # ===== 轮次 =====
    A("## 六、轮次(0-based, 0=第1轮)\n")
    A("| 组 | 首通过轮次 | 最终答案轮次 |")
    A("|---|---|---|")
    for g in groups:
        A(f"| {g.label} | {round_summary(g.first_pass_rounds())} | {round_summary(g.final_rounds())} |")
    A("")
    for g in groups:
        A(f"**{g.label}** 首通过轮次分布: {round_full(g.first_pass_rounds())}")
        A(f"**{g.label}** 最终答案轮次分布: {round_full(g.final_rounds())}")
        A("")

    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", action="append", required=True,
                    help="格式 label:jsonl_dir, 可多次传入")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    groups: List[Group] = []
    for spec in args.group:
        label, dirs = spec.split(":", 1)
        groups.append(Group(label, Path(dirs)))

    if len(groups) < 2:
        print("[error] --group 至少需要两组")
        return 2

    md = render(groups)
    args.output.write_text(md, encoding="utf-8")
    print(f"[report] 已写入 {args.output} ({len(md)} 字符)")
    for g in groups:
        print(f"[report] {g.label}: total={g.total} success={g.n_success} failed={g.n_failed}")
    return 0


if __name__ == "__main__":
    main()