#!/usr/bin/env python
"""MT 后处理脚本入口。

用法::

    # 指定 Export 输出目录（推荐，自动发现 success_*.jsonl）
    python scripts/run_mt.py --input-dir output/20260506_162032

    # 指定具体文件
    python scripts/run_mt.py --input output/success.jsonl --output output/success_with_mt.jsonl
    python scripts/run_mt.py --input output/success.jsonl --output output/success_with_mt.jsonl --workers 8 --max-mutants 30

从 success.jsonl 读取已完成任务，对每条记录执行 Major 变异测试，
回填 mutation_score / mt_detail 字段，输出到新 jsonl 文件。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# 将 src/ 加入 import 路径，使 ``jspecforge`` 包可被找到
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from jspecforge.config.loader import load_config
from jspecforge.mt.orchestrator import MTOrchestrator
from jspecforge.utils.log_utils import get_logger

_logger = get_logger("scripts.run_mt")


def _find_success_jsonl(directory: Path) -> Path | None:
    """在目录中查找 success_*.jsonl 文件。

    如果找到多个，优先选最新的（按修改时间）。
    返回 None 表示未找到。
    """
    candidates = sorted(directory.glob("success_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="JSpecForge MT 后处理：对 success.jsonl 执行变异测试并回填分数",
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input",
        help="输入 success.jsonl 文件路径（与 --input-dir 互斥）",
    )
    input_group.add_argument(
        "--input-dir",
        help="Export 输出目录（如 output/20260506_162032），自动发现 success_*.jsonl（与 --input 互斥）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出 jsonl 文件路径（缺省: 同目录下 success_with_mt_<ts>.jsonl）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="OJ 路径并发线程数（覆盖 config.mt.workers）",
    )
    parser.add_argument(
        "--max-mutants",
        type=int,
        default=None,
        help="每方法最大变异体数（覆盖 config.mt.max_mutants_per_method）",
    )
    parser.add_argument(
        "--work-dir",
        default=None,
        help="MT 工作临时目录（缺省: {config.paths.tmp_dir}/mt_work）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径（缺省: config.yaml）",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── 解析输入路径 ──────────────────────────────
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.is_dir():
            print(f"错误: 目录不存在: {input_dir}", file=sys.stderr)
            sys.exit(1)
        input_path = _find_success_jsonl(input_dir)
        if input_path is None:
            print(f"错误: 目录中未找到 success_*.jsonl: {input_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"自动发现输入文件: {input_path}")
    else:
        input_path = Path(args.input)
        if not input_path.is_file():
            print(f"错误: 输入文件不存在: {input_path}", file=sys.stderr)
            sys.exit(1)

    # ── 解析输出路径 ──────────────────────────────
    if args.output:
        output_path = Path(args.output)
    else:
        # 默认输出到输入文件同目录，带时间戳
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = input_path.parent / f"success_with_mt_{ts}.jsonl"

    work_dir = Path(args.work_dir) if args.work_dir else None

    # 加载配置
    config = load_config(args.config)
    if not config.mt.enabled:
        print("错误: config.mt.enabled=false，MT 未启用", file=sys.stderr)
        sys.exit(1)

    # 执行 MT
    orchestrator = MTOrchestrator(config)
    total = orchestrator.run(
        input_path=input_path,
        output_path=output_path,
        work_dir=work_dir,
        max_mutants=args.max_mutants,
        workers=args.workers,
    )

    print(f"MT 完成: {total} 条记录 → {output_path}")


if __name__ == "__main__":
    main()
